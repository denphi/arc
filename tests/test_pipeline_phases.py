"""Phase classes + reusable hooks (Phase 3)."""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from arc.chat.research.phases import (
    ExecutionPhase,
    ProvenancePhase,
    ReflectionPhase,
    ReviewPhase,
    ValidationPhase,
    POST_BUILD_PHASES,
)
from arc.chat.research.pipeline import Pipeline, PipelineState
from tests.fakes import make_artifact, make_run, make_workflow


pytestmark = pytest.mark.chat


# ── Validation phase ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_phase_passes_when_artifact_valid():
    artifact = make_artifact()
    validation_result = SimpleNamespace(valid=True, errors=[], warnings=[])

    class Adapter:
        async def validate_artifact(self, a):
            assert a is artifact
            return validation_result

    wf = make_workflow()
    wf.adapter = Adapter()
    state = PipelineState(workflow=wf, goal_text="x", artifact=artifact,
                          is_new_artifact=True)
    state = await ValidationPhase().run(state)
    assert state.aborted is False


@pytest.mark.asyncio
async def test_validation_phase_aborts_on_failure(capsys):
    class Adapter:
        async def validate_artifact(self, a):
            return SimpleNamespace(valid=False,
                                   errors=["missing schema field 'bandgap_ev'"],
                                   warnings=["consider raising thickness"])

    wf = make_workflow()
    wf.adapter = Adapter()
    state = PipelineState(workflow=wf, goal_text="x",
                          artifact=make_artifact(), is_new_artifact=True)
    state = await ValidationPhase().run(state)
    assert state.aborted is True
    out = capsys.readouterr().out
    assert "missing schema field" in out


def test_validation_skipped_when_artifact_is_reused():
    """is_new_artifact=False means we already validated this run; skip."""
    state = PipelineState(workflow=make_workflow(), goal_text="x",
                          artifact=make_artifact(), is_new_artifact=False)
    assert ValidationPhase().should_run(state) is False


def test_validation_skipped_without_artifact():
    state = PipelineState(workflow=make_workflow(), goal_text="x", artifact=None)
    assert ValidationPhase().should_run(state) is False


# ── Execution phase ──────────────────────────────────────────────────────

@dataclass
class _FakeAdapter:
    """Captures call args for assertions."""
    prepare_calls: list = field(default_factory=list)
    run_calls: list = field(default_factory=list)

    async def prepare_inputs(self, artifact, params):
        self.prepare_calls.append((artifact, params))
        return params  # echo

    async def run(self, artifact, inputs):
        self.run_calls.append((artifact, inputs))
        return SimpleNamespace(
            run_id="run-123-abcdef",
            status="completed",
            outputs={"bandgap_ev": 1.12},
            logs=["start", "done"],
        )


class _FakeResults:
    def __init__(self):
        self.saved = []
    def save(self, ex):
        self.saved.append(ex)
    def list_all(self):
        return list(self.saved)


class _FakeBackend:
    def __init__(self):
        self.persisted = []
        self.recorded = []

    async def persist_result(self, artifact, execution, inputs):
        self.persisted.append((artifact, execution, inputs))
        return {"persisted": True}

    async def record_execution(self, artifact, execution, inputs, outputs):
        self.recorded.append((artifact, execution, inputs, outputs))
        return {"recorded": True}


class _RaisingBackend:
    name = "raising"

    async def persist_result(self, *args):
        raise RuntimeError("backend unavailable")

    async def record_execution(self, *args):
        raise RuntimeError("backend unavailable")


@pytest.mark.asyncio
async def test_execution_phase_uses_explicit_run_inputs():
    adapter = _FakeAdapter()
    wf = make_workflow()
    wf.adapter = adapter
    wf.results = _FakeResults()
    state = PipelineState(workflow=wf, goal_text="x", artifact=make_artifact())
    state.extras["run_inputs"] = {"thickness": 5.0}

    state = await ExecutionPhase().run(state)
    assert state.execution is not None
    assert adapter.prepare_calls[0][1] == {"thickness": 5.0}
    assert wf.results.saved == [state.execution]


@pytest.mark.asyncio
async def test_execution_phase_calls_backend_result_actions():
    adapter = _FakeAdapter()
    backend = _FakeBackend()
    wf = make_workflow()
    wf.adapter = adapter
    wf.backend = backend
    wf.results = _FakeResults()
    artifact = make_artifact()
    state = PipelineState(workflow=wf, goal_text="x", artifact=artifact)
    state.extras["run_inputs"] = {"thickness": 5.0}

    state = await ExecutionPhase().run(state)

    assert backend.persisted == [(artifact, state.execution, {"thickness": 5.0})]
    assert backend.recorded == [
        (artifact, state.execution, {"thickness": 5.0}, state.execution.outputs)
    ]
    assert state.extras["backend_persist"]["persisted"] is True
    assert state.extras["backend_record"]["recorded"] is True


@pytest.mark.asyncio
async def test_execution_phase_backend_errors_do_not_abort_run():
    adapter = _FakeAdapter()
    wf = make_workflow()
    wf.adapter = adapter
    wf.backend = _RaisingBackend()
    wf.results = _FakeResults()
    state = PipelineState(workflow=wf, goal_text="x", artifact=make_artifact())

    state = await ExecutionPhase().run(state)

    assert state.execution is not None
    assert state.extras["backend_persist"]["persisted"] is False
    assert "backend unavailable" in state.extras["backend_persist"]["error"]


@pytest.mark.asyncio
async def test_execution_phase_falls_back_to_plan_parameters():
    adapter = _FakeAdapter()
    wf = make_workflow()
    wf.adapter = adapter
    wf.results = _FakeResults()
    plan = SimpleNamespace(parameters={"thickness": 9.0})
    state = PipelineState(workflow=wf, goal_text="x", artifact=make_artifact(),
                          plan=plan, is_new_artifact=True)
    await ExecutionPhase().run(state)
    assert adapter.prepare_calls[0][1] == {"thickness": 9.0}


@pytest.mark.asyncio
async def test_execution_phase_prints_distance_to_target(capsys):
    adapter = _FakeAdapter()
    wf = make_workflow()
    wf.adapter = adapter
    wf.results = _FakeResults()
    state = PipelineState(workflow=wf, goal_text="x", artifact=make_artifact(),
                          target={"bandgap_ev": 1.0})
    state.extras["run_inputs"] = {}
    state = await ExecutionPhase().run(state)
    out = capsys.readouterr().out
    # Adapter returned 1.12 vs target 1.0 → 12% off
    assert "vs target" in out
    assert "bandgap_ev" in out


def test_execution_skipped_without_artifact():
    state = PipelineState(workflow=make_workflow(), goal_text="x", artifact=None)
    assert ExecutionPhase().should_run(state) is False


def test_execution_skipped_when_aborted():
    state = PipelineState(workflow=make_workflow(), goal_text="x",
                          artifact=make_artifact(), aborted=True)
    assert ExecutionPhase().should_run(state) is False


# ── Review / Reflection / Provenance — just smoke-test the gates ──────────

def test_review_skipped_without_execution():
    state = PipelineState(workflow=make_workflow(), goal_text="x", artifact=make_artifact())
    assert ReviewPhase().should_run(state) is False


def test_reflection_skipped_without_review():
    state = PipelineState(workflow=make_workflow(), goal_text="x",
                          execution=make_run())
    assert ReflectionPhase().should_run(state) is False


def test_provenance_skipped_without_review():
    state = PipelineState(workflow=make_workflow(), goal_text="x",
                          artifact=make_artifact(), execution=make_run())
    assert ProvenancePhase().should_run(state) is False


def test_post_build_phase_order():
    """Critical invariant: validate → execute → review → reflect → record."""
    names = [p.name for p in POST_BUILD_PHASES]
    assert names == ["validation", "execution", "review", "reflection", "provenance"]


# ── Hook helpers ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_events_hook_emits_structured_events():
    from arc.chat.events import set_sink
    from arc.chat.research.hooks import phase_events
    from arc.chat.research.phases import ValidationPhase

    class CapSink:
        def __init__(self): self.events = []
        def handle(self, ev): self.events.append(ev)
        def close(self): pass

    sink = CapSink()
    prev = set_sink(sink)
    try:
        wf = make_workflow()
        wf.adapter = _FakeAdapter()
        # Make validate_artifact return a valid result
        async def validate(_): return SimpleNamespace(valid=True, errors=[], warnings=[])
        wf.adapter.validate_artifact = validate

        state = PipelineState(workflow=wf, goal_text="x",
                              artifact=make_artifact(), is_new_artifact=True)
        pipe = Pipeline([ValidationPhase()], hooks=phase_events())
        await pipe.run(state)
    finally:
        set_sink(prev)

    kinds = [e.kind for e in sink.events]
    assert "phase_start" in kinds and "phase_end" in kinds


@pytest.mark.asyncio
async def test_auto_save_after_calls_save_session_with_active_goal_text(monkeypatch):
    """The hook MUST persist the current ``goal_text`` (which may include
    refinements), not the bare ``primary_goal`` from memory. Regression
    for P3-2."""
    from arc.chat.research.hooks import auto_save_after
    from arc.chat.research.phases import ValidationPhase

    calls = []

    def fake_save(workflow, goal):
        calls.append((workflow, goal))

    monkeypatch.setattr("arc.chat.session_io.save_session", fake_save)

    # Primary goal in memory differs from goal_text (refinement scenario)
    wf = make_workflow(memory={"primary_goal": "primary only",
                                "refinements": ["bigger thickness"]})
    async def validate(_): return SimpleNamespace(valid=True, errors=[], warnings=[])
    wf.adapter = _FakeAdapter()
    wf.adapter.validate_artifact = validate

    state = PipelineState(
        workflow=wf,
        goal_text="primary only\n\nrefined: bigger thickness",
        artifact=make_artifact(),
        is_new_artifact=True,
    )
    pipe = Pipeline([ValidationPhase()], hooks=[auto_save_after()])
    await pipe.run(state)

    assert calls, "auto_save_after should call _save_session at least once"
    # The hook must use goal_text, NOT primary_goal
    assert calls[0][1] == "primary only\n\nrefined: bigger thickness"
    assert calls[0][1] != "primary only"


@pytest.mark.asyncio
async def test_auto_save_after_falls_back_to_primary_goal_when_no_goal_text(monkeypatch):
    """Edge case: empty goal_text → fall back to memory['primary_goal']."""
    from arc.chat.research.hooks import auto_save_after
    from arc.chat.research.phases import ValidationPhase

    calls = []
    monkeypatch.setattr(
        "arc.chat.session_io.save_session",
        lambda workflow, goal: calls.append((workflow, goal)),
    )

    wf = make_workflow(memory={"primary_goal": "fallback"})
    async def validate(_): return SimpleNamespace(valid=True, errors=[], warnings=[])
    wf.adapter = _FakeAdapter()
    wf.adapter.validate_artifact = validate

    state = PipelineState(workflow=wf, goal_text="",
                          artifact=make_artifact(), is_new_artifact=True)
    pipe = Pipeline([ValidationPhase()], hooks=[auto_save_after()])
    await pipe.run(state)

    assert calls[0][1] == "fallback"
