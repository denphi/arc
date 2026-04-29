"""Unit tests for individual agents."""

import sys
from pathlib import Path

import pytest

# Ensure the repository root is on sys.path for the arc package.
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arc.contracts.agent import AgentContext
from arc.packages import load_ideator, load_planner, load_builder, load_reviewer
from arc.schemas.research import ResearchGoal, ResearchProposal, ExperimentPlan
from arc.schemas.execution import ExecutionResult


@pytest.fixture
def context():
    return AgentContext(session_id="test-session")


@pytest.mark.asyncio
async def test_ideator_returns_proposal(context):
    IdeatorAgent = load_ideator().IdeatorAgent
    agent = IdeatorAgent(context=context)
    goal = ResearchGoal(goal="test goal", domain="physics")
    result = await agent.run(goal)
    assert isinstance(result, ResearchProposal)
    assert result.hypothesis
    assert result.objective
    assert len(result.variables) > 0


@pytest.mark.asyncio
async def test_planner_returns_experiment_plan(context):
    PlannerAgent = load_planner().PlannerAgent
    agent = PlannerAgent(context=context)
    proposal = ResearchProposal(
        hypothesis="Test hypothesis",
        objective="Test objective",
        variables=["x", "y"],
        methodology="simulate",
        expected_outcomes="y > 0",
        evaluation_metrics=["output_quality"],
    )
    result = await agent.run(proposal)
    assert isinstance(result, ExperimentPlan)
    assert result.parameters
    assert result.success_criteria


@pytest.mark.asyncio
async def test_builder_generates_files(context):
    PlannerAgent = load_planner().PlannerAgent
    Sim2LBuilderAgent = load_builder().Sim2LBuilderAgent
    planner = PlannerAgent(context=context)
    proposal = ResearchProposal(
        hypothesis="Test",
        objective="test",
        variables=["x"],
        methodology="compute",
        expected_outcomes="x*2",
        evaluation_metrics=["result"],
    )
    plan = await planner.run(proposal)
    builder = Sim2LBuilderAgent(context=context)
    draft = await builder.run(plan)
    assert "workflow.py" in draft.files
    assert "sim2l.yaml" in draft.files


@pytest.mark.asyncio
async def test_reviewer_approves_good_result(context):
    ReviewerAgent = load_reviewer().ReviewerAgent
    agent = ReviewerAgent(context=context)
    result = ExecutionResult(
        run_id="test-run",
        status="completed",
        outputs={"result": 2.0},
        metrics={"execution_success": True},
    )
    review = await agent.run(result)
    assert review.approved is True


@pytest.mark.asyncio
async def test_reviewer_rejects_failed_result(context):
    ReviewerAgent = load_reviewer().ReviewerAgent
    agent = ReviewerAgent(context=context)
    result = ExecutionResult(
        run_id="test-run",
        status="error",
        outputs={},
    )
    review = await agent.run(result)
    assert review.approved is False


def test_builder_rejects_hanging_simulate():
    builder = load_builder()
    ok, reason = builder._validate_simulate(
        "def simulate(**inputs):\n"
        "    while True:\n"
        "        pass\n"
        "    return {'result': 1}\n"
    )
    assert ok is False
    assert "timed out" in reason
