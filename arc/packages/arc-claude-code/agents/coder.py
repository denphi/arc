from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
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
- Only import from: math, cmath, itertools, functools, operator, statistics, random, decimal, fractions, collections, heapq, bisect, array, typing, abc, dataclasses, enum
- Do not import numpy, scipy, pandas, or any other external package
- Do not use dunder attributes such as __dict__, __class__, or obj.__method__
- Keep code deterministic

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

_PKG_DIR = Path(__file__).parent.parent
_SERVER_SCRIPT = str(_PKG_DIR / "permission_server_main.py")

PermissionCallback = Callable[[str, str, str, str], Awaitable[bool]]
"""async (request_id, tool_name, description, input_preview) -> allow"""

ProgressCallback = Callable[[str], None]
"""sync (message) -> None  — called with short human-readable status lines"""


def _append_option(args: list[str], flag: str, value: str | None) -> None:
    if value:
        args.extend([flag, value])


def _build_claude_code_args(mcp_config_path: str | None = None) -> list[str]:
    args = shlex.split(os.environ.get("ARC_CLAUDE_CODE_ARGS", "-p"))
    _append_option(args, "--model", os.environ.get("ARC_CLAUDE_CODE_MODEL"))
    _append_option(args, "--fallback-model", os.environ.get("ARC_CLAUDE_CODE_FALLBACK_MODEL"))
    # Always use stream-json so we can show progress; text output is reconstructed from events.
    args.extend(["--output-format", "stream-json", "--verbose", "--include-partial-messages"])
    _append_option(args, "--max-budget-usd", os.environ.get("ARC_CLAUDE_CODE_MAX_BUDGET_USD"))
    _append_option(args, "--effort", os.environ.get("ARC_CLAUDE_CODE_EFFORT"))
    _append_option(args, "--system-prompt", os.environ.get("ARC_CLAUDE_CODE_SYSTEM_PROMPT"))
    _append_option(args, "--append-system-prompt", os.environ.get("ARC_CLAUDE_CODE_APPEND_SYSTEM_PROMPT"))
    if os.environ.get("ARC_CLAUDE_CODE_ALLOWED_TOOLS"):
        args.extend(["--allowedTools", os.environ["ARC_CLAUDE_CODE_ALLOWED_TOOLS"]])
    if os.environ.get("ARC_CLAUDE_CODE_DISALLOWED_TOOLS"):
        args.extend(["--disallowedTools", os.environ["ARC_CLAUDE_CODE_DISALLOWED_TOOLS"]])
    args.extend(shlex.split(os.environ.get("ARC_CLAUDE_CODE_EXTRA_ARGS", "")))

    if mcp_config_path:
        args.extend(["--permission-mode", "default"])
        # --mcp-config is variadic. Use --flag=value so the positional prompt
        # appended later is not consumed as another config path.
        args.append(f"--mcp-config={mcp_config_path}")
        args.append("--strict-mcp-config")
    else:
        _append_option(
            args,
            "--permission-mode",
            os.environ.get("ARC_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions"),
        )

    return args


_DELTA_EMIT_EVERY = 120  # emit a progress snippet after this many new chars per block


def _progress_from_event(
    event: dict,
    delta_buffers: dict[int, str],
    delta_emitted: dict[int, int],
) -> str | None:
    """Return a short human-readable status string for notable stream-json events, or None.

    delta_buffers / delta_emitted track per-block-index streaming state across calls.
    """
    typ = event.get("type", "")

    if typ == "system":
        subtype = event.get("subtype")
        model = event.get("model")
        if subtype == "init":
            delta_buffers.clear()
            delta_emitted.clear()
            return f"started  {model}" if model else "started"
        return subtype or "system event"

    if typ == "user":
        return "sent prompt"

    # Partial message events from --include-partial-messages.
    if typ == "stream_event":
        ev = event.get("event", {})
        ev_type = ev.get("type", "")

        if ev_type == "message_start":
            delta_buffers.clear()
            delta_emitted.clear()
            return "generating…"

        if ev_type == "content_block_start":
            block = ev.get("content_block", {})
            block_type = block.get("type", "")
            idx = ev.get("index", 0)
            delta_buffers[idx] = ""
            delta_emitted[idx] = 0
            if block_type == "thinking":
                return "thinking…"
            if block_type == "text":
                return "responding…"
            if block_type == "tool_use":
                name = block.get("name", "")
                return f"calling {name}…"
            return None

        if ev_type == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta", {})
            delta_type = delta.get("type", "")
            text = delta.get("text") or delta.get("thinking") or ""
            if not text:
                return None
            buf = delta_buffers.get(idx, "") + text
            delta_buffers[idx] = buf
            emitted = delta_emitted.get(idx, 0)
            if len(buf) - emitted >= _DELTA_EMIT_EVERY:
                snippet = buf[emitted:emitted + _DELTA_EMIT_EVERY].strip()
                snippet = " ".join(snippet.split())[:100]
                delta_emitted[idx] = len(buf)
                if snippet:
                    prefix = "thinking: " if delta_type == "thinking_delta" else "writing: "
                    return f"{prefix}{snippet}…"
            return None

        return None

    if typ == "assistant":
        # Look for tool_use blocks inside the message content.
        text_parts: list[str] = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                inp  = block.get("input", {})
                if name == "Write":
                    return f"writing {Path(inp.get('file_path', '')).name}"
                if name == "Edit":
                    return f"editing {Path(inp.get('file_path', '')).name}"
                if name == "Bash":
                    cmd = inp.get("command", "")[:60]
                    return f"$ {cmd}"
                if name == "Read":
                    return f"reading {Path(inp.get('file_path', '')).name}"
                return f"tool: {name}"
            if block.get("type") == "text":
                text = " ".join(str(block.get("text", "")).split())
                if text:
                    text_parts.append(text)
        if text_parts:
            return " ".join(text_parts)[:160]

    if typ == "result":
        cost = event.get("total_cost_usd")
        turns = event.get("num_turns", "?")
        cost_str = f"  ${cost:.4f}" if cost else ""
        return f"done  {turns} turn(s){cost_str}"

    return None


def _schema_keys_from_yaml(sim2l_path: Path, section: str) -> list[str]:
    try:
        import yaml

        spec = yaml.safe_load(sim2l_path.read_text(encoding="utf-8")) or {}
        values = spec.get(section, {}) or {}
        return [str(key) for key in values.keys()]
    except Exception:
        return []


def _artifact_description(plan: ExperimentPlan, workflow_path: Path, sim2l_path: Path) -> str:
    objective = " ".join(plan.proposal.objective.split())
    methodology = " ".join(plan.proposal.methodology.split())
    inputs = _schema_keys_from_yaml(sim2l_path, "inputs")
    outputs = _schema_keys_from_yaml(sim2l_path, "outputs")

    parts = [objective[:180]]
    if inputs:
        parts.append("Inputs: " + ", ".join(inputs[:6]))
    if outputs:
        parts.append("Outputs: " + ", ".join(outputs[:8]))
    elif methodology:
        parts.append(methodology[:120])

    return " | ".join(part for part in parts if part)[:500]


class ClaudeCodeCoderAgent(AgentContract):
    name = "coder"
    description = "Creates a Sim2L artifact by delegating code generation to Claude Code."

    async def run(self, input_data: ExperimentPlan) -> ArtifactDraft:
        plan = input_data if isinstance(input_data, ExperimentPlan) else ExperimentPlan(**input_data)
        workspace = self._workspace(plan)
        workspace.mkdir(parents=True, exist_ok=True)

        plan_json = json.dumps(plan.model_dump(), indent=2, default=str)
        prompt = _PROMPT.format(plan_json=plan_json)
        (workspace / "PROMPT.md").write_text(prompt, encoding="utf-8")
        (workspace / "PLAN.json").write_text(plan_json, encoding="utf-8")

        command = os.environ.get("ARC_CLAUDE_CODE_COMMAND", "claude")
        if shutil.which(command) is None:
            raise RuntimeError(
                "Claude Code CLI not found. Install/configure Claude Code, or set "
                "ARC_CLAUDE_CODE_COMMAND to the executable used to run it."
            )

        permission_callback: PermissionCallback | None = self.context.config.get("permission_callback")
        progress_callback: ProgressCallback | None = self.context.config.get("progress_callback")
        timeout = float(os.environ.get("ARC_CLAUDE_CODE_TIMEOUT_SECONDS", "300"))

        if permission_callback:
            stdout, stderr = await self._run_with_permission_relay(
                command, workspace, prompt, permission_callback, progress_callback, timeout
            )
        else:
            allow_non_interactive = bool(
                self.context.config.get("allow_non_interactive")
                or env_flag("ARC_CLAUDE_CODE_ALLOW_NON_INTERACTIVE", False)
            )
            if not allow_non_interactive:
                raise RuntimeError(
                    "Claude Code permission relay is not configured. Provide a "
                    "permission_callback or set "
                    "ARC_CLAUDE_CODE_ALLOW_NON_INTERACTIVE=true to run without "
                    "user approval prompts."
                )
            stdout, stderr = await self._run_bypass(
                command, workspace, prompt, progress_callback, timeout
            )

        (workspace / "claude-code.stdout.txt").write_text(stdout or "", encoding="utf-8")
        (workspace / "claude-code.stderr.txt").write_text(stderr or "", encoding="utf-8")

        if stderr and ("usage limit" in stderr.lower() or "purchase more credits" in stderr.lower()):
            raise RuntimeError(
                "Claude Code usage limit reached. Purchase more credits at "
                "https://chatgpt.com/codex/settings/usage or try again later."
            )

        workflow_path = workspace / "workflow.py"
        sim2l_path = workspace / "sim2l.yaml"
        if not workflow_path.exists() or not sim2l_path.exists():
            raise RuntimeError(
                "Claude Code completed but did not create workflow.py and sim2l.yaml in "
                f"{workspace}. stdout: {(stdout or '').strip()[:300]}"
            )

        args = _build_claude_code_args()
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
                "coding_package": "arc-claude-code",
                "claude_code_command": command,
                "claude_code_args": args,
                "claude_code_workspace": str(workspace),
                # The instruction given to the external coding agent is the
                # Build step's audit artifact — the workspace copy
                # (PROMPT.md / claude-code.stdout.txt) is transient, so
                # persist a hash + excerpts with the artifact itself.
                "claude_code_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "claude_code_prompt_excerpt": prompt[:2000],
                "claude_code_output_tail": (stdout or "")[-2000:],
                "hypothesis": plan.proposal.hypothesis[:200],
                "methodology": plan.proposal.methodology[:500],
                "success_criteria": plan.success_criteria,
                "parameter_constraints": plan.parameter_constraints,
                "experimental_design": plan.experimental_design,
            },
        )

    async def _stream_stdout(
        self,
        proc: asyncio.subprocess.Process,
        progress: ProgressCallback | None,
    ) -> str:
        """Read stream-json lines from proc.stdout, emit progress, return raw text."""
        lines: list[str] = []
        delta_buffers: dict[int, str] = {}
        delta_emitted: dict[int, int] = {}
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\n")
            lines.append(line)
            if progress:
                try:
                    event = json.loads(line)
                    msg = _progress_from_event(event, delta_buffers, delta_emitted)
                    if msg:
                        progress(msg)
                except Exception:  # noqa: BLE001 — progress is best-effort
                    # A non-JSON line, or a fault in the progress callback,
                    # must never break stdout draining. (JSONDecodeError is a
                    # subclass of Exception — the previous tuple was redundant.)
                    pass
        return "\n".join(lines)

    async def _stream_stderr(
        self,
        proc: asyncio.subprocess.Process,
        progress: ProgressCallback | None,
    ) -> str:
        """Read stderr incrementally so CLI/auth errors are visible before exit."""
        chunks: list[str] = []
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\n")
            chunks.append(line)
            if progress and line:
                progress(f"stderr: {line[:160]}")
        return "\n".join(chunks)

    async def _run_bypass(
        self,
        command: str,
        workspace: Path,
        prompt: str,
        progress: ProgressCallback | None,
        timeout: float,
    ) -> tuple[str, str]:
        args = _build_claude_code_args()
        if progress:
            progress("Claude Code started")
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.write_eof()

        try:
            stdout, stderr_b, _ = await asyncio.wait_for(
                asyncio.gather(
                    self._stream_stdout(proc, progress),
                    self._stream_stderr(proc, progress),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Claude Code timed out after {timeout}s")

        stderr = stderr_b.decode(errors="replace") if isinstance(stderr_b, bytes) else str(stderr_b or "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code command failed with exit code {proc.returncode}: "
                f"{(stderr or stdout).strip()[:500]}"
            )
        return stdout, stderr

    async def _run_with_permission_relay(
        self,
        command: str,
        workspace: Path,
        prompt: str,
        callback: PermissionCallback,
        progress: ProgressCallback | None,
        timeout: float,
    ) -> tuple[str, str]:
        import uuid
        run_id = uuid.uuid4().hex[:8]
        tmp = Path(tempfile.gettempdir())
        # Keep paths short — Unix socket limit on macOS is ~104 chars.
        sock_path = str(tmp / f"arc-perm-{run_id}.sock")
        Path(sock_path).unlink(missing_ok=True)

        mcp_config = {
            "mcpServers": {
                "arc-permission-relay": {
                    "command": sys.executable,
                    "args": [_SERVER_SCRIPT, sock_path],
                }
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mcp.json", delete=False, dir=tmp
        ) as f:
            json.dump(mcp_config, f)
            mcp_config_path = f.name

        args = _build_claude_code_args(mcp_config_path=mcp_config_path)
        if progress:
            progress("Claude Code started with permission relay")

        proc = await asyncio.create_subprocess_exec(
            command, *args,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(prompt.encode())
        proc.stdin.write_eof()

        perm_task = asyncio.ensure_future(self._serve_permissions(sock_path, callback, progress))
        try:
            stdout, stderr_b, _ = await asyncio.wait_for(
                asyncio.gather(
                    self._stream_stdout(proc, progress),
                    self._stream_stderr(proc, progress),
                    proc.wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Claude Code timed out after {timeout}s")
        finally:
            perm_task.cancel()
            try:
                await perm_task
            except (asyncio.CancelledError, Exception):
                pass
            Path(mcp_config_path).unlink(missing_ok=True)
            Path(sock_path).unlink(missing_ok=True)

        stderr = stderr_b.decode(errors="replace") if isinstance(stderr_b, bytes) else str(stderr_b or "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code command failed with exit code {proc.returncode}: "
                f"{(stderr or stdout).strip()[:500]}"
            )
        return stdout, stderr

    async def _serve_permissions(
        self,
        sock_path: str,
        callback: PermissionCallback,
        progress: ProgressCallback | None = None,
    ) -> None:
        """
        Connect to the MCP server's Unix socket, read forwarded permission
        requests, call the user-facing callback, and write verdicts back.
        Runs until the socket closes (Claude Code is done).
        """
        # Wait for the server to create the socket.
        if progress:
            progress("waiting for permission relay")
        for _ in range(50):
            if Path(sock_path).exists():
                break
            await asyncio.sleep(0.1)

        last_exc: Exception | None = None
        for _ in range(20):
            try:
                reader, writer = await asyncio.open_unix_connection(sock_path)
                if progress:
                    progress("permission relay connected")
                break
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.1)
        else:
            if progress:
                progress(f"permission relay unavailable: {last_exc}")
            return  # server never came up — permissions will be auto-denied by server

        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                request_id    = params.get("request_id", "")
                tool_name     = params.get("tool_name", "")
                description   = params.get("description", "")
                input_preview = params.get("input_preview", "")

                try:
                    allow = await callback(request_id, tool_name, description, input_preview)
                except Exception:
                    allow = False

                writer.write((json.dumps({"allow": allow}) + "\n").encode())
                await writer.drain()
        except asyncio.CancelledError:
            pass  # proc finished — normal exit
        except Exception:
            pass
        finally:
            writer.close()

    def _workspace(self, plan: ExperimentPlan) -> Path:
        session_id = self.context.session_id
        paths = session_paths(session_id)
        slug = self._artifact_name(plan)
        return Path(paths["artifacts"]).parent / "workspaces" / "arc-claude-code" / slug

    def _artifact_name(self, plan: ExperimentPlan) -> str:
        import re

        words = re.sub(r"[^a-z0-9 ]", " ", plan.proposal.objective.lower()).split()
        stop = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by", "via"}
        stem = "_".join([w for w in words if w not in stop][:4])[:40] or "claude_code_sim2l_artifact"
        digest = hashlib.sha256(
            json.dumps(plan.model_dump(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:8]
        return f"{stem}_{digest}"
