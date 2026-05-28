"""``run_research`` integration with the Phase 3 pipeline.

This is the regression test that proves the pipeline is actually
invoked by the live chat path (P3-1 fix).
"""

from types import SimpleNamespace

import pytest

from arc.chat.loop import run_research
from tests.fakes import make_artifact


pytestmark = pytest.mark.chat


@pytest.mark.asyncio
async def test_run_research_invokes_pipeline_phases(monkeypatch):
    """Patch the post-build pipeline and confirm run_research drives it.

    We patch ``Pipeline.run`` to capture the phases it was given so we
    don't need real adapters / agents."""
    captured = {}

    from arc.chat.research import pipeline as pipeline_mod

    real_init = pipeline_mod.Pipeline.__init__
    def tracking_init(self, phases, hooks=()):
        captured["phase_names"] = [p.name for p in phases]
        captured["hook_count"] = len(list(hooks))
        real_init(self, phases, hooks)
    monkeypatch.setattr(pipeline_mod.Pipeline, "__init__", tracking_init)

    # Make Pipeline.run a no-op that returns the input state with execution set
    real_run = pipeline_mod.Pipeline.run
    async def fake_run(self, state):
        # Mark it as having gone through ALL phases without exploding
        state.execution = SimpleNamespace(
            run_id="r-test", status="completed",
            outputs={"bandgap_ev": 1.0}, logs=[],
        )
        state.review = SimpleNamespace(
            approved=True, summary="ok", strengths=[], weaknesses=[],
            recommendations=[], next_parameters={},
            iteration_complete=True, strategy="stop",
            model_dump=lambda: {"approved": True},
        )
        state.reflection = "lessons"
        return state
    monkeypatch.setattr(pipeline_mod.Pipeline, "run", fake_run)

    # Stub out the packages so run_research doesn't try to call agents
    class _Stub:
        async def run(self, *args, **kwargs):
            from arc.schemas.research import ResearchProposal
            return ResearchProposal(
                hypothesis="h", objective="o", variables=[],
                methodology="m", expected_outcomes="e",
                evaluation_metrics=[],
            )

    class _PStub:
        async def run(self, *args, **kwargs):
            return SimpleNamespace(
                parameters={"x": 1}, parameter_constraints={},
                parameter_sweep={}, success_criteria=[],
                proposal=SimpleNamespace(variables=[]),
            )

    # We pass an existing artifact so the pre-build leg is skipped.
    artifact = make_artifact()
    workflow = SimpleNamespace(
        _context=SimpleNamespace(
            memory={"current_plan": SimpleNamespace(parameters={"x": 1})},
            iteration=0,
            session_id="sess",
        ),
        adapter=SimpleNamespace(),  # won't be called because Pipeline.run is faked
        provider=None,
        artifacts=SimpleNamespace(),
        results=SimpleNamespace(),
        provenance=SimpleNamespace(record=lambda **kw: None),
        registry=SimpleNamespace(),
        session_id="sess",
    )
    result = await run_research(workflow, "test goal", artifact=artifact)

    assert result is not None
    assert captured["phase_names"] == [
        "validation", "execution", "review", "reflection", "provenance",
    ]
    # Pipeline received the phase_events hook
    assert captured["hook_count"] >= 1


# Helper for the test above (defined after run_research because it imports it)
class _CapturingSink:
    def __init__(self):
        self.events = []
    def handle(self, ev):
        self.events.append(ev)
    def close(self):
        pass
