"""ARC interactive chat REPL — core entry points and the chat_loop coroutine.

The chat package is split across focused submodules
(see :mod:`arc.chat` docstring for the map). This file holds:

  * :func:`main` — argparse entry point invoked by ``arc chat`` CLI.
  * :func:`chat_loop` — the async REPL itself.
  * :func:`run_research` — research pipeline orchestrator (pre-build
    leg + post-build :class:`Pipeline` invocation).
  * :func:`_run_with_continuation`, :func:`_post_approval_menu` —
    autonomous iteration loop + post-approval explore menu.
  * Dispatch handlers (`_handle_goal`, `_handle_refinement`,
    `_handle_set_target`) and the v2 router adapter
    (`_route_via_v2`).
  * Session persistence helpers (`_save_session`, `_restore_session`)
    and a few coder/package state helpers.

Most pure utilities (parsers, classifier, target-distance, ANSI
rendering, prompt-toolkit input) have been extracted into
sister modules; this file imports them at module top so call sites
inside ``chat_loop`` can use the legacy underscored names.
"""


import argparse
import asyncio
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# arc/ repo root is three levels up from this file (arc/arc/chat/loop.py → arc/)
_ARC_ROOT = Path(__file__).parent.parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_ARC_ROOT / ".env")
except Exception:
    pass

from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.research import ResearchGoal
from arc.session import (
    new_session_id, list_sessions,
    save_session_meta, load_session_meta,
    delete_session, delete_all_sessions,
)

# Phase 1: input helpers extracted into arc.chat.input.
# The legacy names are re-exported here so existing call sites and tests
# in this module keep resolving them at module scope.
from arc.chat.input import (
    chat_input,
    chat_input_async,
    _prompt_session,
    chat_history_path as _chat_history_path,
)


# Target-distance helpers — canonical home is arc.chat.research.targets.
# Re-exported under the legacy underscored names for callers in this
# file and in commands/optimize.py.
from arc.chat.research.targets import (
    pct_off as _pct_off,
    registry_keys_match as _registry_km,
)

# Hoisted leaf imports — these submodules have no cycle risk with
# loop.py and are read at the top so static analysis sees the real
# dependency graph.
from arc.chat._env import env_flag
from arc.chat.events import emit
from arc.chat.plan_mode import is_plan_mode


# ── ANSI colours / UI helpers (extracted to arc.chat.ui) ──────────────────
# Re-imported here so existing module-level references keep working.
from arc.chat.ui import (
    RESET, BOLD, DIM, CYAN, GREEN, YELLOW, RED, BLUE, GREY,
    c, header, step, ok, warn, err, hr,
)


def _make_permission_callback():
    """Return an async callback that surfaces Claude Code permission requests in the arc chat UI."""

    async def _callback(request_id: str, tool_name: str, description: str, input_preview: str) -> bool:
        print()
        print(f"  {c('◆ Claude Code permission request', BOLD, YELLOW)}")
        print(f"    {c('Tool:', DIM)}    {tool_name}")
        print(f"    {c('Action:', DIM)}  {description}")
        if input_preview:
            preview = input_preview[:200] + ("…" if len(input_preview) > 200 else "")
            print(f"    {c('Input:', DIM)}   {c(preview, DIM)}")
        raw = (await chat_input_async(c("    Allow? [y/N] ", BOLD))).strip().lower()
        allowed = raw in {"y", "yes"}
        if allowed:
            ok(f"Permitted  {c(tool_name, DIM)}")
        else:
            warn(f"Denied  {c(tool_name, DIM)}")
        return allowed

    return _callback


def _make_codex_approval_callback():
    """Return an async callback that surfaces Codex approval requests in the arc chat UI."""

    async def _callback(approval_id: str, command: str, reason: str) -> str:
        print()
        print(f"  {c('◆ Codex approval request', BOLD, YELLOW)}")
        print(f"    {c('Command:', DIM)}  {command}")
        if reason:
            print(f"    {c('Reason:', DIM)}   {reason}")
        print(f"    {c('y', BOLD)} allow  "
              f"  {c('a', BOLD)} allow for session  "
              f"  {c('n', BOLD)} deny  "
              f"  {c('q', BOLD)} abort")
        raw = (await chat_input_async(c("    Decision [y/a/n/q] ", BOLD))).strip().lower()
        decision_map = {
            "y": "accept",
            "yes": "accept",
            "a": "acceptForSession",
            "n": "decline",
            "no": "decline",
            "q": "abort",
        }
        decision = decision_map.get(raw, "decline")
        label = {"accept": "Allowed", "acceptForSession": "Allowed for session",
                 "decline": "Denied", "abort": "Aborted"}[decision]
        color = GREEN if decision.startswith("accept") else YELLOW
        print(f"  {c(label, color)}  {c(command[:80], DIM)}")
        return decision

    return _callback


def _make_codex_progress_callback():
    """Return a callback that surfaces Codex JSON progress in the arc chat UI."""

    def _callback(message: str) -> None:
        print(" " * 40, end="\r")
        print(f"  {c('codex', DIM)}  {message}")

    return _callback


def _make_claude_progress_callback():
    """Return a callback that surfaces Claude Code progress in the arc chat UI."""

    def _callback(message: str) -> None:
        print(" " * 40, end="\r")
        print(f"  {c('claude', DIM)}  {message}")

    return _callback


def _is_codex_approval_stop(exc: Exception) -> bool:
    return getattr(exc, "codex_approval_decision", None) in {"decline", "abort"}


_PLAN_ACCEPT = {"ok", "yes", "y", "accept", "looks good", "good", "done", "go", "proceed", ""}


async def _review_plan_with_user(
    planner,
    plan,
    target: dict,
    max_rounds: int = 5,
    required_outputs: list[str] | None = None,
):
    """Show the plan and loop until the user accepts or max_rounds reached.

    On the first round, warns if the plan's required_outputs don't cover target
    keys, and asks the user to confirm or redirect before building.

    Returns the (possibly revised) ExperimentPlan.
    """
    import asyncio

    # Derive which output key names the builder will be asked to produce.
    # These come from the target (canonical keys) + schema registry if available.
    required_outputs = list(dict.fromkeys(
        list(target.keys() if target else []) + list(required_outputs or [])
    ))

    for round_num in range(max_rounds):
        # Display current plan.
        print()
        print(f"  {c('Parameters', BOLD)}     {plan.parameters}")
        if getattr(plan, "parameter_constraints", None):
            print(f"  {c('Constraints', BOLD)}    {plan.parameter_constraints}")
        print(f"  {c('Sweep', BOLD)}          {plan.parameter_sweep}")
        if getattr(plan, "experimental_design", None):
            print(f"  {c('Design', BOLD)}")
            for item in plan.experimental_design:
                print(f"    - {item}")
        print(f"  {c('Criteria', BOLD)}       {plan.success_criteria}")
        if required_outputs:
            print(f"  {c('Must output', BOLD)}   {c(required_outputs, YELLOW)}")

        if round_num == 0:
            print(f"  {c('─' * 56, DIM)}")
            if required_outputs:
                print(f"  {c('The workflow MUST produce output keys:', DIM)} "
                      f"{c(str(required_outputs), YELLOW)}")
                print(f"  {c('Confirm this is correct, or tell me what outputs you need.', DIM)}")
            else:
                print(f"  {c('No target set — accept the plan or describe changes.', DIM)}")
            print(f"  {c('Type ok/yes/enter to accept, or describe what to change.', DIM)}")

        try:
            raw = (await chat_input_async(c("  plan> ", BOLD, CYAN))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if raw.lower() in _PLAN_ACCEPT:
            ok("Plan accepted.")
            break

        # User gave feedback — check if they're specifying output keys.
        # Pattern: "output must be X" / "must produce X, Y" → update required_outputs.
        import re as _re
        output_override = _re.findall(
            r'(?:output|produce|return|compute|calculate)\s+([a-z][a-z0-9_]*(?:\s*,\s*[a-z][a-z0-9_]*)*)',
            raw.lower()
        )
        if output_override:
            new_keys = [k.strip() for part in output_override for k in part.split(",") if k.strip()]
            if new_keys:
                required_outputs = new_keys
                warn(f"Required outputs updated: {required_outputs}")

        # Refine the plan with LLM.
        print(f"  {c('refining...', DIM)}", end="\r")
        feedback = raw
        if required_outputs:
            feedback += f"\n\nIMPORTANT: the simulation workflow MUST produce output keys: {required_outputs}"
        try:
            plan = await planner.refine(plan, feedback)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Refinement interrupted — keeping current plan.', YELLOW)}")
            break
        print(" " * 40, end="\r")
        ok(f"Plan revised  {c(f'(round {round_num + 1}/{max_rounds})', DIM)}")

    # Store confirmed required outputs on the plan so the builder can use them.
    if required_outputs:
        plan.proposal.variables = list(
            dict.fromkeys(list(plan.proposal.variables or []) + required_outputs)
        )
        if not hasattr(plan, '_required_outputs'):
            object.__setattr__(plan, '_required_outputs', required_outputs) if hasattr(plan, '__setattr__') else None
        try:
            plan._required_outputs = required_outputs
        except Exception:
            pass

    return plan


# ── Post-approval exploration menu ──────────────────────────────────────────

async def _post_approval_menu(workflow, artifact, result, target: dict):
    """Offer exploration options after the goal is approved."""
    import csv, io, asyncio

    ctx = workflow._context
    run_history = ctx.memory.get("run_history", [])
    plan = ctx.memory.get("current_plan")

    print()
    print(f"{c('●', BOLD, GREEN)} {c('Goal achieved! What would you like to do next?', BOLD)}")
    print(f"  {c('1', CYAN)}  Show all session runs ranked by fitness")
    print(f"  {c('2', CYAN)}  Run a parameter sensitivity sweep")
    print(f"  {c('3', CYAN)}  Export results to CSV")
    print(f"  {c('4', CYAN)}  Run with custom parameters  (key=val ...)")
    print(f"  {c('5', CYAN)}  Set a new goal")
    print(f"  {c('Enter', DIM)}  Continue to prompt")

    try:
        choice = (await chat_input_async(c("  explore> ", BOLD, GREEN))).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if choice == "1":
        # Rank all runs by distance to target.
        from arc.packages import load_reviewer
        _km = load_reviewer()._keys_match
        registry = ctx.memory.get("schema_registry", {})
        rows = []
        for r in run_history:
            outs = r.get("outputs", {})
            ins  = r.get("inputs", {})
            if target:
                errors = []
                for tk, tv in target.items():
                    for ok_k, ov in outs.items():
                        if _km(tk, ok_k) or (registry and _registry_km(tk, ok_k, registry)):
                            if isinstance(ov, (int, float)):
                                errors.append(abs(ov - tv) / max(abs(tv), 1e-12) * 100)
                            break
                fitness = sum(errors) / len(errors) if errors else float("inf")
            else:
                fitness = -sum(v for v in outs.values() if isinstance(v, (int, float)))
            rows.append((fitness, ins, outs))
        rows.sort(key=lambda x: x[0])
        print()
        header(f"Session runs ranked  {c(f'({len(rows)} total)', DIM)}")
        for i, (fit, ins, outs) in enumerate(rows[:10], 1):
            fit_str = f"{fit:.2f}%" if fit != float("inf") else "∞"
            print(f"  {c(f'{i:>2}.', DIM)} fit={c(fit_str, CYAN)}  inputs={ins}  outputs={outs}")

    elif choice == "2":
        # Sensitivity sweep over the artifact's parameter_sweep definition.
        sweep = plan.parameter_sweep if plan else {}
        if not sweep:
            warn("No parameter sweep defined — use /sweep or /exec to explore manually.")
            return
        header(f"Sensitivity sweep  {c(artifact.name, CYAN)}")
        try:
            for param, values in sweep.items():
                print(f"  {c(param, BOLD)}")
                for v in values:
                    execution = await workflow.adapter.run(artifact, {param: v})
                    workflow.results.save(execution)
                    outs = execution.outputs or {}
                    pct = _pct_off(outs, target, ctx.memory.get("schema_registry", {})) if target else ""
                    cols = "  ".join(f"{k}={val:.4g}" for k, val in outs.items() if isinstance(val, (int, float)))
                    status_c = GREEN if execution.status == "completed" else RED
                    print(f"    {c('●', status_c)} {param}={v}  {cols}  {c(pct, YELLOW)}")
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Sweep interrupted.', YELLOW)}")

    elif choice == "3":
        # Export run history to CSV string and print it.
        if not run_history:
            warn("No run history to export.")
            return
        all_keys = set()
        for r in run_history:
            all_keys.update(r.get("inputs", {}).keys())
            all_keys.update(r.get("outputs", {}).keys())
        fieldnames = sorted(all_keys)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in run_history:
            row = {**r.get("inputs", {}), **r.get("outputs", {})}
            writer.writerow(row)
        csv_text = buf.getvalue()
        # Save to file.
        import os
        from pathlib import Path
        out_path = Path(os.environ.get("SIM2L_HOME", Path.home() / ".sim2l" / "code")) \
                   / workflow.session_id / "results.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(csv_text)
        ok(f"Exported {len(run_history)} runs to {c(str(out_path), CYAN)}")
        print(f"  {c('Columns:', DIM)} {fieldnames}")

    elif choice == "4":
        # Custom params.
        raw = (await chat_input_async(c("  Params (key=val ...) > ", BOLD))).strip()
        params = {}
        for kv in raw.split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    params[k] = float(v)
                except ValueError:
                    params[k] = v
        if params:
            await run_artifact(workflow, artifact.artifact_id, params)
        else:
            warn("No parameters parsed. Use format: key=value key2=value2")

    elif choice == "5":
        # Signal caller to reset — set a flag the continuation loop can check.
        ctx.memory["_reset_goal"] = True

    # choice == "" or anything else → fall through to prompt


# ── Streaming workflow runner ────────────────────────────────────────────────

async def run_artifact(workflow: ResearchWorkflow, artifact_id: str, params: dict):
    """Directly run an already-registered artifact with given parameters."""
    # Try exact ID first, then match by name prefix.
    records = workflow.artifacts.list_all()
    artifact = None
    for r in records:
        if r.artifact_id == artifact_id or r.artifact_id.startswith(artifact_id) or r.name == artifact_id:
            artifact = r
            break

    if artifact is None:
        err(f"No artifact found matching '{artifact_id}'. Use /artifacts to list them.")
        return

    adapter_name = type(workflow.adapter).__name__
    header(f"Executing  {c(artifact.name, CYAN)}  {c(f'[{adapter_name}]', DIM)}")

    # Validate first.
    validation = await workflow.adapter.validate_artifact(artifact)
    if not validation.valid:
        for e in validation.errors:
            err(e)
        return

    inputs = await workflow.adapter.prepare_inputs(artifact, params)
    step("Inputs", inputs)
    execution = await workflow.adapter.run(artifact, inputs)
    workflow.results.save(execution)

    step("Run ID",  execution.run_id[:8] + "...")
    step("Status",  c(execution.status, GREEN if execution.status == "completed" else RED))
    step("Outputs", execution.outputs)
    if execution.metrics:
        step("Metrics", {k: v for k, v in execution.metrics.items()
                         if k not in ("squid_id",) and not isinstance(v, str)})
    for log_line in execution.logs:
        print(f"    {c(log_line, DIM)}")
    hr()
    print()


# Free-text parsers — canonical home is arc.chat.parsers.
# Legacy underscored names re-exported for backwards compatibility.
from arc.chat.parsers import (
    NOISE_WORDS as _NOISE,
    parse_target as _parse_target,
    parse_refinement_target as _parse_refinement_target,
    parse_target_command as _parse_target_command,
    refinement_needs_artifact_rebuild as _refinement_needs_artifact_rebuild,
    normalize_chat_command as _normalize_chat_command,
    tokens_for_relevance as _tokens_for_relevance,
    is_related_refinement as _is_related_refinement,
    build_refined_goal as _build_refined_goal,
)


# Session persistence — canonical home is arc.chat.session_io.
# Re-exported here under the legacy underscored names for callers.
from arc.chat.session_io import (
    save_session as _save_session,
    restore_session as _restore_session,
)


# The builder is now a strategy role (catalogue role ``builder``), so coder
# selection flows through the same resolver as every other role. We keep the
# loop's historical *backend label* (``builder`` / ``arc-codex:coder`` /
# ``arc-claude-code:coder``) as the public surface — the build step keys its
# permission/progress callbacks off that label — and map it to/from the
# catalogue key the resolver understands.
#
#   backend label          catalogue key
#   ---------------------  -------------
#   builder                default
#   arc-codex:coder        codex
#   arc-claude-code:coder  claude_code
_CODER_LABEL_TO_KEY = {
    "builder": "default",
    "arc-codex:coder": "codex",
    "arc-claude-code:coder": "claude_code",
}
_CODER_KEY_TO_LABEL = {v: k for k, v in _CODER_LABEL_TO_KEY.items()}


def _available_coding_backends(workflow: ResearchWorkflow) -> list[str]:
    backends = ["builder"]
    for package_name in workflow.registry.list_agent_sources("coder"):
        backends.append(f"{package_name}:coder")
    return backends


def _selected_coder(workflow: ResearchWorkflow) -> str:
    """Return the active coder backend *label*.

    Prefers the resolver's ``strategy_overrides["builder"]`` (the unified
    role-override store); falls back to the legacy
    ``agent_overrides["coder"]`` so sessions saved before the builder
    became a strategy role keep working.
    """
    memory = workflow._context.memory
    key = (memory.get("strategy_overrides") or {}).get("builder")
    if key:
        return _CODER_KEY_TO_LABEL.get(key, key)
    legacy = (memory.get("agent_overrides") or {}).get("coder")
    return legacy or "builder"


def _set_selected_coder(workflow: ResearchWorkflow, backend: str) -> str:
    aliases = {
        "builtin": "builder",
        "built-in": "builder",
        "sim2l": "builder",
        "default": "builder",
        "codex": "arc-codex:coder",
        "claude": "arc-claude-code:coder",
        "claude-code": "arc-claude-code:coder",
    }
    backend = aliases.get(backend, backend)
    if backend not in _available_coding_backends(workflow):
        raise ValueError(
            f"Unknown coder backend '{backend}'. Available: {', '.join(_available_coding_backends(workflow))}"
        )
    memory = workflow._context.memory
    strategy_overrides = memory.setdefault("strategy_overrides", {})
    agent_overrides = memory.setdefault("agent_overrides", {})
    key = _CODER_LABEL_TO_KEY.get(backend, backend)
    if backend == "builder":
        # Default: clear both stores so the resolver uses the catalogue default.
        strategy_overrides.pop("builder", None)
        agent_overrides.pop("coder", None)
    else:
        strategy_overrides["builder"] = key
        # Mirror into the legacy store so anything still reading it agrees.
        agent_overrides["coder"] = backend
    return backend


def _coder_agent_class(workflow: ResearchWorkflow):
    """Resolve the active coder to (backend_label, agent_class).

    Routes through the strategy resolver (``builder`` role) so coder
    selection honours the same precedence as every other role. When the
    session has an explicit choice (``/coder``, stored in
    ``strategy_overrides["builder"]`` or the legacy
    ``agent_overrides["coder"]``) that wins; otherwise the resolver's
    lower layers apply — ``ARC_STRATEGY_BUILDER`` → ``arc.toml
    [strategies] builder`` → catalogue default. Returns the loop's
    backend *label* alongside the class so the build step can wire
    backend-specific callbacks.
    """
    from arc.core.strategies import resolve_role as _core_resolve

    memory = workflow._context.memory
    session_key = (
        (memory.get("strategy_overrides") or {}).get("builder")
        or _CODER_LABEL_TO_KEY.get((memory.get("agent_overrides") or {}).get("coder"))
    )
    # Explicit session choice → force it. No session choice → let the
    # resolver consult env + arc.toml (don't pin to a key here).
    overrides = {"builder": session_key} if session_key else None
    try:
        from arc.core.config import load_arc_toml
        _path, config = load_arc_toml()
    except Exception:  # noqa: BLE001 — arc.toml is optional
        config = {}
    try:
        cls = _core_resolve("builder", overrides=overrides, config=config)
    except Exception:  # noqa: BLE001 — never let coder resolution break the build
        from arc.packages import load_builder
        return "builder", load_builder().Sim2LBuilderAgent

    # Map the resolved class back to the loop's backend label so the build
    # step's callback wiring (which keys off the label) matches the class
    # actually chosen — including when env/arc.toml selected it.
    label = _label_for_class(cls, session_key)
    return label, cls


def _label_for_class(cls, session_key: str | None) -> str:
    """Best-effort backend label for a resolved builder class.

    Maps the known coder classes to their loop labels; falls back to the
    session-derived label, then ``builder``.
    """
    by_class = {
        "CodexCoderAgent": "arc-codex:coder",
        "ClaudeCodeCoderAgent": "arc-claude-code:coder",
        "Sim2LBuilderAgent": "builder",
    }
    label = by_class.get(cls.__name__)
    if label:
        return label
    if session_key:
        return _CODER_KEY_TO_LABEL.get(session_key, "builder")
    return "builder"


async def _register_artifact_with_sim2l(workflow: ResearchWorkflow, artifact) -> dict | None:
    """Publish a built artifact via the workflow's backend.

    Routes through ``workflow.backend`` (see arc/runtime/backend.py).
    When the backend is the silent no-op — i.e. sim2l isn't active —
    this returns ``None`` so the caller skips the registration UI
    entirely. ARC then runs fully local with no shared catalog push and
    no warnings. The sim2l backend (active when sim2l is importable +
    the adapter supports it) deploys to the catalog as before.

    The name is kept for backwards-compat with existing call sites; the
    function is no longer sim2l-specific.
    """
    # Plan-mode gate: don't publish anything in --plan mode.
    if is_plan_mode():
        emit("info", "[plan-mode] skipping artifact registration",
             label="register")
        return {"registered": False, "error": "plan mode — registration skipped"}

    backend = getattr(workflow, "backend", None)
    if backend is None or not backend.is_active():
        # No active publish backend → fully local, skip silently.
        return None

    from arc.runtime.backend import safe_backend_action
    return await safe_backend_action(backend, "register_artifact", artifact)


def _registration_success_parts(registration: dict, artifact) -> tuple[str, str, bool]:
    """Return display label, detail, and whether to show Sim2L catalog advice."""
    backend_name = str(registration.get("backend") or "sim2l").lower()
    if backend_name == "github":
        detail = " / ".join(
            str(part) for part in (
                registration.get("repo"),
                registration.get("path"),
            ) if part
        )
        return "GitHub published", detail or artifact.name, False
    if backend_name == "sim2l":
        name = registration.get("sim_name", artifact.name)
        version = registration.get("sim_version", artifact.version)
        return "Sim2L registered", f"{name}/{version}", not registration.get("catalog_persisted", True)
    return f"{backend_name} registered", artifact.name, False


def _registration_failure_label(registration: dict) -> str:
    backend_name = str(registration.get("backend") or "sim2l").lower()
    if backend_name == "github":
        return "GitHub"
    if backend_name == "sim2l":
        return "Sim2L"
    return backend_name


def _set_session_package_state(workflow: ResearchWorkflow, package_name: str, enabled: bool) -> None:
    state = workflow._context.memory.setdefault("packages", {})
    enabled_set = set(state.get("enabled", []))
    disabled_set = set(state.get("disabled", []))
    if enabled:
        enabled_set.add(package_name)
        disabled_set.discard(package_name)
    else:
        disabled_set.add(package_name)
        enabled_set.discard(package_name)
        overrides = workflow._context.memory.setdefault("agent_overrides", {})
        for role, backend in list(overrides.items()):
            if str(backend).startswith(f"{package_name}:"):
                overrides.pop(role, None)
    state["enabled"] = sorted(enabled_set)
    state["disabled"] = sorted(disabled_set)




# Intent classifier — canonical home is arc.chat.classifier. Legacy
# underscored names re-exported here so existing call sites keep working.
from arc.chat.classifier import (
    QUESTION_STARTERS as _QUESTION_STARTERS,
    RESEARCH_STARTERS as _RESEARCH_STARTERS,
    CONVERSATIONAL as _CONVERSATIONAL,
    INTENT_SYSTEM_PROMPT as _INTENT_SYSTEM,
    is_question as _is_question,
    llm_classify_intent,
)


async def _llm_classify_intent(provider, text, has_active_goal):
    """Legacy positional adapter — the new helper uses a keyword-only
    ``has_active_goal``. Some tests still call this with the positional
    form, so we keep the legacy signature here.
    """
    return await llm_classify_intent(
        provider, text, has_active_goal=has_active_goal
    )


async def _answer_question(workflow: ResearchWorkflow, question: str) -> None:
    """Answer a conversational question directly using the LLM, without
    launching the research pipeline."""
    provider = workflow.provider
    if provider is None:
        # Stub mode — give a helpful static reply
        print(f"  {c('(stub mode — no LLM)', DIM)}")
        print(f"  {c('Use /run <goal> to start a research workflow, or /help for commands.', DIM)}")
        return

    ctx = workflow._context
    artifact = ctx.memory.get("current_artifact")
    goal = ctx.memory.get("primary_goal", "")

    context_block = ""
    if goal:
        context_block += f"\nCurrent research goal: {goal}"
    if artifact:
        context_block += (
            f"\nCurrent artifact: {artifact.name} (id {artifact.artifact_id[:8]})"
            f"\n  Inputs:  {list((artifact.metadata.get('sim2l_inputs') or {}).keys())}"
            f"\n  Outputs: {list((artifact.metadata.get('sim2l_outputs') or {}).keys())}"
        )
    run_history = ctx.memory.get("run_history", [])
    if run_history:
        context_block += f"\nRuns so far: {len(run_history)}"

    system = (
        "You are ARC, an autonomous research assistant for computational science "
        "and simulation. Answer the user's question concisely and helpfully. "
        "If they are asking about ARC commands, refer them to /help. "
        "Do not launch any research workflow — just answer the question."
        + (f"\n\nSession context:{context_block}" if context_block else "")
    )

    try:
        reply = await provider.complete(question, system=system)
        # Print with a subtle arc> prefix so it's visually distinct from user input
        print(f"\n  {c('arc>', BOLD + CYAN)} ", end="")
        # Word-wrap to 72 chars, preserving the indent
        wrapped = textwrap.fill(reply.strip(), width=72, subsequent_indent="       ")
        print(wrapped)
        print()
    except Exception as exc:
        err(f"Could not answer question: {exc}")


async def run_research(
    workflow: ResearchWorkflow,
    goal_text: str,
    domain: str = None,
    artifact: "ArtifactRecord | None" = None,
    refinement: str | None = None,
):
    from arc.schemas.artifact import ArtifactRecord as _AR  # noqa: F401

    from arc.packages import resolve_role

    ctx = workflow._context

    IdeatorAgent   = resolve_role("ideator",   workflow)
    PlannerAgent   = resolve_role("planner",   workflow)
    ReviewerAgent  = resolve_role("reviewer",  workflow)
    ReflectorAgent = resolve_role("reflector", workflow)
    CuratorAgent   = resolve_role("curator",   workflow)

    def agent(cls):
        return cls(context=ctx)

    # If this is a refinement of the primary goal, merge it into the goal text.
    if refinement:
        refinements = ctx.memory.get("refinements", [])
        refinements.append(refinement)
        ctx.memory["refinements"] = refinements
        goal_text = _build_refined_goal(ctx.memory.get("primary_goal", goal_text), refinements)

    if refinement:
        target = _parse_refinement_target(refinement) or ctx.memory.get("target", {})
    else:
        target = _parse_target(goal_text) or ctx.memory.get("target", {})
    goal = ResearchGoal(
        goal=goal_text,
        domain=domain or "computational science",
        target=target,
    )
    if target:
        ctx.memory["target"] = target

    preserved_output_keys: list[str] = []
    rebuild_for_refinement = bool(refinement and artifact and _refinement_needs_artifact_rebuild(refinement))
    if rebuild_for_refinement:
        preserved_output_keys = list((artifact.metadata.get("sim2l_outputs", {}) or {}).keys())
        if preserved_output_keys:
            ctx.memory["required_outputs"] = preserved_output_keys
        ctx.memory.pop("current_artifact", None)
        ctx.memory.pop("current_plan", None)
        artifact = None
        warn("Refinement affects artifact logic — rebuilding workflow.")

    is_new_artifact = artifact is None

    if is_new_artifact:
        # ── Ideation ──────────────────────────────────────────────────────
        header("Ideation")
        print(f"  {c('searching catalog + thinking...', DIM)}", end="\r")
        proposal = await agent(IdeatorAgent).run(goal)
        print(" " * 40, end="\r")
        step("Hypothesis", textwrap.fill(proposal.hypothesis, 60, subsequent_indent=" " * 16))
        step("Objective",  textwrap.fill(proposal.objective,  60, subsequent_indent=" " * 16))

        # ── Recipe auto-suggest ──────────────────────────────────────────
        await _maybe_suggest_recipe(workflow, goal, ctx)

        # ── Catalog reuse check ───────────────────────────────────────────
        catalog_hits = ctx.memory.pop("catalog_hits", [])
        prior_results = ctx.memory.pop("catalog_prior_results", [])
        catalog_artifact = None

        if catalog_hits and not rebuild_for_refinement:
            best = catalog_hits[0]
            best_name = best.get("name", "")
            best_desc = (best.get("description") or "")[:80]
            step("Catalog hit", f"{c(best_name, CYAN)}  {c(best_desc, DIM)}")
            if prior_results:
                step("Prior runs", f"{len(prior_results)} result(s) found")
                for r in prior_results[:2]:
                    print(f"    {c('inputs', DIM)}={r.get('input_params', {})}  "
                          f"{c('outputs', DIM)}={r.get('output_params', {})}")

            # Ask user whether to reuse the catalog artifact.
            raw = (await chat_input_async(
                c(f"  Reuse catalog artifact '{best_name}'? [Y / n] > ", BOLD)
            )).strip().lower()
            if raw not in ("n", "no"):
                # Wrap the catalog hit as a local artifact so the rest of the
                # pipeline (validate → execute → review) works unchanged.
                from arc.schemas.artifact import ArtifactDraft
                input_schema  = best.get("input_schema") or {}
                output_schema = best.get("output_schema") or {}
                # Build default parameters from schema defaults.
                default_params = {
                    k: v.get("default", 1.0) if isinstance(v, dict) else 1.0
                    for k, v in input_schema.items()
                }
                catalog_draft = ArtifactDraft(
                    name=best_name,
                    description=best_desc,
                    files={},          # no local files — will be fetched via sim2l repo
                    metadata={
                        "created_by": "catalog",
                        "strategy": "reuse_catalog",
                        "hypothesis": proposal.hypothesis,
                        "success_criteria": "",
                        "sim2l_inputs": {
                            k: {"type": "Number",
                                "default": v.get("default", 1.0) if isinstance(v, dict) else 1.0,
                                "description": k}
                            for k, v in input_schema.items()
                        },
                        "sim2l_outputs": {
                            k: {"type": "Number", "description": k}
                            for k in output_schema
                        },
                        "catalog_id": best.get("id"),
                        "workflow_source": best.get("metadata", {}).get("workflow_source", ""),
                    },
                )
                catalog_artifact = workflow.artifacts.register(catalog_draft)
                ok(f"Reusing catalog artifact  {c(best_name, CYAN)}")
                if prior_results:
                    ok(f"Prior results available: "
                       f"{c(str(len(prior_results)) + ' runs', DIM)}")

                # Seed next_parameters from prior results if we have a target.
                if target and prior_results:
                    best_prior = min(
                        prior_results,
                        key=lambda r: sum(
                            abs(r.get("output_params", {}).get(ok_k, float("inf")) - tv)
                            for ok_k, tv in target.items()
                            if ok_k in r.get("output_params", {})
                        ),
                    )
                    seed_params = {
                        k: v for k, v in best_prior.get("input_params", {}).items()
                        if k in input_schema
                    }
                    if seed_params:
                        ctx.memory["next_parameters"] = seed_params
                        step("Seed params", c(str(seed_params), YELLOW))

                ctx.memory["current_artifact"] = catalog_artifact
                ctx.memory["current_plan"] = None
                artifact = catalog_artifact
                # Skip build — jump straight to validation.
                is_new_artifact = False  # prevents double-validation below

        if catalog_artifact is None:
            # ── Planning ──────────────────────────────────────────────────
            header("Planning")
            print(f"  {c('thinking...', DIM)}", end="\r")
            plan = await agent(PlannerAgent).run(proposal)
            print(" " * 40, end="\r")
            if target:
                step("Target", target)
            plan = await _review_plan_with_user(
                agent(PlannerAgent),
                plan,
                target=target,
                required_outputs=preserved_output_keys,
            )
            # Inject confirmed required outputs into context for the builder.
            required_outputs = getattr(plan, '_required_outputs', list(target.keys()) if target else [])
            if required_outputs:
                ctx.memory["required_outputs"] = required_outputs

            # ── Building artifact ──────────────────────────────────────────
            coder_backend, CoderAgent = _coder_agent_class(workflow)
            header(f"Building artifact  {c(f'[{coder_backend}]', DIM)}")
            print(f"  {c('generating workflow...', DIM)}", end="\r")
            try:
                _coder_agent = CoderAgent(context=ctx)
                if "arc-codex" in coder_backend:
                    _coder_agent.context.config["permission_callback"] = _make_codex_approval_callback()
                    _coder_agent.context.config["progress_callback"] = _make_codex_progress_callback()
                elif "arc-claude-code" in coder_backend:
                    _coder_agent.context.config["permission_callback"] = _make_permission_callback()
                    _coder_agent.context.config["progress_callback"] = _make_claude_progress_callback()
                draft = await _coder_agent.run(plan)
            except Exception as _coder_exc:
                print(" " * 40, end="\r")
                if _is_codex_approval_stop(_coder_exc):
                    raise
                if coder_backend != "builder":
                    warn(f"Coder [{coder_backend}] failed: {_coder_exc}")
                    warn("Falling back to built-in builder.")
                    from arc.packages import load_builder as _load_builder
                    _FallbackAgent = _load_builder().Sim2LBuilderAgent
                    header(f"Building artifact  {c('[builder]', DIM)}")
                    print(f"  {c('generating workflow...', DIM)}", end="\r")
                    draft = await agent(_FallbackAgent).run(plan)
                else:
                    raise
            print(" " * 40, end="\r")
            artifact = workflow.artifacts.register(draft)
            ok(f"Artifact registered  {c(artifact.artifact_id[:8] + '...', DIM)}")
            ok(f"Path: {c(artifact.path, DIM)}")

            # ── Curation ──────────────────────────────────────────────────
            print(f"  {c('curating schema...', DIM)}", end="\r")
            artifact = await agent(CuratorAgent).run(artifact)
            print(" " * 40, end="\r")
            registry = ctx.memory.get("schema_registry", {})
            if registry:
                ok(f"Schema registry: {c(list(registry.keys()), DIM)}")

            sim2l_registration = await _register_artifact_with_sim2l(workflow, artifact)
            if sim2l_registration and sim2l_registration.get("registered"):
                label, detail, show_catalog_warning = _registration_success_parts(
                    sim2l_registration, artifact,
                )
                ok(f"{label}  {c(detail, DIM)}")
                if show_catalog_warning:
                    print(f"  {c('ℹ', CYAN)} Catalog service not reachable — artifact is saved locally only.")
            elif sim2l_registration:
                error_msg = sim2l_registration.get("error", "unknown error")
                backend_label = _registration_failure_label(sim2l_registration)
                _conn_errors = ("connection refused", "max retries", "newconnectionerror", "failed to establish")
                if backend_label == "Sim2L" and any(e in error_msg.lower() for e in _conn_errors):
                    print(f"  {c('ℹ', CYAN)} Sim2L services are not running — artifact saved to local ARC session only.")
                    print(f"  {c('  Start sim2l services to enable catalog/results sync.', DIM)}")
                else:
                    warn(f"{backend_label} registration skipped: {error_msg}")

            # Save for later iterations
            ctx.memory["current_artifact"] = artifact
            ctx.memory["current_plan"] = plan
    else:
        # ── Reuse existing artifact, optionally re-plan for a refinement ───
        plan = ctx.memory.get("current_plan")

        if refinement and plan is None:
            # Refinement with no current plan — re-plan against the updated goal.
            header("Re-planning  (refinement)")
            print(f"  {c('Refinement:', YELLOW)} {refinement}")
            print(f"  {c('thinking...', DIM)}", end="\r")
            from arc.packages import load_planner as _lp
            _PlannerCls = _lp().PlannerAgent
            from arc.schemas.research import ResearchProposal
            # Build a minimal proposal from context + current artifact to re-plan.
            _proposal = ResearchProposal(
                hypothesis=artifact.metadata.get("hypothesis", goal_text),
                objective=goal_text,
                variables=list(artifact.metadata.get("sim2l_inputs", {}).keys()),
                methodology=artifact.metadata.get(
                    "methodology",
                    "Reuse the current Sim2L artifact and adjust its input parameters.",
                ),
                expected_outcomes=(
                    "Refined outputs should move closer to the requested target."
                    if target else
                    "Refined outputs should remain valid and scientifically interpretable."
                ),
                evaluation_metrics=list(artifact.metadata.get("sim2l_outputs", {}).keys())
                or list(target.keys())
                or ["outputs"],
            )
            plan = await agent(_PlannerCls).run(_proposal)
            print(" " * 40, end="\r")
            plan = await _review_plan_with_user(agent(_PlannerCls), plan, target=target)
            ctx.memory["current_plan"] = plan
            required_outputs = getattr(plan, '_required_outputs', list(target.keys()) if target else [])
            if required_outputs:
                ctx.memory["required_outputs"] = required_outputs

        next_params = ctx.memory.pop("next_parameters", {})

        # Strip keys that don't belong to the artifact's input schema
        # (e.g. duration_seconds, squid_id that sneak in via metrics).
        if next_params and artifact is not None:
            schema_keys = set(artifact.metadata.get("sim2l_inputs", {}).keys())
            if schema_keys:
                next_params = {k: v for k, v in next_params.items() if k in schema_keys}

        if next_params:
            header(f"Iteration {ctx.iteration + 1}  {c('(reusing artifact, new params)', DIM)}")
            step("Next params", next_params)
            run_inputs = next_params
        else:
            header(f"Iteration {ctx.iteration + 1}  {c('(reusing artifact, same params)', DIM)}")
            run_inputs = plan.parameters if plan else {}

    # ── Post-build pipeline: validation → execution → review → reflect → record
    # Wraps the legacy inline code in the Phase 3 ``POST_BUILD_PHASES``.
    # The pipeline runs each phase, dispatches hooks, and aborts cleanly
    # when validation fails.
    from arc.chat.research.pipeline import PipelineState, Pipeline
    from arc.chat.research.phases import POST_BUILD_PHASES
    from arc.chat.research.hooks import phase_events

    pstate = PipelineState(
        workflow=workflow,
        goal_text=goal_text,
        domain=domain,
        artifact=artifact,
        refinement=refinement,
        target=target,
        plan=plan if is_new_artifact else ctx.memory.get("current_plan"),
        is_new_artifact=is_new_artifact,
    )
    if not is_new_artifact:
        # Legacy: ``run_inputs`` is computed earlier on the reuse branch.
        pstate.extras["run_inputs"] = run_inputs

    pipe = Pipeline(POST_BUILD_PHASES, hooks=phase_events())
    pstate = await pipe.run(pstate)

    if pstate.aborted:
        print()
        return None

    # Drain any catalog/results push errors the adapter accumulated
    # during this run and surface them once. Previously these were
    # swallowed at logger.debug, leaving the user thinking persistence
    # worked when it didn't.
    push_errors = getattr(workflow.adapter, "last_push_errors", None)
    if push_errors:
        warn(
            f"{len(push_errors)} sim2l push(es) failed this iteration — "
            "results aren't saved to the catalog/results services."
        )
        for label, msg in push_errors[:5]:  # cap to avoid wall-of-text
            print(f"    {c('•', YELLOW)} {c(f'{label}: {msg}', DIM)}")
        if len(push_errors) > 5:
            print(f"    {c(f'(+{len(push_errors) - 5} more)', DIM)}")
        # Reset for the next iteration so we don't double-report.
        workflow.adapter.last_push_errors = []

    _save_session(workflow, goal_text)
    hr()
    print()
    return {
        "artifact": pstate.artifact,
        "execution": pstate.execution,
        "review": pstate.review,
        "reflection": pstate.reflection,
        # Surfaced so the continuation loop can break out and trigger
        # an artifact rebuild when the current artifact's outputs don't
        # cover the user's target keys.
        "unmatched_target_keys": list(pstate.unmatched_target_keys),
    }


# ── Autonomous continuation loop ─────────────────────────────────────────────

async def _run_with_continuation(
    workflow: ResearchWorkflow,
    goal_text: str,
    max_iterations: int,
    start_artifact=None,
    refinement: str | None = None,
):
    """Run the first iteration, ask the user once to confirm the continuation
    budget, then loop autonomously until approved / budget exhausted / stopped.

    First-run confirmation prompt:
      - Enter / y / yes      → run up to max_iterations more steps
      - n / no               → stop after the first result
      - a number N           → run up to N more steps
      - "explore" / "ga"     → switch immediately to GA mode
      - "explore N" / "ga N" → GA with N generations

    Mid-run (only when reviewer is uncertain and has no next_parameters):
      Shows a mini-survey with 4 choices.  Otherwise runs silently.
    """
    ctx = workflow._context
    artifact = start_artifact
    iterations_done = 0
    # Budget confirmed by user after first iteration; None = not yet asked
    confirmed_budget: int | None = None

    # Only pass refinement on the first iteration; subsequent iterations reuse artifact.
    _first_refinement = refinement

    while True:
        result = await run_research(workflow, goal_text, artifact=artifact, refinement=_first_refinement)
        _first_refinement = None  # consumed after first call
        iterations_done += 1

        if result is None:
            break  # validation failed

        review = result["review"]
        execution = result["execution"]
        artifact = result["artifact"]  # reuse on next pass

        if execution.status != "completed":
            warn("Execution failed; stopping continuation so the artifact can be repaired before optimization.")
            break

        # ── Auto-rebuild on schema mismatch ───────────────────────────────
        # The reviewer can't approve when target keys don't appear in the
        # artifact's outputs. Detect that here and schedule an artifact
        # rebuild whose builder is told exactly which keys are missing.
        unmatched = result.get("unmatched_target_keys", []) or []
        if unmatched:
            warn(
                f"Schema mismatch detected — artifact outputs {list(execution.outputs.keys())} "
                f"don't include {unmatched}. Rebuilding the artifact."
            )
            # Tell the builder which output keys are required next time.
            existing = ctx.memory.get("required_outputs", []) or []
            ctx.memory["required_outputs"] = list(dict.fromkeys(list(existing) + list(unmatched)))
            # Force the next iteration to start fresh: no artifact, no plan.
            ctx.memory.pop("current_artifact", None)
            ctx.memory.pop("current_plan", None)
            artifact = None
            # Budget the rebuild attempt against the same max_iterations.
            if iterations_done >= max_iterations:
                warn(f"Reached iteration limit ({max_iterations}) before rebuild could complete.")
                break
            # Loop again — run_research will see artifact=None and ideate
            # a new one with required_outputs guiding the build.
            continue

        if review.approved:
            ok(c("Goal achieved — approved by reviewer.", GREEN, BOLD))
            target = ctx.memory.get("target", {})
            await _post_approval_menu(workflow, artifact, result, target)
            break

        if review.iteration_complete or review.strategy == "stop":
            print(c("  Reviewer: no further improvement possible.", DIM))
            break

        # ── Compute next_p and strategy once ──────────────────────────────
        strategy = review.strategy or "step"
        next_p = dict(review.next_parameters or {})
        schema_keys = set(artifact.metadata.get("sim2l_inputs", {}).keys())
        if schema_keys:
            next_p = {k: v for k, v in next_p.items() if k in schema_keys}

        # ── First-run: ask user once to confirm the continuation budget ────
        if confirmed_budget is None:
            remaining_display = max_iterations
            if strategy == "explore":
                print(f"\n{c('●', BOLD, CYAN)} {c('Reviewer suggests:', BOLD)} broad GA exploration")
                print(f"   {c(review.summary, DIM)}")
                print(f"   {c(f'Default: 10 generations × pop 8  |  or type N to change  |  n to stop', DIM)}")
                raw = (await chat_input_async(c("  Confirm? [Y / N / <gens> <pop>] > ", BOLD))).strip()
            else:
                print(f"\n{c('●', BOLD, CYAN)} {c('Reviewer suggests:', BOLD)} continue iterating")
                print(f"   {c(review.summary, DIM)}")
                if next_p:
                    print(f"   Next params: {c(str(next_p), YELLOW)}")
                print(f"   {c(f'Will run up to {remaining_display} more iterations automatically', DIM)}")
                raw = (await chat_input_async(c("  Confirm? [Y / N / <iterations> / explore] > ", BOLD))).strip()

            rl = raw.lower()
            if rl in ("n", "no"):
                print(c("  Stopping.", DIM))
                break
            elif rl in ("explore", "ga") or (strategy == "explore" and rl in ("y", "yes", "")):
                strategy = "explore"
                confirmed_budget = 0  # GA handles its own budget
            elif rl.split() and rl.split()[0].isdigit():
                parts = rl.split()
                if strategy == "explore":
                    confirmed_budget = 0
                    # Treat as gens [pop] override — handled below in explore block
                else:
                    confirmed_budget = int(parts[0])
                    max_iterations = iterations_done + confirmed_budget
            else:
                # y / yes / Enter → accept defaults
                confirmed_budget = max_iterations
                if strategy == "explore":
                    confirmed_budget = 0

        else:
            # Subsequent iterations — only pause for a mini-survey when the
            # reviewer is stuck (step strategy but no next_parameters).
            if strategy == "step" and not next_p:
                print(f"\n{c('●', BOLD, YELLOW)} {c('Reviewer uncertain — what next?', BOLD)}")
                print(f"   {c(review.summary, DIM)}")
                print(f"    {c('1', CYAN)} Same parameters again")
                print(f"    {c('2', CYAN)} Switch to genetic algorithm")
                print(f"    {c('3', CYAN)} Stop")
                print(f"    {c('4', CYAN)} Custom params  (key=val ...)")
                raw = (await chat_input_async(c("  Choice > ", BOLD))).strip()
                if raw == "2":
                    strategy = "explore"
                elif raw == "3" or raw.lower() in ("n", "no"):
                    break
                elif raw == "4" or "=" in raw:
                    for kv in raw.lstrip("4").strip().split():
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            try:
                                next_p[k] = float(v)
                            except ValueError:
                                pass
                # 1 / Enter → fall through, reuse params

        # Enforce budget (GA has its own)
        if strategy != "explore" and iterations_done >= max_iterations:
            warn(f"Reached iteration limit ({max_iterations}).")
            break

        # ── Execute strategy ───────────────────────────────────────────────
        if strategy == "explore":
            gens, pop = 10, 8
            # Allow override from first-run raw input
            if confirmed_budget == 0 and 'raw' in dir():
                parts = raw.split()
                if len(parts) >= 1 and parts[0].isdigit():
                    gens = int(parts[0])
                if len(parts) >= 2 and parts[1].isdigit():
                    pop = int(parts[1])

            from arc.packages import resolve_role
            GeneticOptimizerAgent = resolve_role("optimizer", workflow)
            ctx.memory["adapter"] = workflow.adapter
            target = ctx.memory.get("target", {})

            header(f"Genetic Optimisation  {c(artifact.name, CYAN)}  {c(f'[{gens} gen × pop {pop}]', DIM)}")
            if target:
                step("Target", target)
            hr()

            _reg = ctx.memory.get("schema_registry", {})

            async def _on_gen(gen, best_ind, best_out, fitness):
                fit_str = f"{fitness:.4g}" if fitness != float("inf") else "∞"
                pct = _pct_off(best_out, target, _reg) if target else ""
                print(f"  {c(f'gen {gen:>2}', DIM)}  fit={c(fit_str, CYAN)}  {c(pct, YELLOW)}")

            ga_result = await GeneticOptimizerAgent(context=ctx).run(
                artifact, target=target,
                max_generations=gens, pop_size=pop,
                on_generation=_on_gen,
            )
            hr()
            ok(f"GA done — {ga_result['generations_run']} generations")
            step("Best inputs",  {k: round(v, 6) for k, v in ga_result["best_inputs"].items()})
            step("Best outputs", {k: round(v, 6) if isinstance(v, float) else v
                                   for k, v in ga_result["best_outputs"].items()})
            if target:
                pct = _pct_off(ga_result["best_outputs"], target, _reg)
                if pct:
                    step("vs target", c(pct, YELLOW))
            if ga_result.get("converged"):
                ok(c("Converged within threshold!", GREEN, BOLD))
                _save_session(workflow, goal_text)
                break

            _save_session(workflow, goal_text)
            # Switch back to step mode with GA's best params for final verification
            strategy = "step"
            confirmed_budget = confirmed_budget if confirmed_budget else 1
            max_iterations = iterations_done + 1  # one final verification run
            # next_parameters already set in context by optimizer

        else:
            # step: put suggested params into context for next run_research
            if next_p:
                ctx.memory["next_parameters"] = next_p

    _save_session(workflow, goal_text)


# ── Chat loop ────────────────────────────────────────────────────────────────
# Startup helpers extracted into arc.chat.io_utils. Legacy names re-exported
# for callers that imported them from arc.chat (now arc.chat.loop).
from arc.chat.io_utils import (
    _SIM2L_SERVICES,
    check_sim2l_services as _check_sim2l_services,
    install_sigint_handler as _install_sigint_handler,
    print_banner,
)


async def _confirm_goal_launch(prompt_text: str) -> bool:
    """Ask the user to confirm before starting a research workflow."""
    try:
        confirm = (await chat_input_async(prompt_text)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return confirm not in ("n", "no")


async def _maybe_suggest_recipe(workflow, goal, ctx) -> None:
    """If a recipe's triggers match this goal, offer to apply it.

    Called once per ``run_research`` invocation, right after ideation
    prints the hypothesis + objective. We surface a single y/N prompt;
    accepting writes the recipe's strategy overrides into memory so
    every later phase in this same iteration picks them up.

    Never offers the same recipe twice in one session — see
    :func:`arc.core.recipe_suggest.remember_suggestion`.
    """
    try:
        from arc.core.recipe_suggest import remember_suggestion, suggest_recipe
        from arc.core.recipes import apply_recipe, validate_recipe
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("recipe_suggest unavailable: %s", exc)
        return

    suggestion = suggest_recipe(goal, ctx.memory)
    if suggestion is None:
        return

    # Don't pester even if the user closes the prompt without answering.
    remember_suggestion(ctx.memory, suggestion.recipe.name)

    if validate_recipe(suggestion.recipe):
        # Invalid recipe — log + skip rather than offer a broken one.
        logger.debug(
            "skipping suggestion of %r: recipe has validation errors",
            suggestion.recipe.name,
        )
        return

    print()
    step(
        c("▸ Suggested", CYAN, BOLD),
        f"/recipe apply {c(suggestion.recipe.name, CYAN, BOLD)}  "
        f"{c('— matches ' + suggestion.reason, DIM)}",
    )
    print(f"    {c(suggestion.recipe.display_strategies(), DIM)}")
    try:
        answer = (
            await chat_input_async(
                c("  Apply now? [y / N] > ", BOLD)
            )
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer not in ("y", "yes"):
        return

    result = apply_recipe(suggestion.recipe, ctx.memory)
    ctx.memory["active_recipe"] = suggestion.recipe.name
    ok(f"Applied recipe {c(suggestion.recipe.name, CYAN, BOLD)}")
    for role, impl in result.overrides_set.items():
        print(f"    {c(role, CYAN)} → {c(impl, CYAN, BOLD)}")
    if result.overrides_skipped:
        warn("Some roles were already manually overridden and were kept:")
        for role, impl in result.overrides_skipped.items():
            print(f"    {c(role, CYAN)} stayed at {c(impl, CYAN)}")


# ── Dispatch handlers ────────────────────────────────────────────────────
# Each handler runs the per-route side effects (prompts, workflow
# launches, state writes). They're called from chat_loop's main switch.


async def _handle_set_target(state, route) -> None:
    """Apply a typed set_target route (v2 only). No regex round-trip."""
    key = str(route.args.get("key", "")).strip()
    value = route.args.get("value")
    if not key or value is None:
        err("set_target requires a non-empty key and numeric value.")
        return
    try:
        state.target = {**state.target, key: float(value)}
    except (TypeError, ValueError):
        err(f"set_target value must be numeric, got {value!r}.")
        return
    ok(f"Target updated: {key} = {value}")
    _save_session(state.workflow, state.primary_goal)


async def _handle_goal(state, route, *, max_iterations: int) -> None:
    """Confirm and launch a fresh research goal.

    When a goal is already active, the prompt clarifies it's REPLACING
    the previous goal; otherwise it's the first goal of the session.
    Either way the user gets a chance to cancel.
    """
    primary_goal = state.primary_goal
    current_artifact = state.current_artifact
    if primary_goal and current_artifact:
        prompt_label = "New goal"
        prompt_text = c("  Start new research workflow? [Y/n] ", BOLD)
    else:
        prompt_label = "Goal"
        prompt_text = c("  Start research workflow? [Y/n] ", BOLD)
    print(f"\n  {c(prompt_label + ':', BOLD)} {c(route.text, CYAN)}")
    if not await _confirm_goal_launch(prompt_text):
        print(c("  Cancelled.", DIM))
        return
    state.reset_for_new_goal(route.text)
    await _run_with_continuation(
        state.workflow, route.text, max_iterations=max_iterations,
    )


async def _handle_refinement(state, route, *, max_iterations: int) -> None:
    """Apply a refinement route on top of the active goal.

    The router only emits this kind when ``has_active_goal=True``.
    Belt-and-braces: if the artifact has gone away between routing and
    dispatch, fall back to answering the input as a question rather
    than launching a half-configured workflow.
    """
    workflow = state.workflow
    primary_goal = state.primary_goal
    current_artifact = state.current_artifact
    if primary_goal is None or current_artifact is None:
        await _answer_question(workflow, route.text)
        return
    refinement = route.text
    print(f"  {c('Refining goal:', DIM)} {c(primary_goal[:60], DIM)}")
    print(f"  {c('Constraint:   ', DIM)} {c(refinement, YELLOW)}")
    workflow._context.memory.pop("current_plan", None)
    workflow._context.memory.pop("next_parameters", None)
    await _run_with_continuation(
        workflow, primary_goal,
        max_iterations=max_iterations,
        start_artifact=current_artifact,
        refinement=refinement,
    )


def _materialise_pending_sink(workflow) -> None:
    """Install the deferred event sink, resolving the per-session path.

    When ``--events jsonl`` is set without ``--events-path``, the file
    lands at ``<session_dir>/events.jsonl``. This runs AFTER
    ``_restore_session`` so ``workflow.session_id`` is final.

    Session paths are built via ``validate_session_id`` (refuses
    path-traversal) rather than a raw ``sim2l_home() / session_id`` join.
    """
    from arc.chat.events import (
        AnsiSink, JsonlSink, MultiSink, StdoutJsonSink,
        get_sink_config, set_sink, set_sink_config,
    )
    from arc.session import session_paths, sim2l_home, validate_session_id
    cfg = get_sink_config()
    if cfg is None:
        return
    # Per-session default path; honour explicit override.
    # validate_session_id() refuses traversal / shell-control characters.
    try:
        safe_id = validate_session_id(workflow.session_id)
    except ValueError as exc:
        # Refuse to attach a sink for an unsafe session id rather than
        # crash the chat — log + skip.
        import logging
        logging.getLogger(__name__).warning(
            "skipping --events sink, unsafe session_id: %s", exc,
        )
        set_sink_config(None)
        return
    session_dir = sim2l_home() / safe_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.path or (session_dir / "events.jsonl")
    if cfg.kind == "jsonl":
        set_sink(MultiSink(AnsiSink(), JsonlSink(path)))
    elif cfg.kind == "multi":
        set_sink(MultiSink(AnsiSink(), JsonlSink(path), StdoutJsonSink()))
    print(f"ⓘ Event log: {path}")
    # Consume the config so a re-entrant chat_loop doesn't double-install.
    set_sink_config(None)


class RouterBudgetExceeded(RuntimeError):
    """Raised when ``ChatState.router_calls`` exceeds ``router_call_budget``."""


async def _route_via_v2(raw: str, *, registry, provider, has_active_goal: bool,
                          tool_registry=None, state=None):
    """Adapter: run the v2 tool-call router and return a Phase-1-compatible Route.

    Lets the chat loop's dispatch logic stay unchanged when
    ``ARC_CHAT_V2=1`` is set. Slash commands are still handled by the
    legacy registry path (much cheaper and more reliable for them).

    ``tool_registry`` is optional. When ``None``, a fresh registry is
    built — handy for tests. In ``chat_loop`` it's built once at startup
    and passed on every call so we don't reallocate per user input.

    ``state`` is also optional; when provided, the function enforces
    ``state.router_call_budget``. The counter is bumped once per LLM
    round-trip — slash commands don't tick it.
    """
    from arc.chat.router import Route, route_input as _heuristic_route
    from arc.chat.router_v2 import route_via_tools
    from arc.chat.tools import build_tool_registry

    stripped = (raw or "").strip()
    if stripped.startswith("/") or stripped.startswith("\\"):
        # Slash commands always use the heuristic registry — v2 is for
        # free-text intent classification only.
        return await _heuristic_route(
            raw, registry=registry, provider=provider,
            has_active_goal=has_active_goal,
        )

    # Router cost budget. Refuse before contacting the provider so we
    # don't burn quota on an exhausted session.
    #
    # The counter only ticks when an LLM round-trip actually happens.
    # When ``provider is None`` (stub mode), ``route_via_tools``
    # short-circuits to the fallback tool without a network call, so we
    # skip the counter — otherwise stub-mode sessions would exhaust the
    # budget for free.
    if state is not None and provider is not None:
        budget = getattr(state, "router_call_budget", None)
        if budget is not None and state.router_calls >= budget:
            raise RouterBudgetExceeded(
                f"router budget exhausted ({state.router_calls}/{budget} calls); "
                "use /quit to end the session or restart with a higher budget"
            )
        state.router_calls += 1

    tool_reg = tool_registry if tool_registry is not None else build_tool_registry()
    decision = await route_via_tools(
        stripped,
        provider=provider,
        registry=tool_reg,
        has_active_goal=has_active_goal,
    )
    if decision.tool == "answer_question":
        return Route(kind="question", text=stripped)
    if decision.tool == "start_research_goal":
        goal_text = decision.args.get("goal") or stripped
        return Route(kind="goal", text=goal_text)
    if decision.tool == "refine_goal" and has_active_goal:
        refinement = decision.args.get("refinement") or stripped
        return Route(kind="refinement", text=refinement)
    if decision.tool == "set_target":
        # Typed route — no regex round-trip via free text. The chat loop
        # handles set_target by updating state.target directly.
        return Route(
            kind="set_target",
            text=stripped,
            args={
                "key":   decision.args.get("key", ""),
                "value": decision.args.get("value", 0.0),
            },
        )
    # Unknown / fallback path
    return Route(kind="question", text=stripped)


async def _authenticate_and_prompt(workflow) -> bool:
    """Log in to sim2l services and attach the resulting session ids.

    Returns ``True`` when the chat should continue, ``False`` when the
    user chose to abort. Always returns ``True`` when authentication
    fully succeeded; prompts only on partial / total failure.

    Side effect: when the workflow's adapter is a Sim2LRuntimeAdapter,
    its ``_catalog_session_id`` / ``_results_session_id`` /
    ``_cache_session_id`` fields are populated so subsequent pushes
    carry the right headers and the executor reaches the cache service.
    """
    from arc.chat.sim2l_auth import login_to_services

    import asyncio
    auth = await asyncio.get_event_loop().run_in_executor(None, login_to_services)

    # Attach to the adapter (if any) regardless of partial success — the
    # services that did authenticate should still receive pushes.
    adapter = getattr(workflow, "adapter", None)
    if adapter is not None:
        for attr, value in (
            ("_catalog_session_id", auth.catalog_session),
            ("_results_session_id", auth.results_session),
            ("_cache_session_id",   auth.cache_session),
        ):
            if value is not None:
                try:
                    setattr(adapter, attr, value)
                except AttributeError:
                    # Adapter doesn't accept it (LocalRuntimeAdapter, …).
                    pass

    if auth.authenticated:
        ok(f"Signed in to sim2l services as {c(auth.username, CYAN)}.")
        return True

    # Partial or total failure — make the user choose explicitly.
    if auth.partial:
        warn("Partial sim2l sign-in — some services rejected the login.")
    else:
        warn("Could not sign in to sim2l services.")
    for line in auth.errors:
        print(f"    {c('•', YELLOW)} {c(line, DIM)}")
    print(f"    {c('Hint:', DIM)} set SIM2L_USERNAME / SIM2L_PASSWORD in your env if "
          f"the defaults don't apply.")
    try:
        answer = (await chat_input_async(
            c("  Continue without persistence? Catalog and results pushes will fail. "
              "[y/N] ", BOLD)
        )).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


async def chat_loop(workflow: ResearchWorkflow, provider, model, base_url, max_iterations: int = 20):
    """Main REPL — dispatch via CommandRegistry, fall back to the router for
    free text.

    Phase 1 rewrite: the giant if/elif tree is gone. Slash commands resolve
    through ``arc.chat.commands.build_registry``; free text classifies via
    ``arc.chat.router.route_input`` and the corresponding handlers below.
    """
    import asyncio
    from arc.chat.commands import build_registry
    from arc.chat.commands.builtins import _QuitRequested
    from arc.chat.router import route_input
    from arc.chat.state import ChatState

    # ``ARC_CHAT_V2=1`` swaps to the v2 tool-call router (Phase 4);
    # default keeps the heuristic+LLM classifier (Phase 1).
    _use_v2_router = env_flag("ARC_CHAT_V2")

    _install_sigint_handler()

    # Restore any previously saved session state.
    saved_goal = _restore_session(workflow)

    # Materialise any deferred event sink config now that we know the
    # session id — so --events jsonl writes to <session>/events.jsonl
    # by default.
    _materialise_pending_sink(workflow)

    # Check sim2l service availability once at startup (fast, 1s timeout each).
    sim2l_status = await asyncio.get_event_loop().run_in_executor(None, _check_sim2l_services)
    print_banner(provider, model, base_url, workflow.session_id, _selected_coder(workflow),
                 sim2l_status=sim2l_status)

    from arc.services import (
        sim2l_available,
        is_running as _service_running,
        start as _start_service,
        start_all as _start_all,
    )

    if not any(sim2l_status.values()) and sim2l_available():
        print(f"  {c('⚠', YELLOW + BOLD)}  sim2l is installed but no services are running.")
        try:
            answer = (await chat_input_async(
                c("  Start sim2l services now? [Y/n] ", BOLD)
            )).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
            print()
        if answer not in ("n", "no"):
            print(f"  {c('starting services...', DIM)}", end="\r")
            results = await asyncio.get_event_loop().run_in_executor(None, _start_all)
            print(" " * 40, end="\r")
            for name, success, msg in results:
                if success:
                    ok(msg)
                else:
                    err(msg)
            # Refresh status line. Newly spawned Flask services can take a
            # moment after Popen returns before /health and /session/login are
            # reachable, especially the last service in the startup batch.
            for _ in range(10):
                sim2l_status = await asyncio.get_event_loop().run_in_executor(None, _check_sim2l_services)
                if all(sim2l_status.values()):
                    break
                await asyncio.sleep(0.5)
            print()
        else:
            print(f"  {c('Continuing without sim2l services — use /services start to start them later.', DIM)}")
            print()
    elif not any(sim2l_status.values()):
        print(f"  {c('ℹ', CYAN)}  No sim2l services running. Install sim2l or use /services start.")
        print()

    if sim2l_available() and env_flag("ARC_SIM2L_START_MCP"):
        mcp_running = await asyncio.get_event_loop().run_in_executor(
            None, _service_running, "mcp"
        )
        if not mcp_running:
            success, msg = await asyncio.get_event_loop().run_in_executor(
                None, _start_service, "mcp"
            )
            (ok if success else warn)(msg)

    # ── Authenticate to sim2l services so pushes don't silently 401 ──
    # Only attempt when at least one service is reachable AND sim2l is
    # actually installed; otherwise there's nothing to authenticate to.
    if any(sim2l_status.values()) and sim2l_available():
        proceed = await _authenticate_and_prompt(workflow)
        if not proceed:
            print(c("Goodbye.", DIM))
            return

    if saved_goal:
        ok(f"Resumed session  {c(workflow.session_id, CYAN)}")
        primary_goal = workflow._context.memory.get("primary_goal") or saved_goal
        step("Primary goal", primary_goal)
        refinements_so_far = workflow._context.memory.get("refinements", [])
        if refinements_so_far:
            step("Refinements", f"{len(refinements_so_far)}: " + "; ".join(refinements_so_far))
        artifact = workflow._context.memory.get("current_artifact")
        if artifact:
            step("Artifact", f"{artifact.name}  {c(artifact.artifact_id[:8] + '...', DIM)}")
        step("Iteration", workflow._context.iteration)
        print()
        # The active goal lives on ``state.primary_goal`` from here on.

    registry = build_registry()
    state = ChatState(workflow=workflow, max_iterations=max_iterations)
    state.sim2l_status = sim2l_status

    # Build tool registry once for v2 router. Cheap, but rebuilding
    # every keystroke would compound on long sessions.
    _tool_registry = None
    if _use_v2_router:
        from arc.chat.tools import build_tool_registry as _btr
        _tool_registry = _btr()

    while True:
        try:
            raw = (await chat_input_async(c("you> ", BOLD, CYAN))).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{c('Goodbye.', DIM)}")
            break

        if not raw:
            continue
        raw = _normalize_chat_command(raw)

        # Bareword shortcuts for legacy ``continue`` / ``resume`` —
        # translate into the slash form so the registry handles them.
        if raw.lower() in ("continue", "resume"):
            raw = "/continue"

        if _use_v2_router:
            try:
                route = await _route_via_v2(
                    raw,
                    registry=registry,
                    provider=workflow.provider,
                    has_active_goal=state.has_active_goal(),
                    tool_registry=_tool_registry,
                    state=state,
                )
            except RouterBudgetExceeded as exc:
                err(str(exc))
                continue
        else:
            # Apply the same budget guard to the v1 router's LLM
            # fallback. ``on_llm_call`` fires only when the heuristic
            # is uncertain and the LLM is about to be consulted; in
            # stub mode (provider=None) the LLM call short-circuits so
            # we skip the tick.
            def _check_budget():
                if workflow.provider is None:
                    return
                budget = getattr(state, "router_call_budget", None)
                if budget is not None and state.router_calls >= budget:
                    raise RouterBudgetExceeded(
                        f"router budget exhausted "
                        f"({state.router_calls}/{budget} calls); "
                        "use /quit to end the session"
                    )
                state.router_calls += 1
            try:
                route = await route_input(
                    raw,
                    registry=registry,
                    provider=workflow.provider,
                    has_active_goal=state.has_active_goal(),
                    on_llm_call=_check_budget,
                )
            except RouterBudgetExceeded as exc:
                err(str(exc))
                continue

        try:
            if route.kind == "command":
                # Apply the requires_* gates.
                if route.command.requires_provider and workflow.provider is None:
                    err(f"/{route.command.name} requires a live LLM provider. Start without --stub.")
                elif route.command.requires_artifact and state.current_artifact is None:
                    err(f"/{route.command.name} needs an active artifact. Set a goal first.")
                else:
                    try:
                        await route.command.resolve_handler()(state, route.argv)
                    except _QuitRequested:
                        break
                continue

            if route.kind == "command_error":
                err(route.error or "Unknown command.")
                print(c("  Type /help for available commands.", DIM))
                continue

            if route.kind == "noop":
                continue

            if route.kind == "question":
                await _answer_question(workflow, route.text)
                continue

            if route.kind == "set_target":
                await _handle_set_target(state, route)
                continue

            if route.kind == "goal":
                await _handle_goal(state, route, max_iterations=max_iterations)
                continue

            if route.kind == "refinement":
                await _handle_refinement(state, route, max_iterations=max_iterations)
                continue

        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Interrupted.', YELLOW)}  Session saved. Type a new goal or /quit.")
            _save_session(workflow, state.primary_goal)



def main():
    parser = argparse.ArgumentParser(description="ARC interactive chat")
    parser.add_argument("--provider", default=None,
                        help="Provider name. Defaults to ARC_PROVIDER or openwebui.")
    parser.add_argument("--token",    default=None,
                        help="Provider token. Defaults to OPENWEBUI_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY.")
    parser.add_argument("--model",    default=None,
                        help="Model name. Defaults to ARC_MODEL or provider-specific model env.")
    parser.add_argument("--url",      default=None,
                        help="Provider base URL. Defaults to OPENWEBUI_URL for openwebui.")
    parser.add_argument("--stub",     action="store_true")
    parser.add_argument("--session",  default=None,
                        help="Resume an existing session ID, or omit to start a new one.")
    parser.add_argument("--list-sessions", action="store_true",
                        help="List all sessions and exit.")
    parser.add_argument("--delete-session", metavar="SESSION_ID",
                        help="Delete a specific session and exit.")
    parser.add_argument("--delete-all-sessions", action="store_true",
                        help="Delete ALL sessions and exit (asks for confirmation).")
    parser.add_argument("--max-iterations", type=int, default=20,
                        help="Max auto-iterations per goal before stopping (default 20).")
    args = parser.parse_args()

    if args.list_sessions:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found.")
        for s in sessions:
            print(f"  {s['session_id']}  iter={s['iteration']}  {(s['goal'] or '')[:60]}")
        return

    if args.delete_session:
        sid = args.delete_session
        meta = load_session_meta(sid)
        if not meta:
            print(f"ERROR: session '{sid}' not found.", file=sys.stderr)
            sys.exit(1)
        confirm = chat_input(f"Delete session '{sid}' and all its data? [y/N] ").strip().lower()
        if confirm in ("y", "yes"):
            if delete_session(sid):
                print(f"Deleted session '{sid}'.")
            else:
                print(f"ERROR: could not delete '{sid}'.", file=sys.stderr)
                sys.exit(1)
        else:
            print("Aborted.")
        return

    if args.delete_all_sessions:
        sessions = list_sessions()
        if not sessions:
            print("No sessions to delete.")
            return
        print(f"Found {len(sessions)} session(s):")
        for s in sessions:
            print(f"  {s['session_id']}  iter={s['iteration']}  {(s['goal'] or '')[:50]}")
        confirm = chat_input("Delete ALL sessions and their data? [y/N] ").strip().lower()
        if confirm in ("y", "yes"):
            deleted = delete_all_sessions()
            print(f"Deleted {len(deleted)} session(s).")
        else:
            print("Aborted.")
        return

    if args.stub:
        provider = token = model = base_url = None
    else:
        provider = args.provider or os.environ.get("ARC_PROVIDER") or "openwebui"
        model = (
            args.model
            or os.environ.get("ARC_MODEL")
            or os.environ.get("OPENWEBUI_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
        )
        if provider == "openwebui":
            token = args.token or os.environ.get("OPENWEBUI_KEY")
            base_url = args.url or os.environ.get("OPENWEBUI_URL") or "https://genai.rcac.purdue.edu/api"
        elif provider == "openai":
            token = args.token or os.environ.get("OPENAI_API_KEY")
            base_url = args.url
        elif provider == "anthropic":
            token = args.token or os.environ.get("ANTHROPIC_API_KEY")
            base_url = args.url
        else:
            token = args.token
            base_url = args.url
        if not token:
            print(
                f"ERROR: token required for provider '{provider}'. "
                "Set it in .env or pass --token, or use --stub.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Always use an explicit session ID — resume with --session, otherwise create fresh.
    if args.session:
        session_id = args.session
        meta = load_session_meta(session_id)
        if not meta:
            print(f"ERROR: session '{session_id}' not found. Use --list-sessions to see available sessions.", file=sys.stderr)
            sys.exit(1)
    else:
        session_id = new_session_id()
        save_session_meta(
            session_id=session_id,
            goal=None,
            iteration=0,
            current_artifact_id=None,
            current_artifact_name=None,
            run_history=[],
            target={},
            next_parameters={},
            packages={},
            agent_overrides={},
            created=datetime.now(timezone.utc).isoformat(),
        )

    workflow = ResearchWorkflow(
        provider_name=provider,
        token=token,
        model=model,
        base_url=base_url,
        session_id=session_id,
    )

    asyncio.run(chat_loop(workflow, provider, model, base_url, max_iterations=args.max_iterations))


if __name__ == "__main__":
    main()
