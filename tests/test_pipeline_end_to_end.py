"""End-to-end pipeline integration test.

Drives ``POST_BUILD_PHASES`` through real ``Pipeline.run`` (no mocking
of the runner itself) against stub agents and a stub adapter, asserting:

  * Every phase fires in the expected order
  * Phase output flows into the next phase's input
  * The execution log written to ``workflow.results`` matches what
    ``workflow.adapter.run`` returned
  * ``workflow.provenance.record`` is called with the right keys
  * ``ctx.iteration`` ticks exactly once
  * Validation failure aborts the pipeline cleanly

This is the test that catches a future "silent regression" in
``run_research`` after the Phase 3 split (Finding #6/#7 from the
post-fix review).
"""

from types import SimpleNamespace

import pytest

from arc.chat.research.phases import POST_BUILD_PHASES
from arc.chat.research.pipeline import Pipeline, PipelineState
from tests.fakes import make_artifact


pytestmark = pytest.mark.chat


# ── Stub support ──────────────────────────────────────────────────────────


class _RecordingAdapter:
    """Stub Sim2L runtime adapter that records every call."""

    def __init__(self, *, validation_valid: bool = True,
                 run_outputs: dict | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.validation_valid = validation_valid
        self.run_outputs = run_outputs or {"bandgap_ev": 1.12}

    async def validate_artifact(self, artifact):
        self.calls.append(("validate_artifact", (artifact,), {}))
        return SimpleNamespace(
            valid=self.validation_valid,
            errors=[] if self.validation_valid else ["bad schema"],
            warnings=[],
        )

    async def prepare_inputs(self, artifact, params):
        self.calls.append(("prepare_inputs", (artifact, params), {}))
        return dict(params)

    async def run(self, artifact, inputs):
        self.calls.append(("run", (artifact, inputs), {}))
        return SimpleNamespace(
            run_id="run-e2e-12345678",
            status="completed",
            outputs=dict(self.run_outputs),
            logs=["starting", "done"],
        )


class _RecordingResults:
    def __init__(self):
        self.saved = []
    def save(self, ex):
        self.saved.append(ex)
    def list_all(self):
        return list(self.saved)


class _RecordingProvenance:
    def __init__(self):
        self.records = []
    def record(self, **kwargs):
        self.records.append(kwargs)


def _build_state(monkeypatch, *, validation_valid=True, target=None,
                 outputs=None, is_new=True):
    """Construct a PipelineState wired to stub everything the phases need."""
    # Stub the agents that ReviewPhase / ReflectionPhase load
    class _StubReviewer:
        def __init__(self, context):
            self.context = context
        async def run(self, execution):
            return _FakeReview()

    class _StubReflector:
        def __init__(self, context):
            self.context = context
        async def run(self, review, execution=None):
            return "lessons-learned"

    reviewer_pkg = SimpleNamespace(
        _keys_match=lambda a, b: a == b,  # exact match
        ReviewerAgent=_StubReviewer,
    )
    reflector_pkg = SimpleNamespace(
        ReflectorAgent=_StubReflector,
    )
    monkeypatch.setattr("arc.packages.load_reviewer", lambda: reviewer_pkg)
    monkeypatch.setattr("arc.packages.load_reflector", lambda: reflector_pkg)
    # Phases now go through resolve_role() instead of load_*().
    # Patch both code paths so the test still injects the stubs.
    role_map = {"reviewer": _StubReviewer, "reflector": _StubReflector}
    monkeypatch.setattr(
        "arc.packages.resolve_role",
        lambda role, workflow=None: role_map[role],
    )

    adapter = _RecordingAdapter(
        validation_valid=validation_valid,
        run_outputs=outputs or {"bandgap_ev": 1.12},
    )
    results = _RecordingResults()
    provenance = _RecordingProvenance()
    workflow = SimpleNamespace(
        adapter=adapter,
        results=results,
        provenance=provenance,
        session_id="sess-e2e",
        _context=SimpleNamespace(
            memory={},
            iteration=0,
            session_id="sess-e2e",
        ),
    )
    plan = SimpleNamespace(parameters={"thickness": 5.0})
    state = PipelineState(
        workflow=workflow,
        goal_text="e2e test goal",
        domain="materials",
        artifact=make_artifact(),
        target=target or {},
        plan=plan,
        is_new_artifact=is_new,
    )
    return state, adapter, results, provenance


class _FakeReview:
    """Stand-in for the reviewer's ReviewResult — just enough attrs."""
    approved = True
    summary = "looks good"
    strengths = ["clear hypothesis"]
    weaknesses = []
    recommendations = ["try GA next"]
    next_parameters = {"thickness": 6.0}
    iteration_complete = True
    strategy = "stop"
    def model_dump(self):
        return {
            "approved": True, "summary": self.summary,
            "next_parameters": self.next_parameters,
        }


# ── The actual end-to-end test ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_runs_all_post_build_phases_in_order(monkeypatch):
    """Full pipeline: validation → execution → review → reflection → provenance."""
    state, adapter, results, provenance = _build_state(monkeypatch)
    pipe = Pipeline(POST_BUILD_PHASES)
    state = await pipe.run(state)

    # 1. Validation happened first
    assert adapter.calls[0][0] == "validate_artifact"
    # 2. Execution next: prepare_inputs then run
    assert adapter.calls[1][0] == "prepare_inputs"
    assert adapter.calls[2][0] == "run"
    # 3. Run output is on state.execution
    assert state.execution is not None
    assert state.execution.run_id == "run-e2e-12345678"
    assert state.execution.outputs == {"bandgap_ev": 1.12}
    # 4. Execution was saved to workflow.results
    assert len(results.saved) == 1
    assert results.saved[0] is state.execution
    # 5. Review fired — state.review carries the fake review
    assert state.review is not None
    assert state.review.approved is True
    # 6. Reflection wrote into state.reflection
    assert state.reflection == "lessons-learned"
    # 7. Provenance.record was called exactly once with the right keys
    assert len(provenance.records) == 1
    rec = provenance.records[0]
    assert rec["session_id"] == "sess-e2e"
    assert rec["artifact_id"] == state.artifact.artifact_id
    assert rec["run_id"] == state.execution.run_id
    # 8. Iteration counter ticked exactly once
    assert state.workflow._context.iteration == 1


@pytest.mark.asyncio
async def test_pipeline_aborts_on_validation_failure(monkeypatch):
    """When validate_artifact returns invalid=True, subsequent phases skip."""
    state, adapter, results, provenance = _build_state(
        monkeypatch, validation_valid=False,
    )
    pipe = Pipeline(POST_BUILD_PHASES)
    state = await pipe.run(state)

    assert state.aborted is True
    # Validation ran; execution/review did NOT
    call_names = [c[0] for c in adapter.calls]
    assert "validate_artifact" in call_names
    assert "prepare_inputs" not in call_names
    assert "run" not in call_names
    # No execution saved
    assert results.saved == []
    # No provenance written
    assert provenance.records == []
    # Iteration NOT bumped — the run failed before it counted
    assert state.workflow._context.iteration == 0


@pytest.mark.asyncio
async def test_pipeline_skips_validation_when_reusing_artifact(monkeypatch):
    """is_new_artifact=False means we already validated last iteration."""
    state, adapter, results, _ = _build_state(monkeypatch, is_new=False)
    # The reuse path supplies run_inputs explicitly
    state.extras["run_inputs"] = {"thickness": 7.5}
    pipe = Pipeline(POST_BUILD_PHASES)
    state = await pipe.run(state)

    # Validation skipped
    call_names = [c[0] for c in adapter.calls]
    assert "validate_artifact" not in call_names
    # Execution used the explicit run_inputs
    prepare_call = next(c for c in adapter.calls if c[0] == "prepare_inputs")
    assert prepare_call[1][1] == {"thickness": 7.5}


@pytest.mark.asyncio
async def test_pipeline_target_diagnostic_runs_when_target_set(monkeypatch, capsys):
    """When target is set, ExecutionPhase prints vs-target diff."""
    state, _, _, _ = _build_state(
        monkeypatch,
        target={"bandgap_ev": 1.0},  # off by 12%
    )
    pipe = Pipeline(POST_BUILD_PHASES)
    await pipe.run(state)
    out = capsys.readouterr().out
    assert "vs target" in out
    assert "bandgap_ev" in out


@pytest.mark.asyncio
async def test_pipeline_no_target_skips_diagnostic(monkeypatch, capsys):
    state, _, _, _ = _build_state(monkeypatch)  # no target
    pipe = Pipeline(POST_BUILD_PHASES)
    await pipe.run(state)
    out = capsys.readouterr().out
    assert "vs target" not in out


@pytest.mark.asyncio
async def test_pipeline_emits_phase_events_when_hook_attached(monkeypatch):
    """Wire the phase_events hook and confirm phase boundaries are observed."""
    from arc.chat.events import set_sink, ChatEvent, Sink
    from arc.chat.research.hooks import phase_events

    class CapSink(Sink):
        def __init__(self): self.events = []
        def handle(self, ev): self.events.append(ev)
        def close(self): pass

    sink = CapSink()
    prev = set_sink(sink)
    try:
        state, _, _, _ = _build_state(monkeypatch)
        pipe = Pipeline(POST_BUILD_PHASES, hooks=phase_events())
        await pipe.run(state)
    finally:
        set_sink(prev)

    starts = [e.text for e in sink.events if e.kind == "phase_start"]
    ends   = [e.text for e in sink.events if e.kind == "phase_end"]
    # All five phases produced both events
    expected = {"validation", "execution", "review", "reflection", "provenance"}
    assert set(starts) == expected
    assert set(ends) == expected
