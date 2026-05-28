"""Integration tests for the arc-mars YAML workflows (TODO items 8/9/10/11).

These two workflows were declared in ``arc-mars/package.yaml`` but never
exercised end-to-end, so several agent input-contract mismatches went
unnoticed (the planner/reviewer expected a bare model but the workflow
feeds a wrapper dict; the improvement planner hardcoded an
``input_parameter`` name and crashed on a ``None`` history). These tests
run each workflow in stub mode so "declared workflow" now implies
"verified runnable".
"""

import asyncio

import pytest

from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ResearchGoal, ResearchProposal

pytestmark = pytest.mark.chat


def _proposal() -> dict:
    return ResearchProposal(
        hypothesis="h",
        objective="o",
        variables=["value"],
        methodology="m",
        expected_outcomes="e",
        evaluation_metrics=["q"],
    ).model_dump()


def _cap_iterations(wf: ResearchWorkflow, name: str, max_iterations: int = 2) -> None:
    """Re-register the workflow with a small max_iterations so the
    stub-mode loop (which never 'approves') doesn't run the full 10-20
    iterations — keeps the test fast while still exercising every step."""
    definition = dict(wf.registry.get_workflow(name))
    definition["config"] = {**definition.get("config", {}), "max_iterations": max_iterations}
    capped_name = f"{name}-capped"
    definition["name"] = capped_name
    wf.registry.register_workflow(capped_name, definition)
    wf.workflow_name = capped_name


def test_mars_research_loop_runs_end_to_end():
    wf = ResearchWorkflow(workflow_name="mars-research-loop")
    _cap_iterations(wf, "mars-research-loop")
    result = asyncio.run(
        wf.run_once(ResearchGoal(goal="Verify parameter doubling", target={"result": 2.0}))
    )
    # Every declared step must have executed.
    expected = {"decompose", "plan", "build", "validate", "run", "reflect", "improve", "review"}
    assert expected.issubset(set(result["steps"]))
    assert result["status"] in {"completed", "iteration_limit"}


def test_mars_iterative_improvement_runs_end_to_end():
    wf = ResearchWorkflow(workflow_name="mars-iterative-improvement")
    _cap_iterations(wf, "mars-iterative-improvement")
    wf._context.memory["current_plan"] = None
    wf._context.memory["last_result"] = ExecutionResult(
        run_id="seed", status="completed", outputs={"result": 1.0}
    ).model_dump()
    wf._context.memory["proposal"] = _proposal()

    result = asyncio.run(
        wf.run_once(ResearchGoal(goal="Improve the doubling result", target={"result": 2.0}))
    )
    expected = {"reflect", "improve", "build", "validate", "run", "compare", "review"}
    assert expected.issubset(set(result["steps"]))
    assert result["status"] in {"completed", "iteration_limit"}


# ── experiment_decomposer now returns multiple distinct sub-experiments ──


def test_experiment_decomposer_returns_multiple_distinct_proposals():
    # YAML-workflow-only agent (registry path), so import by file.
    from arc.core.loader import _import_class
    from arc.contracts.agent import AgentContext

    DecomposerCls = _import_class(
        "arc.packages.arc-mars.agents.experiment_decomposer:ExperimentDecomposerAgent"
    )
    agent = DecomposerCls(context=AgentContext(session_id="t", memory={}))
    out = asyncio.run(agent.run(ResearchGoal(goal="optimize the thing")))
    subs = out["sub_experiments"]
    assert len(subs) >= 2, "decomposer must return multiple sub-experiments"
    # They must be genuinely distinct, not one proposal repeated.
    hypotheses = {s["hypothesis"] for s in subs}
    assert len(hypotheses) == len(subs)
    assert out["primary_proposal"] == subs[0]


# ── improvement_planner is robust + uses real parameter names ────────────


def test_improvement_planner_handles_none_history_and_review_model():
    from arc.core.loader import _import_class
    from arc.contracts.agent import AgentContext
    from arc.schemas.review import ReviewResult

    Cls = _import_class(
        "arc.packages.arc-mars.agents.improvement_planner:ImprovementPlannerAgent"
    )
    agent = Cls(context=AgentContext(session_id="t", memory={}))

    review = ReviewResult(approved=False, summary="no", strategy="explore",
                          recommendations=["widen the sweep"])
    out = asyncio.run(agent.run({
        "reflection": review,           # a model, not a dict
        "history": None,                # must not crash
        "proposal": _proposal(),
    }))
    assert out["action"] in {"adjust_parameters", "widen_parameter_sweep", "modify_artifact"}
    assert "widen the sweep" in out["recommendations"]
    assert out["plan"] is not None
    # The plan must NOT use the old hardcoded "input_parameter" key.
    assert "input_parameter" not in out["plan"]["parameters"]


def test_improvement_planner_derives_param_names_from_plan():
    from arc.core.loader import _import_class
    from arc.contracts.agent import AgentContext

    Cls = _import_class(
        "arc.packages.arc-mars.agents.improvement_planner:ImprovementPlannerAgent"
    )
    agent = Cls(context=AgentContext(session_id="t", memory={}))

    out = asyncio.run(agent.run({
        "reflection": {"improving": False},
        "history": [{"parameters": {"thickness": 1.0}}, {"parameters": {"thickness": 2.0}}],
        "proposal": _proposal(),
        "plan": {
            "parameter_constraints": {"thickness": {"min": 0.5, "max": 5.0}},
        },
    }))
    # The swept parameter is the real schema name, not "input_parameter".
    assert "thickness" in out["plan"]["parameter_sweep"]
    assert "input_parameter" not in out["plan"]["parameter_sweep"]
    swept = out["plan"]["parameter_sweep"]["thickness"]
    assert all(0.5 <= v <= 5.0 for v in swept)
