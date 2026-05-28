"""Phase / Pipeline core abstractions.

A ``Phase`` is one logical step in the research workflow. It implements:

    async def run(self, state: PipelineState) -> PipelineState

and optionally ``should_run(state) -> bool``. Phases mutate ``state`` and
return it; the pipeline composes them.

Hooks (``PipelineHook``) subscribe to ``before_phase`` / ``after_phase``
/ ``on_error`` events. A buggy hook can never crash the pipeline — the
runner swallows hook exceptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)


logger = logging.getLogger(__name__)


HookEvent = Literal["before_phase", "after_phase", "on_error"]


# ── State ────────────────────────────────────────────────────────────────


@dataclass
class PipelineState:
    """Mutable bag of data passed through every phase.

    Phases read what they need and write back any results they produce.
    Compared to a ``ResearchContext.memory`` dict, this gives us a typed
    surface for the most-used fields without locking the loose dict.
    """

    workflow: Any                 # arc.orchestrator.workflow.ResearchWorkflow
    goal_text: str
    domain: str | None = None
    artifact: Any = None          # ArtifactRecord
    refinement: str | None = None
    target: dict = field(default_factory=dict)
    plan: Any = None              # ExperimentPlan
    proposal: Any = None          # ResearchProposal
    execution: Any = None         # Execution
    review: Any = None            # ReviewResult
    reflection: Any = None
    extras: dict = field(default_factory=dict)
    # Lifecycle flags
    is_new_artifact: bool = True
    rebuild_for_refinement: bool = False
    aborted: bool = False         # set by a phase that wants the pipeline to stop
    # Target keys the user asked for that the executed artifact didn't
    # produce. Populated by ``ExecutionPhase`` and consumed by the chat
    # loop, which uses it to schedule an artifact rebuild instead of
    # looping with an artifact that can never approve.
    unmatched_target_keys: list[str] = field(default_factory=list)


# ── Phase protocol ───────────────────────────────────────────────────────


@runtime_checkable
class Phase(Protocol):
    """One step in the research pipeline."""

    name: str

    def should_run(self, state: PipelineState) -> bool: ...

    async def run(self, state: PipelineState) -> PipelineState: ...


# ── Hooks ────────────────────────────────────────────────────────────────


@dataclass
class PipelineHook:
    """Subscribe to phase lifecycle events.

    ``phase_name=None`` matches all phases. ``callback`` is awaitable and
    receives ``(state, phase, exc)``: ``exc`` is None except for
    ``on_error`` events.
    """

    event: HookEvent
    phase_name: Optional[str]
    callback: Callable[[PipelineState, Phase, Optional[BaseException]], Awaitable[None]]


class PipelinePhaseError(RuntimeError):
    """Raised by ``Pipeline.run`` when a phase raises and no on_error hook
    suppressed the error. The original exception is the ``__cause__``."""

    def __init__(self, phase_name: str, original: BaseException):
        super().__init__(f"phase {phase_name!r} failed: {original!r}")
        self.phase_name = phase_name


# ── Runner ───────────────────────────────────────────────────────────────


class Pipeline:
    """Runs a list of phases in order, dispatching hooks at each boundary."""

    def __init__(
        self,
        phases: Iterable[Phase],
        hooks: Iterable[PipelineHook] = (),
    ):
        self.phases = list(phases)
        self.hooks = list(hooks)

    def add_hook(self, hook: PipelineHook) -> None:
        self.hooks.append(hook)

    async def run(self, state: PipelineState) -> PipelineState:
        for phase in self.phases:
            if state.aborted:
                logger.info("pipeline aborted before phase %s", phase.name)
                break
            if not phase.should_run(state):
                logger.debug("skipping phase %s (should_run=False)", phase.name)
                continue
            await self._dispatch_hooks("before_phase", state, phase)
            try:
                state = await phase.run(state)
            except BaseException as exc:  # noqa: BLE001 — re-raised or hook-handled
                handled = await self._dispatch_hooks(
                    "on_error", state, phase, exc=exc, allow_handle=True
                )
                if handled:
                    continue
                raise PipelinePhaseError(phase.name, exc) from exc
            await self._dispatch_hooks("after_phase", state, phase)
        return state

    async def _dispatch_hooks(
        self,
        event: HookEvent,
        state: PipelineState,
        phase: Phase,
        *,
        exc: BaseException | None = None,
        allow_handle: bool = False,
    ) -> bool:
        """Run every matching hook. Returns True if at least one
        on_error hook ran (we treat that as "handled").

        Hook exceptions are caught and logged so a buggy hook can't
        break the pipeline.
        """
        handled = False
        for hook in self.hooks:
            if hook.event != event:
                continue
            if hook.phase_name is not None and hook.phase_name != phase.name:
                continue
            try:
                await hook.callback(state, phase, exc)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "hook %s for phase %s raised; ignoring",
                    hook.callback, phase.name,
                )
                continue
            handled = True if allow_handle else handled
        return handled


# ── Convenience entry point ──────────────────────────────────────────────


async def run_pipeline(
    phases: Iterable[Phase],
    state: PipelineState,
    hooks: Iterable[PipelineHook] = (),
) -> PipelineState:
    """Build a one-shot Pipeline and run it."""
    return await Pipeline(phases, hooks).run(state)
