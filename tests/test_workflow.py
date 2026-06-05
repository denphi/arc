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


class SpyBackend:
    def __init__(self):
        self.registered = []
        self.persisted = []
        self.recorded = []

    def is_active(self):
        return True

    async def register_artifact(self, artifact):
        self.registered.append(artifact)
        return {"registered": True, "backend": "spy"}

    async def persist_result(self, artifact, execution, inputs):
        self.persisted.append((artifact, execution, inputs))
        return {"persisted": True, "backend": "spy"}

    async def record_execution(self, artifact, execution, inputs, outputs):
        self.recorded.append((artifact, execution, inputs, outputs))
        return {"recorded": True, "backend": "spy"}


class EchoSkill:
    async def execute(self, inputs, context):
        return inputs


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
async def test_yaml_workflow_calls_backend_publish_actions():
    workflow = ResearchWorkflow()
    backend = SpyBackend()
    workflow.backend = backend

    result = await workflow.run_once(
        ResearchGoal(goal="Verify backend publish hooks", target={"result": 2.0})
    )

    assert result["status"] == "completed"
    assert len(backend.registered) == 1
    assert len(backend.persisted) >= 1
    assert len(backend.recorded) >= 1
    artifact, execution, inputs = backend.persisted[-1]
    assert artifact is backend.registered[-1]
    assert execution.run_id == result["execution"]["run_id"]
    assert isinstance(inputs, dict)


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


# ── YAML engine honours the strategy resolver (TODO item 7) ─────────────


def test_resolve_agent_class_honours_session_override():
    """A ``/strategy``-style override on the session memory must change
    which class the YAML engine resolves for a role — the chat path
    already did this; the YAML path now does too."""
    workflow = ResearchWorkflow()

    default_cls = workflow._resolve_agent_class("reviewer")
    assert default_cls.__name__ == "ReviewerAgent"

    workflow._context.memory["strategy_overrides"] = {"reviewer": "reflective"}
    overridden = workflow._resolve_agent_class("reviewer")
    assert overridden.__name__ == "ReflectiveReviewerAgent"
    assert overridden is not default_cls


def test_resolve_agent_class_unknown_name_falls_back_to_registry():
    """Non-role agent names (package-specific agents) still go through the
    registry — the resolver only owns the catalogue roles."""
    workflow = ResearchWorkflow()

    class _PkgAgent:
        def __init__(self, context=None):
            self.context = context

    workflow.registry.register_agent("experiment_decomposer", _PkgAgent)
    assert workflow._resolve_agent_class("experiment_decomposer") is _PkgAgent


def test_resolve_agent_class_bad_override_falls_back_not_raises():
    """A nonsense override must not make a runnable workflow unrunnable —
    resolve_role falls back to the catalogue default internally, so the
    engine still gets a usable class."""
    workflow = ResearchWorkflow()
    workflow._context.memory["strategy_overrides"] = {"reviewer": "does-not-exist"}
    cls = workflow._resolve_agent_class("reviewer")
    assert cls.__name__ == "ReviewerAgent"  # fell back to default


@pytest.mark.asyncio
async def test_yaml_engine_runs_with_reviewer_override():
    """End-to-end: a reviewer override flows through run_once's YAML
    engine and the run still completes."""
    workflow = ResearchWorkflow()
    workflow._context.memory["strategy_overrides"] = {"reviewer": "reflective"}
    result = await workflow.run_once(
        ResearchGoal(goal="Verify override path", target={"result": 2.0})
    )
    assert result["status"] in {"completed", "iteration_limit"}
    assert "review" in result


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


@pytest.mark.asyncio
async def test_workflow_inputs_root_and_mapping_refs(tmp_path):
    registry = ComponentRegistry()
    registry.register_skill("echo", EchoSkill())
    workflow = ResearchWorkflow(registry=registry, workflow_name="input-loop")
    paper = tmp_path / "paper.txt"
    paper.write_text("paper", encoding="utf-8")
    asset = workflow.file_store.import_file(paper, role="paper", session_id=workflow.session_id)
    workflow.registry.register_workflow(
        "input-loop",
        {
            "name": "input-loop",
            "inputs": {
                "paper": {"type": "file", "required": True},
                "benchmark_locator": {"type": "string", "required": True},
                "format": {"type": "string", "default": "json"},
            },
            "steps": [
                {
                    "id": "echo",
                    "skill": "echo",
                    "input": {
                        "paper": "inputs.paper",
                        "benchmark": "inputs.benchmark_locator",
                        "format": "inputs.format",
                        "goal": "inputs.goal",
                    },
                }
            ],
        },
    )

    result = await workflow.run_once(
        ResearchGoal(
            goal="Replicate section 5.4",
            constraints={"paper": asset.id, "benchmark_locator": "section 5.4"},
        )
    )

    output = result["steps"]["echo"]
    assert output == {
        "paper": asset.id,
        "benchmark": "section 5.4",
        "format": "json",
        "goal": "Replicate section 5.4",
    }


@pytest.mark.asyncio
async def test_workflow_steps_root_resolves_previous_outputs():
    registry = ComponentRegistry()
    registry.register_skill("echo", EchoSkill())
    workflow = ResearchWorkflow(registry=registry, workflow_name="steps-root-loop")
    workflow.registry.register_workflow(
        "steps-root-loop",
        {
            "name": "steps-root-loop",
            "steps": [
                {"id": "first", "skill": "echo", "input": {"value": 7}},
                {
                    "id": "second",
                    "skill": "echo",
                    "input": {
                        "old_style": "first.output.value",
                        "explicit_root": "steps.first.output.value",
                    },
                },
            ],
        },
    )

    result = await workflow.run_once(ResearchGoal(goal="Check step refs"))

    assert result["steps"]["second"] == {
        "old_style": 7,
        "explicit_root": 7,
    }


@pytest.mark.asyncio
async def test_workflow_condition_ordering_requires_numeric_operands():
    registry = ComponentRegistry()
    registry.register_skill("echo", EchoSkill())
    workflow = ResearchWorkflow(registry=registry, workflow_name="bad-condition-loop")
    workflow.registry.register_workflow(
        "bad-condition-loop",
        {
            "name": "bad-condition-loop",
            "steps": [{"id": "first", "skill": "echo", "input": {"value": "abc"}}],
            "conditions": [
                {"after": "first", "if": "steps.first.output.value > 3", "goto": "first"}
            ],
        },
    )

    with pytest.raises(ValueError, match="non-numeric operands"):
        await workflow.run_once(ResearchGoal(goal="Check condition"))


def test_workflow_input_precedence_constraints_shadow_goal_fields():
    workflow = ResearchWorkflow(registry=ComponentRegistry(), workflow_name="unused")

    bound = workflow._bind_workflow_inputs(
        {},
        ResearchGoal(
            goal="real goal",
            domain="real domain",
            constraints={"goal": "constraint goal", "domain": "constraint domain"},
            target={"mode": "target mode"},
        ),
    )

    assert bound["goal"] == "constraint goal"
    assert bound["domain"] == "constraint domain"
    assert bound["mode"] == "target mode"


@pytest.mark.asyncio
async def test_workflow_required_input_missing_fails_before_steps():
    registry = ComponentRegistry()
    registry.register_skill("echo", EchoSkill())
    workflow = ResearchWorkflow(registry=registry, workflow_name="missing-input-loop")
    workflow.registry.register_workflow(
        "missing-input-loop",
        {
            "name": "missing-input-loop",
            "inputs": {"paper": {"type": "file", "required": True}},
            "steps": [{"id": "echo", "skill": "echo", "input": "inputs.paper"}],
        },
    )

    with pytest.raises(ValueError, match="Missing required workflow input"):
        await workflow.run_once(ResearchGoal(goal="Missing paper"))
