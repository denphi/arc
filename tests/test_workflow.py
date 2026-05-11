"""Integration tests for the core research workflow."""

import pytest

from arc.core.registry import ComponentRegistry
from arc.orchestrator.workflow import ResearchWorkflow
from arc.runtime.local import LocalRuntimeAdapter
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ResearchGoal


@pytest.mark.asyncio
async def test_run_once_completes():
    workflow = ResearchWorkflow()
    goal = ResearchGoal(
        goal="Test that input_parameter doubles to produce result",
        target={"result": 2.0},
    )
    result = await workflow.run_once(goal)

    assert result["status"] == "completed"
    assert "proposal" in result
    assert "plan" in result
    assert "artifact" in result
    assert "execution" in result
    assert "review" in result
    assert result["workflow"] == "research-loop"
    assert "validate" in result["steps"]


@pytest.mark.asyncio
async def test_run_once_execution_produces_output():
    workflow = ResearchWorkflow()
    goal = ResearchGoal(goal="Verify parameter doubling", domain="test", target={"result": 2.0})
    result = await workflow.run_once(goal)

    execution = result["execution"]
    assert execution["status"] == "completed"
    assert "result" in execution["outputs"]


@pytest.mark.asyncio
async def test_review_not_approved_without_target():
    workflow = ResearchWorkflow()
    goal = ResearchGoal(goal="Simple computation test")
    result = await workflow.run_once(goal)

    assert result["status"] == "iteration_limit"
    review = result["review"]
    assert review["approved"] is False
    assert review["iteration_complete"] is False


class MarkerAdapter(LocalRuntimeAdapter):
    async def run(self, artifact, inputs):
        return ExecutionResult(
            run_id="marker-run",
            status="completed",
            outputs={"result": 42},
            metrics={"marker_adapter": True, **inputs},
        )


class FlakyAdapter(LocalRuntimeAdapter):
    attempts = 0

    async def run(self, artifact, inputs):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("temporary failure")
        return await super().run(artifact, inputs)


@pytest.mark.asyncio
async def test_workflow_uses_declared_adapter():
    workflow = ResearchWorkflow()
    workflow.registry.register_adapter("marker", MarkerAdapter)
    definition = dict(workflow.registry.get_workflow("research-loop"))
    definition["name"] = "marker-loop"
    definition["steps"] = [
        {**step, "adapter": "marker"} if step.get("id") == "run" else step
        for step in definition["steps"]
    ]
    workflow.registry.register_workflow("marker-loop", definition)
    workflow.workflow_name = "marker-loop"

    result = await workflow.run_once(
        ResearchGoal(goal="Use marker adapter", target={"result": 42})
    )
    assert result["execution"]["outputs"]["result"] == 42
    assert result["execution"]["metrics"]["marker_adapter"] is True


@pytest.mark.asyncio
async def test_workflow_retries_retry_steps():
    FlakyAdapter.attempts = 0
    workflow = ResearchWorkflow()
    workflow.registry.register_adapter("flaky", FlakyAdapter)
    definition = dict(workflow.registry.get_workflow("research-loop"))
    definition["name"] = "flaky-loop"
    definition["steps"] = [
        {**step, "adapter": "flaky", "on_error": "retry", "retry_max": 1}
        if step.get("id") == "run" else step
        for step in definition["steps"]
    ]
    workflow.registry.register_workflow("flaky-loop", definition)
    workflow.workflow_name = "flaky-loop"

    result = await workflow.run_once(
        ResearchGoal(goal="Retry adapter", target={"result": 2.0})
    )
    assert result["execution"]["status"] == "completed"
    assert FlakyAdapter.attempts == 2


@pytest.mark.asyncio
async def test_workflow_name_selects_registered_workflow():
    registry = ComponentRegistry()
    workflow = ResearchWorkflow(registry=registry, workflow_name="custom-loop")
    workflow.registry.register_workflow(
        "custom-loop",
        {
            "name": "custom-loop",
            "config": {"max_iterations": 1},
            "steps": [],
        },
    )
    result = await workflow.run_once(ResearchGoal(goal="Custom workflow"))
    assert result["workflow"] == "custom-loop"
