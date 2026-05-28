"""Pipeline / Phase / Hook core tests (Phase 3)."""

from dataclasses import dataclass

import pytest

from arc.chat.research.pipeline import (
    Pipeline,
    PipelineHook,
    PipelinePhaseError,
    PipelineState,
    run_pipeline,
)
from tests.fakes import make_workflow


pytestmark = pytest.mark.chat


# ── A tiny helper to build PipelineState ──────────────────────────────────

def _state(goal: str = "test goal") -> PipelineState:
    return PipelineState(workflow=make_workflow(), goal_text=goal)


# ── Minimal Phase implementations used across tests ───────────────────────

class AppendPhase:
    """Appends its name to ``state.extras["log"]`` and increments ``n``."""

    def __init__(self, name: str, predicate=None):
        self.name = name
        self._predicate = predicate

    def should_run(self, state: PipelineState) -> bool:
        if self._predicate is None:
            return True
        return self._predicate(state)

    async def run(self, state: PipelineState) -> PipelineState:
        state.extras.setdefault("log", []).append(self.name)
        state.extras["n"] = state.extras.get("n", 0) + 1
        return state


class FailingPhase:
    name = "failing"

    def should_run(self, state):
        return True

    async def run(self, state):
        raise RuntimeError("boom")


class AbortingPhase:
    name = "aborter"

    def should_run(self, state):
        return True

    async def run(self, state):
        state.aborted = True
        return state


# ── Basic flow ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_runs_each_phase_in_order():
    state = _state()
    pipe = Pipeline([AppendPhase("a"), AppendPhase("b"), AppendPhase("c")])
    state = await pipe.run(state)
    assert state.extras["log"] == ["a", "b", "c"]
    assert state.extras["n"] == 3


@pytest.mark.asyncio
async def test_phase_skipped_when_should_run_false():
    state = _state()
    pipe = Pipeline([
        AppendPhase("a"),
        AppendPhase("b", predicate=lambda s: False),
        AppendPhase("c"),
    ])
    state = await pipe.run(state)
    assert state.extras["log"] == ["a", "c"]


@pytest.mark.asyncio
async def test_aborted_state_stops_pipeline():
    state = _state()
    pipe = Pipeline([
        AppendPhase("a"),
        AbortingPhase(),
        AppendPhase("c"),
    ])
    state = await pipe.run(state)
    # "c" must not have run
    assert state.extras["log"] == ["a"]
    assert state.aborted is True


@pytest.mark.asyncio
async def test_run_pipeline_convenience():
    state = _state()
    state = await run_pipeline([AppendPhase("only")], state)
    assert state.extras["log"] == ["only"]


# ── Hooks ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_before_after_hooks_fire():
    state = _state()
    fired = []

    async def before(s, phase, exc):
        fired.append(("before", phase.name))

    async def after(s, phase, exc):
        fired.append(("after", phase.name))

    pipe = Pipeline(
        [AppendPhase("p")],
        hooks=[
            PipelineHook("before_phase", None, before),
            PipelineHook("after_phase", None, after),
        ],
    )
    await pipe.run(state)
    assert fired == [("before", "p"), ("after", "p")]


@pytest.mark.asyncio
async def test_hook_filtered_by_phase_name():
    state = _state()
    fired = []

    async def cb(s, phase, exc):
        fired.append(phase.name)

    pipe = Pipeline(
        [AppendPhase("a"), AppendPhase("b")],
        hooks=[PipelineHook("after_phase", "b", cb)],
    )
    await pipe.run(state)
    assert fired == ["b"]


@pytest.mark.asyncio
async def test_phase_error_raises_pipeline_phase_error():
    state = _state()
    pipe = Pipeline([AppendPhase("a"), FailingPhase()])
    with pytest.raises(PipelinePhaseError) as exc_info:
        await pipe.run(state)
    assert exc_info.value.phase_name == "failing"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_on_error_hook_suppresses_failure():
    state = _state()
    handled = []

    async def on_err(s, phase, exc):
        handled.append((phase.name, type(exc).__name__))

    pipe = Pipeline(
        [AppendPhase("a"), FailingPhase(), AppendPhase("c")],
        hooks=[PipelineHook("on_error", None, on_err)],
    )
    # Should not raise — hook absorbed the error
    state = await pipe.run(state)
    assert handled == [("failing", "RuntimeError")]
    # Subsequent phases still ran
    assert "c" in state.extras["log"]


@pytest.mark.asyncio
async def test_buggy_hook_does_not_crash_pipeline():
    state = _state()

    async def boom(s, phase, exc):
        raise ValueError("hook is broken")

    pipe = Pipeline(
        [AppendPhase("a")],
        hooks=[PipelineHook("before_phase", None, boom)],
    )
    # Should complete without raising
    state = await pipe.run(state)
    assert state.extras["log"] == ["a"]


@pytest.mark.asyncio
async def test_add_hook_after_construction():
    state = _state()
    fired = []

    async def cb(s, phase, exc):
        fired.append(phase.name)

    pipe = Pipeline([AppendPhase("a")])
    pipe.add_hook(PipelineHook("after_phase", None, cb))
    await pipe.run(state)
    assert fired == ["a"]


# ── PipelineState integrity ──────────────────────────────────────────────

def test_pipeline_state_defaults():
    state = _state()
    assert state.target == {}
    assert state.extras == {}
    assert state.aborted is False
    assert state.is_new_artifact is True
