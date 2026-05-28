"""Reusable pipeline hooks.

Hooks here are pure helpers — they don't own state, they just react to
phase events. The chat layer wires them up before invoking the pipeline.
"""

from __future__ import annotations

from typing import Optional

from arc.chat.events import emit
from arc.chat.research.pipeline import (
    Phase,
    PipelineHook,
    PipelineState,
)


# ── auto_save_after — persist session after each completed phase ──────────

def auto_save_after() -> PipelineHook:
    """Save the session to disk after every phase finishes.

    Persists the active ``goal_text`` (the refined compound goal being
    processed, not the bare ``primary_goal``) so session.json reflects
    exactly what was running when a crash interrupts the pipeline.

    Fixes the legacy issue that ``_save_session`` was called in only 3
    of 12 spots; with this hook it runs after every phase boundary so
    crash recovery is consistent.
    """
    async def _cb(state: PipelineState, phase: Phase, exc):
        from arc.chat.session_io import save_session
        # state.goal_text holds the actual goal being processed (may
        # include refinements). Fall back to primary_goal if absent.
        goal = state.goal_text or state.workflow._context.memory.get("primary_goal")
        save_session(state.workflow, goal)
    return PipelineHook("after_phase", None, _cb)


# ── phase_events — emit structured events at phase boundaries ────────────

def phase_events() -> list[PipelineHook]:
    """Emit ``phase_start`` / ``phase_end`` / ``phase_error`` structured
    events. Consumed by the JSONL sink (Phase 2)."""
    async def _start(state: PipelineState, phase: Phase, exc):
        emit("phase_start", phase.name, phase=phase.name)

    async def _end(state: PipelineState, phase: Phase, exc):
        emit("phase_end", phase.name, phase=phase.name)

    async def _err(state: PipelineState, phase: Phase, exc):
        emit("err", f"phase {phase.name} raised: {exc!r}",
             phase=phase.name, error=type(exc).__name__ if exc else "")

    return [
        PipelineHook("before_phase", None, _start),
        PipelineHook("after_phase",  None, _end),
        PipelineHook("on_error",     None, _err),
    ]


# ── plan_mode_skip — short-circuit side-effecting phases in plan mode ────

def plan_mode_skip(*phase_names: str) -> PipelineHook:
    """Return an ``on_error`` hook that absorbs ``PlanModeBlocked`` errors
    raised by listed phases. Use when a phase calls ``check_plan_gate``
    but the rest of the pipeline should continue.
    """
    targets = set(phase_names)

    async def _cb(state: PipelineState, phase: Phase, exc):
        if phase.name not in targets:
            return
        # Re-raise anything that isn't a plan-mode block — the runner
        # checks the truthiness of hook handling, not the absorb count.
        from arc.chat.plan_mode import PlanModeBlocked
        if not isinstance(exc, PlanModeBlocked):
            raise exc  # noqa: TRY201 — re-raise verbatim
        emit("info", f"[plan-mode] phase {phase.name} skipped", phase=phase.name)

    return PipelineHook("on_error", None, _cb)
