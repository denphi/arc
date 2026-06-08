from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pty
import shlex
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from arc.contracts.agent import AgentContract
from arc.core.env import env_flag
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.research import ExperimentPlan
from arc.session import session_paths


_PROMPT = """\
You are generating an ARC Sim2L artifact.

Create exactly these files in the current working directory:
- workflow.py
- sim2l.yaml

workflow.py requirements:
- Define def simulate(**inputs) -> dict
- Read every input via inputs.get("name", default)
- Return only numeric scalar outputs in a dict
- Only import from the allowed modules listed below.
- If an allowed external runtime package is required, import it inside simulate()
  so artifact registration can still perform syntax/static checks on hosts where
  that runtime is not installed.
- Do not use dunder names or dunder attributes such as __dict__, __class__, or x.__tan__
- Use normal math helpers or local helper functions instead of hidden/internal methods
- Keep code deterministic

Allowed imports:
{allowed_imports}

sim2l.yaml requirements:
- Include name, description, inputs, and outputs
- Inputs and outputs must match workflow.py
- Inputs must use numeric defaults

Experiment plan JSON:
{plan_json}

If Experiment plan JSON includes build_contexts, treat them as authoritative
pre-build domain context from package workflows. Satisfy their requirements,
use their facts and provenance, and preserve their acceptance criteria in the
generated artifact behavior where applicable.
"""

PermissionCallback = Callable[[str, str, str], Awaitable[str]]
"""async (approval_id, command, reason) -> decision

decision is one of: accept | acceptForSession | decline | abort
"""

ProgressCallback = Callable[[str], None]
"""sync (message) -> None — called with short human-readable status lines"""


class CodexApprovalError(RuntimeError):
    """Raised when a user approval decision stops a Codex run."""

    codex_approval_decision: str

    def __init__(self, message: str, decision: str):
        super().__init__(message)
        self.codex_approval_decision = decision


class CodexApprovalDenied(CodexApprovalError):
    pass


class CodexApprovalAborted(CodexApprovalError):
    pass


def _append_option(args: list[str], flag: str, value: str | None) -> None:
    if value:
        args.extend([flag, value])


def _build_codex_args(interactive_approvals: bool = False) -> tuple[list[str], list[str]]:
    global_args = shlex.split(os.environ.get("ARC_CODEX_GLOBAL_ARGS", ""))
    approval_policy = os.environ.get("ARC_CODEX_APPROVAL_POLICY", "never")
    if interactive_approvals:
        approval_policy = os.environ.get("ARC_CODEX_INTERACTIVE_APPROVAL_POLICY") or (
            approval_policy if approval_policy in {"on-request", "untrusted"} else "on-request"
        )
    _append_option(global_args, "--ask-for-approval", approval_policy)

    args = shlex.split(os.environ.get("ARC_CODEX_ARGS", "exec"))
    _append_option(args, "--model", os.environ.get("ARC_CODEX_MODEL"))
    _append_option(args, "--profile", os.environ.get("ARC_CODEX_PROFILE"))
    _append_option(args, "--sandbox", os.environ.get("ARC_CODEX_SANDBOX"))
    _append_option(args, "--cd", os.environ.get("ARC_CODEX_WORKDIR"))
    for item in shlex.split(os.environ.get("ARC_CODEX_CONFIG", "")):
        args.extend(["--config", item])
    if os.environ.get("ARC_CODEX_SKIP_GIT_REPO_CHECK", "true").lower() in {"1", "true", "yes", "on"}:
        args.append("--skip-git-repo-check")
    if interactive_approvals:
        args.append("--json")
    args.extend(shlex.split(os.environ.get("ARC_CODEX_EXTRA_ARGS", "")))

    return global_args, args


def _progress_from_event(event: dict) -> str | None:
    typ = event.get("type", "")
    item = event.get("item", {})

    if typ == "item.started":
        item_type = item.get("type", "")
        if item_type == "command_execution":
            command = str(item.get("command", "")).replace("\n", " ")
            return f"$ {command[:80]}"
        if item_type == "file_change":
            changes = item.get("changes", [])
            names = [Path(change.get("path", "")).name for change in changes[:3]]
            names = [name for name in names if name]
            return f"writing {', '.join(names)}" if names else "writing files"

    if typ == "item.completed":
        item_type = item.get("type", "")
        if item_type == "agent_message":
            text = " ".join(str(item.get("text", "")).split())
            return text[:160] if text else None
        if item_type == "file_change":
            changes = item.get("changes", [])
            names = [Path(change.get("path", "")).name for change in changes[:3]]
            names = [name for name in names if name]
            return f"wrote {', '.join(names)}" if names else "wrote files"
        if item_type == "command_execution":
            status = item.get("status", "")
            command = str(item.get("command", "")).replace("\n", " ")
            return f"{status}: {command[:70]}" if status else None

    if typ == "turn.completed":
        return "Codex finished"

    return None


def _preflight_workflow(workflow_path: Path) -> None:
    """Validate Codex output before registering it as an ARC artifact."""
    from arc.runtime.workflow_safety import (
        STRICT_ALLOWED_IMPORTS,
        check_workflow_source,
        validate_workflow_import_timeout,
    )

    source = workflow_path.read_text(encoding="utf-8")
    check_workflow_source(source, allowed_imports=_codex_allowed_imports(STRICT_ALLOWED_IMPORTS))
    module_name = f"arc_codex_preflight_{os.getpid()}_{abs(hash(str(workflow_path)))}"
    validate_workflow_import_timeout(source, module_name, str(workflow_path))


def _codex_allowed_imports(base: frozenset[str]) -> frozenset[str]:
    extra = frozenset(
        item.strip()
        for item in os.environ.get("ARC_CODEX_ALLOWED_IMPORTS", "").split(",")
        if item.strip()
    )
    return base | extra


def _schema_keys_from_yaml(sim2l_path: Path, section: str) -> list[str]:
    try:
        import yaml

        spec = yaml.safe_load(sim2l_path.read_text(encoding="utf-8")) or {}
        values = spec.get(section, {}) or {}
        return [str(key) for key in values.keys()]
    except Exception:
        return []


def _artifact_description(plan: ExperimentPlan, workflow_path: Path, sim2l_path: Path) -> str:
    """Build a useful persisted artifact description from the goal and generated schema."""
    objective = " ".join(plan.proposal.objective.split())
    methodology = " ".join(plan.proposal.methodology.split())
    outputs = _schema_keys_from_yaml(sim2l_path, "outputs")
    inputs = _schema_keys_from_yaml(sim2l_path, "inputs")

    parts = [objective[:180]]
    if inputs:
        parts.append("Inputs: " + ", ".join(inputs[:6]))
    if outputs:
        parts.append("Outputs: " + ", ".join(outputs[:8]))
    elif methodology:
        parts.append(methodology[:120])

    return " | ".join(part for part in parts if part)[:500]


class CodexCoderAgent(AgentContract):
    name = "coder"
    description = "Creates a Sim2L artifact by delegating code generation to the Codex CLI."

    async def run(self, input_data: ExperimentPlan) -> ArtifactDraft:
        plan = input_data if isinstance(input_data, ExperimentPlan) else ExperimentPlan(**input_data)
        workspace = self._workspace(plan)
        workspace.mkdir(parents=True, exist_ok=True)

        plan_json = json.dumps(plan.model_dump(), indent=2, default=str)
        from arc.runtime.workflow_safety import STRICT_ALLOWED_IMPORTS
        prompt = _PROMPT.format(
            allowed_imports=", ".join(sorted(_codex_allowed_imports(STRICT_ALLOWED_IMPORTS))),
            plan_json=plan_json,
        )
        (workspace / "PROMPT.md").write_text(prompt, encoding="utf-8")
        (workspace / "PLAN.json").write_text(plan_json, encoding="utf-8")

        command = os.environ.get("ARC_CODEX_COMMAND", "codex")
        if shutil.which(command) is None:
            raise RuntimeError(
                "Codex CLI not found. Install/configure Codex, or set ARC_CODEX_COMMAND "
                "to the executable used to run Codex."
            )

        permission_callback: PermissionCallback | None = self.context.config.get("permission_callback")
        progress_callback: ProgressCallback | None = self.context.config.get("progress_callback")
        timeout = float(os.environ.get("ARC_CODEX_TIMEOUT_SECONDS", "300"))

        if permission_callback:
            stdout, stderr = await self._run_with_approvals(
                command, workspace, prompt, permission_callback, progress_callback, timeout
            )
        else:
            allow_non_interactive = bool(
                self.context.config.get("allow_non_interactive")
                or env_flag("ARC_CODEX_ALLOW_NON_INTERACTIVE", False)
            )
            if not allow_non_interactive:
                raise RuntimeError(
                    "Codex approval relay is not configured. Provide a permission_callback "
                    "or set ARC_CODEX_ALLOW_NON_INTERACTIVE=true to run without user approval prompts."
                )
            stdout, stderr = await self._run_bypass(command, workspace, prompt, timeout)

        (workspace / "codex.stdout.txt").write_text(stdout or "", encoding="utf-8")
        (workspace / "codex.stderr.txt").write_text(stderr or "", encoding="utf-8")

        output = (stderr or stdout or "").strip()
        if "usage limit" in output.lower() or "purchase more credits" in output.lower():
            raise RuntimeError(
                "Codex usage limit reached. Purchase more credits at "
                "https://chatgpt.com/codex/settings/usage or try again later."
            )

        workflow_path = workspace / "workflow.py"
        sim2l_path = workspace / "sim2l.yaml"
        if not workflow_path.exists() or not sim2l_path.exists():
            raise RuntimeError(
                "Codex completed but did not create workflow.py and sim2l.yaml in "
                f"{workspace}"
            )
        try:
            _preflight_workflow(workflow_path)
        except Exception as exc:
            raise RuntimeError(f"Codex generated invalid workflow.py: {exc}") from exc

        global_args, args = _build_codex_args()
        description = _artifact_description(plan, workflow_path, sim2l_path)
        return ArtifactDraft(
            name=self._artifact_name(plan),
            description=description,
            files={
                "workflow.py": workflow_path.read_text(encoding="utf-8"),
                "sim2l.yaml": sim2l_path.read_text(encoding="utf-8"),
            },
            metadata={
                "created_by": self.name,
                "description": description,
                "coding_package": "arc-codex",
                "codex_command": command,
                "codex_args": global_args + args,
                "codex_workspace": str(workspace),
                "hypothesis": plan.proposal.hypothesis[:200],
                "methodology": plan.proposal.methodology[:500],
                "success_criteria": plan.success_criteria,
                "parameter_constraints": plan.parameter_constraints,
                "experimental_design": plan.experimental_design,
            },
        )

    async def _run_bypass(
        self,
        command: str,
        workspace: Path,
        prompt: str,
        timeout: float,
    ) -> tuple[str, str]:
        global_args, args = _build_codex_args(interactive_approvals=False)
        proc = await asyncio.create_subprocess_exec(
            command, *global_args, *args, prompt,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Codex timed out after {timeout}s")
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(
                f"Codex command failed with exit code {proc.returncode}: "
                f"{(stderr or stdout).strip()[:500]}"
            )
        return stdout, stderr

    async def _run_with_approvals(
        self,
        command: str,
        workspace: Path,
        prompt: str,
        callback: PermissionCallback,
        progress: ProgressCallback | None,
        timeout: float,
    ) -> tuple[str, str]:
        """Run Codex with --json and relay approval requests to the arc chat UI."""
        global_args, args = _build_codex_args(interactive_approvals=True)
        # Open the PTY first, then guard the *entire* run with a single
        # finally so the master fd cannot leak if create_subprocess_exec or
        # any subsequent step raises before we reach the post-run cleanup.
        stdin_master_fd, stdin_slave_fd = pty.openpty()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    command, *global_args, *args, prompt,
                    cwd=workspace,
                    stdin=stdin_slave_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            finally:
                # The child has its own copy of stdin_slave_fd now; close
                # ours so the slave side is fully owned by the subprocess.
                os.close(stdin_slave_fd)

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            approval_decisions: list[str] = []

            async def read_stdout() -> None:
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").rstrip("\n")
                    stdout_lines.append(line)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if progress:
                        msg = _progress_from_event(event)
                        if msg:
                            try:
                                progress(msg)
                            except Exception:
                                pass

                    if event.get("type") == "item/commandExecution/requestApproval":
                        params = event.get("params", event)
                        approval_id = params.get("approval_id", "")
                        command_str = params.get("command", params.get("shell_command", ""))
                        reason = params.get("reason", "")
                        try:
                            decision = await callback(approval_id, command_str, reason)
                        except Exception:
                            decision = "decline"
                        approval_decisions.append(decision)
                        response = json.dumps({
                            "method": "execCommandApproval",
                            "params": {
                                "approval_id": approval_id,
                                "decision": decision,
                            },
                        }) + "\n"
                        # The PTY may already be closed if Codex exited
                        # between emitting the request and our reply. Writing
                        # to a dead fd raises OSError; swallow it and stop
                        # reading rather than letting a clean decline turn
                        # into an unhandled crash — the decision is already
                        # recorded in approval_decisions above.
                        try:
                            os.write(stdin_master_fd, response.encode())
                        except OSError:
                            break

            async def read_stderr() -> None:
                while True:
                    raw = await proc.stderr.readline()
                    if not raw:
                        break
                    stderr_lines.append(raw.decode(errors="replace").rstrip("\n"))

            try:
                await asyncio.wait_for(
                    asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(f"Codex timed out after {timeout}s")

            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
            if "abort" in approval_decisions:
                raise CodexApprovalAborted("Codex run aborted by user approval decision.", "abort")
            if proc.returncode != 0:
                if any(decision == "decline" for decision in approval_decisions):
                    raise CodexApprovalDenied("Codex run stopped after user declined approval.", "decline")
                raise RuntimeError(
                    f"Codex command failed with exit code {proc.returncode}: "
                    f"{(stderr or stdout).strip()[:500]}"
                )
            return stdout, stderr
        finally:
            # Close the master fd no matter how we exited the run — including
            # the failure-before-subprocess-start path where the old code
            # leaked it.
            try:
                os.close(stdin_master_fd)
            except OSError:
                # Already closed (e.g. inherited or double-close); ignore.
                pass

    def _workspace(self, plan: ExperimentPlan) -> Path:
        session_id = self.context.session_id
        paths = session_paths(session_id)
        slug = self._artifact_name(plan)
        return Path(paths["artifacts"]).parent / "workspaces" / "arc-codex" / slug

    def _artifact_name(self, plan: ExperimentPlan) -> str:
        import re

        words = re.sub(r"[^a-z0-9 ]", " ", plan.proposal.objective.lower()).split()
        stop = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by", "via"}
        stem = "_".join([w for w in words if w not in stop][:4])[:40] or "codex_sim2l_artifact"
        digest = hashlib.sha256(
            json.dumps(plan.model_dump(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:8]
        return f"{stem}_{digest}"
