"""Validator role — post-execution grading of run outputs.

Pins three things:

  * The default ``NoopValidatorAgent`` returns ``passed=True`` without
    side effects, so the loop's behaviour is unchanged for callers that
    didn't opt in.
  * The materials validator wraps the existing arc-materials evaluators
    and reports per-evaluator verdicts on the ``ValidatorReport``.
  * ``ExecutionPhase`` invokes the resolved validator, stores the report
    on ``state.extras["validator_report"]``, and surfaces warnings on
    out-of-range outputs.

The materials evaluators (band gap, formation energy, …) have been
declared in package.yaml for several releases but never invoked — this
test suite is the first place that exercises them through the loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.schemas.research import ValidatorReport


pytestmark = pytest.mark.chat


# ── Noop default ───────────────────────────────────────────────────────


def test_noop_validator_passes_anything():
    from arc.packages.arc_sim2l_agents.validator import NoopValidatorAgent

    agent = NoopValidatorAgent()
    report = asyncio.run(agent.validate({"band_gap": -99.0}))  # absurd
    assert isinstance(report, ValidatorReport)
    assert report.passed is True
    assert report.errors == []
    assert report.warnings == []
    assert report.evaluations == {}


def test_noop_validator_run_alias_accepts_outputs_dict():
    """``run({outputs, target})`` matches the AgentContract abstract method."""
    from arc.packages.arc_sim2l_agents.validator import NoopValidatorAgent

    agent = NoopValidatorAgent()
    report = asyncio.run(agent.run({"outputs": {"x": 1}, "target": {"x": 1}}))
    assert report.passed is True


def test_validator_default_resolves_to_noop():
    from arc.core.strategies import resolve_role
    cls = resolve_role("validator")
    assert cls.__name__ == "NoopValidatorAgent"


def test_validator_override_resolves_to_materials_validator():
    from arc.core.strategies import resolve_role
    cls = resolve_role(
        "validator", overrides={"validator": "materials_evaluators"},
    )
    assert cls.__name__ == "MaterialsValidatorAgent"


# ── Materials validator ────────────────────────────────────────────────


def _materials_validator():
    from arc.core.strategies import resolve_role
    cls = resolve_role(
        "validator", overrides={"validator": "materials_evaluators"},
    )
    return cls()


def test_materials_validator_passes_in_range_bandgap():
    report = asyncio.run(_materials_validator().validate({"band_gap": 1.1}))
    assert report.passed is True
    assert "BandgapEvaluator" in report.evaluations
    assert report.evaluations["BandgapEvaluator"]["classification"] == "semiconductor"


def test_materials_validator_fails_negative_bandgap():
    report = asyncio.run(_materials_validator().validate({"band_gap": -0.5}))
    assert report.passed is False
    assert report.errors
    assert "outside physical range" in report.errors[0]


def test_materials_validator_fails_absurdly_large_bandgap():
    report = asyncio.run(_materials_validator().validate({"band_gap": 50.0}))
    assert report.passed is False


def test_materials_validator_warns_on_marginal_formation_energy():
    """0 ± 0.05 eV/atom is in range but worth a warning."""
    report = asyncio.run(_materials_validator().validate(
        {"formation_energy": 0.01},
    ))
    assert report.passed is True
    assert report.warnings
    assert "marginal" in report.warnings[0].lower() or "near zero" in report.warnings[0].lower()


def test_materials_validator_runs_every_applicable_evaluator():
    """Outputs touching multiple evaluators get one entry each."""
    outputs = {"band_gap": 1.1, "formation_energy": -1.5}
    report = asyncio.run(_materials_validator().validate(outputs))
    assert "BandgapEvaluator" in report.evaluations
    assert "FormationEnergyEvaluator" in report.evaluations
    assert report.passed is True


def test_materials_validator_skips_non_materials_outputs():
    """A polymer run shouldn't be marked failed just because it lacks band_gap."""
    report = asyncio.run(_materials_validator().validate({"glass_transition_k": 450.0}))
    assert report.passed is True
    assert report.warnings  # informational "no materials-domain outputs"
    assert "skipping" in report.warnings[0].lower()


def test_materials_validator_handles_empty_outputs():
    report = asyncio.run(_materials_validator().validate({}))
    assert report.passed is False
    assert report.errors


def test_materials_validator_handles_non_numeric_bandgap():
    """A bandgap returned as a string is a soft error, not a crash."""
    report = asyncio.run(_materials_validator().validate({"band_gap": "wat"}))
    assert report.passed is False
    assert any("not numeric" in e.lower() for e in report.errors)


# ── ExecutionPhase wiring ──────────────────────────────────────────────


def _build_execution_state(
    *,
    target=None,
    outputs=None,
    validator_override=None,
    artifact_inputs=None,
):
    """Build a PipelineState wired to a stub adapter so ExecutionPhase.run
    has everything it needs end-to-end."""
    from arc.chat.research.pipeline import PipelineState
    from arc.schemas.execution import ExecutionResult

    async def _prepare_inputs(artifact, run_inputs):
        return run_inputs or artifact_inputs or {"thickness": 5.0}

    async def _run(artifact, inputs):
        return ExecutionResult(
            run_id="exec-test-1",
            status="completed",
            outputs=outputs if outputs is not None else {"bandgap_ev": 1.1},
            logs=[],
        )

    adapter = SimpleNamespace(prepare_inputs=_prepare_inputs, run=_run)
    results = SimpleNamespace(save=lambda exec: None)
    memory: dict = {}
    if validator_override:
        memory["strategy_overrides"] = {"validator": validator_override}
    workflow = SimpleNamespace(
        adapter=adapter,
        results=results,
        _context=SimpleNamespace(memory=memory),
    )
    artifact = SimpleNamespace(
        artifact_id="art-1",
        metadata={"sim2l_inputs": {"thickness": {}}},
    )
    state = PipelineState(
        workflow=workflow,
        goal_text="design a silicon nanowire bandgap of 1.1 eV",
        artifact=artifact,
        is_new_artifact=True,
    )
    if target:
        state.target = dict(target)
    if artifact_inputs:
        state.extras["run_inputs"] = artifact_inputs
    return state


def test_execution_phase_uses_default_validator_when_none_chosen():
    """No override → noop validator → report on extras with passed=True."""
    from arc.chat.research.phases import ExecutionPhase

    state = _build_execution_state(
        target={"bandgap_ev": 1.1},
        outputs={"bandgap_ev": 1.1},
    )
    asyncio.run(ExecutionPhase().run(state))
    report = state.extras.get("validator_report")
    assert report is not None
    assert report.passed is True


def test_execution_phase_applies_materials_validator_when_overridden(capsys):
    """Materials validator runs and the per-evaluator line lands on stdout."""
    from arc.chat.research.phases import ExecutionPhase

    state = _build_execution_state(
        outputs={"band_gap": 1.1},
        validator_override="materials_evaluators",
    )
    asyncio.run(ExecutionPhase().run(state))
    report = state.extras.get("validator_report")
    assert report.passed is True
    assert "BandgapEvaluator" in report.evaluations

    captured = capsys.readouterr().out
    assert "band_gap=1.1" in captured  # per-evaluator inline display


def test_execution_phase_surfaces_validator_errors(capsys):
    """An out-of-range bandgap prints an error and the report is failed."""
    from arc.chat.research.phases import ExecutionPhase

    state = _build_execution_state(
        outputs={"band_gap": -0.5},
        validator_override="materials_evaluators",
    )
    asyncio.run(ExecutionPhase().run(state))
    report = state.extras.get("validator_report")
    assert report.passed is False

    captured = capsys.readouterr().out
    assert "validator" in captured
    assert "outside physical range" in captured


def test_execution_phase_validator_failure_does_not_abort_pipeline():
    """A failing validator must not set state.aborted — that's the
    reviewer's call, not the validator's."""
    from arc.chat.research.phases import ExecutionPhase

    state = _build_execution_state(
        outputs={"band_gap": -0.5},
        validator_override="materials_evaluators",
    )
    asyncio.run(ExecutionPhase().run(state))
    assert state.aborted is False
    assert state.execution is not None


def test_execution_phase_skips_validator_when_outputs_empty():
    """No outputs → no validator call (it would error anyway). The
    report key is absent from extras so the reviewer falls through."""
    from arc.chat.research.phases import ExecutionPhase

    state = _build_execution_state(outputs={})
    asyncio.run(ExecutionPhase().run(state))
    assert "validator_report" not in state.extras


# ── Recipe integration ────────────────────────────────────────────────


def test_recipe_can_reference_validator_role():
    """A new recipe naming validator=materials_evaluators must validate."""
    from arc.core.recipes import Recipe, validate_recipe

    r = Recipe(
        name="materials-strict",
        description="strict materials validation",
        source="user",
        strategies={
            "validator": "materials_evaluators",
            "optimizer": "bayesopt",
        },
    )
    assert validate_recipe(r) == []


def test_apply_recipe_writes_validator_override(monkeypatch):
    """End-to-end: apply a recipe with validator role → ExecutionPhase
    picks the materials validator."""
    import asyncio
    from arc.chat.commands.recipe import run as recipe_run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes
    from arc.core.recipes import Recipe
    from arc.packages import resolve_role
    from tests.fakes import make_workflow

    # Inject a one-off recipe by monkeypatching list_recipes.
    custom = Recipe(
        name="custom-materials",
        description="",
        source="user",
        strategies={"validator": "materials_evaluators"},
    )
    monkeypatch.setattr(_recipes, "list_recipes", lambda: [custom])
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    state = ChatState(workflow=make_workflow())
    asyncio.run(recipe_run(state, ["apply", "custom-materials"]))

    cls = resolve_role("validator", state.workflow)
    assert cls.__name__ == "MaterialsValidatorAgent"
