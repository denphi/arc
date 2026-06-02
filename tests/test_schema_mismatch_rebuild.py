"""Schema-mismatch detection + auto-rebuild flow.

Scenario: the user asked for ``target = {bandgap_ev: 1.1}`` but the
running artifact only produces ``{result: 5.0}``. The reviewer can't
compute a target distance, approval always fails, and (before this
change) the chat looped indefinitely. The fix is:

  1. ``ExecutionPhase`` records the unmatched target keys on
     ``PipelineState.unmatched_target_keys``.
  2. ``run_research`` surfaces those keys in its return dict.
  3. ``_run_with_continuation`` reacts by dropping the artifact and
     seeding ``ctx.memory['required_outputs']`` so the next
     ``run_research`` rebuilds with the missing keys explicit.
"""

from types import SimpleNamespace

import pytest

from arc.chat.research.phases import ExecutionPhase
from arc.chat.research.pipeline import PipelineState
from tests.fakes import make_artifact, make_workflow

pytestmark = pytest.mark.chat


class _FakeAdapter:
    def __init__(self, outputs):
        self._outputs = outputs

    async def prepare_inputs(self, artifact, params):
        return dict(params)

    async def run(self, artifact, inputs):
        return SimpleNamespace(
            run_id="run-mismatch-1",
            status="completed",
            outputs=dict(self._outputs),
            logs=[],
        )


class _FakeResults:
    def __init__(self):
        self.saved = []
    def save(self, ex):
        self.saved.append(ex)


# ── ExecutionPhase populates unmatched_target_keys ────────────────────────


@pytest.mark.asyncio
async def test_execution_records_unmatched_target_keys():
    wf = make_workflow()
    wf.adapter = _FakeAdapter(outputs={"result": 5.0})
    wf.results = _FakeResults()
    state = PipelineState(
        workflow=wf,
        goal_text="x",
        artifact=make_artifact(),
        target={"bandgap_ev": 1.1},  # not in outputs
    )
    state.extras["run_inputs"] = {"thickness": 5.0}
    state = await ExecutionPhase().run(state)
    assert state.unmatched_target_keys == ["bandgap_ev"]


@pytest.mark.asyncio
async def test_execution_no_unmatched_when_all_keys_resolve():
    wf = make_workflow()
    wf.adapter = _FakeAdapter(outputs={"bandgap_ev": 1.1})
    wf.results = _FakeResults()
    state = PipelineState(
        workflow=wf,
        goal_text="x",
        artifact=make_artifact(),
        target={"bandgap_ev": 1.1},
    )
    state.extras["run_inputs"] = {}
    state = await ExecutionPhase().run(state)
    assert state.unmatched_target_keys == []


@pytest.mark.asyncio
async def test_execution_records_multiple_unmatched_targets():
    wf = make_workflow()
    wf.adapter = _FakeAdapter(outputs={"result": 1.0})
    wf.results = _FakeResults()
    state = PipelineState(
        workflow=wf,
        goal_text="x",
        artifact=make_artifact(),
        target={"bandgap_ev": 1.1, "compliance": 0.8},
    )
    state.extras["run_inputs"] = {}
    state = await ExecutionPhase().run(state)
    assert set(state.unmatched_target_keys) == {"bandgap_ev", "compliance"}


@pytest.mark.asyncio
async def test_execution_emits_schema_mismatch_event():
    """The mismatch is also surfaced as a structured event so the JSONL
    sink (and a future TUI) can react."""
    from arc.chat.events import Sink, set_sink

    class Cap(Sink):
        def __init__(self): self.events = []
        def handle(self, ev): self.events.append(ev)
        def close(self): pass

    sink = Cap()
    prev = set_sink(sink)
    try:
        wf = make_workflow()
        wf.adapter = _FakeAdapter(outputs={"result": 5.0})
        wf.results = _FakeResults()
        state = PipelineState(
            workflow=wf, goal_text="x",
            artifact=make_artifact(),
            target={"bandgap_ev": 1.1},
        )
        state.extras["run_inputs"] = {}
        await ExecutionPhase().run(state)
    finally:
        set_sink(prev)

    schema_events = [
        e for e in sink.events
        if e.kind == "warn" and "schema_mismatch" in e.text
    ]
    assert len(schema_events) == 1
    # The structured meta carries the actionable info
    meta = schema_events[0].meta
    assert meta["targets"] == ["bandgap_ev"]
    assert meta["outputs"] == ["result"]


# ── _run_with_continuation triggers the rebuild ──────────────────────────


@pytest.mark.asyncio
async def test_continuation_loop_rebuilds_on_schema_mismatch(monkeypatch):
    """When ``run_research`` reports unmatched_target_keys, the loop
    must clear the artifact, seed required_outputs, and re-enter
    ``run_research`` instead of bailing or looping uselessly."""
    from arc.chat.loop import _run_with_continuation

    iteration_calls = []

    async def fake_run_research(workflow, goal_text, artifact=None, refinement=None):
        iteration_calls.append({
            "artifact": artifact,
            "required_outputs": workflow._context.memory.get("required_outputs"),
        })
        # First iteration: schema mismatch
        if len(iteration_calls) == 1:
            return {
                "artifact": make_artifact(),
                "execution": SimpleNamespace(
                    status="completed",
                    outputs={"result": 5.0},
                    run_id="r1",
                ),
                "review": SimpleNamespace(
                    approved=False, iteration_complete=False, strategy="step",
                    next_parameters={}, summary="", strengths=[],
                    weaknesses=[], recommendations=[],
                ),
                "reflection": "",
                "unmatched_target_keys": ["bandgap_ev"],
            }
        # Second iteration: clean result, approved
        return {
            "artifact": make_artifact(),
            "execution": SimpleNamespace(
                status="completed",
                outputs={"bandgap_ev": 1.1},
                run_id="r2",
            ),
            "review": SimpleNamespace(
                approved=True, iteration_complete=True, strategy="stop",
                next_parameters={}, summary="ok", strengths=[],
                weaknesses=[], recommendations=[],
            ),
            "reflection": "lessons",
            "unmatched_target_keys": [],
        }
    monkeypatch.setattr("arc.chat.loop.run_research", fake_run_research)

    # Stub the post-approval menu so it doesn't prompt
    async def noop(*a, **kw): pass
    monkeypatch.setattr("arc.chat.loop._post_approval_menu", noop)

    wf = make_workflow(memory={"target": {"bandgap_ev": 1.1}})
    await _run_with_continuation(wf, "test goal", max_iterations=5,
                                  start_artifact=make_artifact())

    # Two iterations happened
    assert len(iteration_calls) == 2
    # The second iteration started with NO artifact (rebuild triggered)
    assert iteration_calls[1]["artifact"] is None
    # And required_outputs was seeded from the unmatched target
    assert iteration_calls[1]["required_outputs"] == ["bandgap_ev"]
    retry = wf._context.memory["last_retry_context"]
    assert retry["reason"] == "schema_mismatch"
    assert retry["required_outputs"] == ["bandgap_ev"]
    assert retry["actual_outputs"] == {"result": 5.0}
    assert wf._context.memory["retry_context"][-1] == retry


@pytest.mark.asyncio
async def test_continuation_loop_no_rebuild_when_targets_match(monkeypatch):
    """Happy path: no mismatch → no spurious rebuild trigger."""
    from arc.chat.loop import _run_with_continuation

    iteration_calls = []

    async def fake_run_research(workflow, goal_text, artifact=None, refinement=None):
        iteration_calls.append(artifact)
        return {
            "artifact": make_artifact(),
            "execution": SimpleNamespace(
                status="completed",
                outputs={"bandgap_ev": 1.1},
                run_id="r1",
            ),
            "review": SimpleNamespace(
                approved=True, iteration_complete=True, strategy="stop",
                next_parameters={}, summary="ok", strengths=[],
                weaknesses=[], recommendations=[],
            ),
            "reflection": "",
            "unmatched_target_keys": [],
        }
    monkeypatch.setattr("arc.chat.loop.run_research", fake_run_research)

    async def noop(*a, **kw): pass
    monkeypatch.setattr("arc.chat.loop._post_approval_menu", noop)

    wf = make_workflow(memory={"target": {"bandgap_ev": 1.1}})
    await _run_with_continuation(wf, "g", max_iterations=5,
                                  start_artifact=make_artifact())

    # Only one iteration ran (approved on first pass)
    assert len(iteration_calls) == 1
    # required_outputs was NOT touched
    assert "required_outputs" not in wf._context.memory


@pytest.mark.asyncio
async def test_continuation_loop_respects_iteration_budget_on_rebuild(monkeypatch):
    """If we're already at max_iterations, a mismatch shouldn't loop forever."""
    from arc.chat.loop import _run_with_continuation

    calls = []
    async def fake_run_research(workflow, goal_text, artifact=None, refinement=None):
        calls.append(1)
        return {
            "artifact": make_artifact(),
            "execution": SimpleNamespace(
                status="completed",
                outputs={"result": 5.0},
                run_id="rN",
            ),
            "review": SimpleNamespace(
                approved=False, iteration_complete=False, strategy="step",
                next_parameters={}, summary="", strengths=[],
                weaknesses=[], recommendations=[],
            ),
            "reflection": "",
            "unmatched_target_keys": ["bandgap_ev"],
        }
    monkeypatch.setattr("arc.chat.loop.run_research", fake_run_research)
    async def noop(*a, **kw): pass
    monkeypatch.setattr("arc.chat.loop._post_approval_menu", noop)

    wf = make_workflow(memory={"target": {"bandgap_ev": 1.1}})
    await _run_with_continuation(wf, "g", max_iterations=1,
                                  start_artifact=make_artifact())

    # Exactly one iteration (the budget) — no infinite rebuild loop
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_continuation_loop_merges_with_existing_required_outputs(monkeypatch):
    """If ctx.memory already has required_outputs, the new keys are
    merged in (order-preserving, deduped)."""
    from arc.chat.loop import _run_with_continuation

    captured = []
    async def fake_run_research(workflow, goal_text, artifact=None, refinement=None):
        captured.append(list(workflow._context.memory.get("required_outputs", [])))
        if len(captured) == 1:
            return {
                "artifact": make_artifact(),
                "execution": SimpleNamespace(
                    status="completed", outputs={"result": 5.0}, run_id="r",
                ),
                "review": SimpleNamespace(
                    approved=False, iteration_complete=False, strategy="step",
                    next_parameters={}, summary="", strengths=[],
                    weaknesses=[], recommendations=[],
                ),
                "reflection": "",
                "unmatched_target_keys": ["bandgap_ev", "compliance"],
            }
        return {
            "artifact": make_artifact(),
            "execution": SimpleNamespace(
                status="completed",
                outputs={"bandgap_ev": 1.1, "compliance": 0.8},
                run_id="r2",
            ),
            "review": SimpleNamespace(
                approved=True, iteration_complete=True, strategy="stop",
                next_parameters={}, summary="ok", strengths=[],
                weaknesses=[], recommendations=[],
            ),
            "reflection": "",
            "unmatched_target_keys": [],
        }
    monkeypatch.setattr("arc.chat.loop.run_research", fake_run_research)
    async def noop(*a, **kw): pass
    monkeypatch.setattr("arc.chat.loop._post_approval_menu", noop)

    wf = make_workflow(memory={
        "target": {"bandgap_ev": 1.1, "compliance": 0.8},
        "required_outputs": ["pre_existing_key"],
    })
    await _run_with_continuation(wf, "g", max_iterations=5,
                                  start_artifact=make_artifact())

    # First iteration saw the original list; second saw the merge
    assert captured[0] == ["pre_existing_key"]
    # Merge preserves order, no dupes
    assert wf._context.memory["required_outputs"] == [
        "pre_existing_key", "bandgap_ev", "compliance",
    ]
