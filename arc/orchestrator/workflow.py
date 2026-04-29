"""Core research workflow orchestrator.

Wires together agents, adapter, registry, and stores for a single iteration.
The provider is optional — without it, agents use deterministic stub logic.
"""

import logging
import os
from pathlib import Path

from arc.contracts.agent import AgentContext
from arc.core.loader import load_packages
from arc.core.registry import ComponentRegistry
from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.provenance import ProvenanceLog
from arc.memory.results_store import ResultsStore
from arc.schemas.artifact import ArtifactDraft, ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ResearchGoal
from arc.schemas.review import ReviewResult
from arc.session import session_paths


def _build_adapter(db_path: str | None = None, session_id: str | None = None):
    """Use Sim2LRuntimeAdapter when sim2l is installed, otherwise fall back to local stub."""
    try:
        import sim2l  # noqa: F401
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)
    except ImportError:
        from arc.runtime.local import LocalRuntimeAdapter
        logger.info("sim2l not found — using LocalRuntimeAdapter (stub)")
        return LocalRuntimeAdapter()


def _instantiate_adapter(adapter_class, db_path: str | None = None, session_id: str | None = None):
    """Instantiate an adapter class while tolerating smaller constructor signatures."""
    try:
        return adapter_class(db_path=db_path, session_id=session_id)
    except TypeError:
        try:
            return adapter_class(session_id=session_id)
        except TypeError:
            return adapter_class()

logger = logging.getLogger(__name__)


def _default_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    root_config = Path(__file__).resolve().parents[2] / "arc.toml"
    package_config = Path(__file__).resolve().parents[1] / "arc.toml"
    config_path = root_config if root_config.exists() else package_config
    package_paths: list[str] = []
    if config_path.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        with config_path.open("rb") as f:
            config = tomllib.load(f)
        base = config_path.parent
        package_paths = [
            str((base / path).resolve()) if not Path(path).is_absolute() else path
            for path in config.get("packages", {}).get("paths", [])
        ]
    load_packages(package_paths, registry)
    return registry


def _build_provider(
    provider_name: str | None = None,
    token: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
):
    name = provider_name or os.environ.get("ARC_PROVIDER", "")
    if name == "anthropic":
        from arc.providers.anthropic.provider import AnthropicProvider
        return AnthropicProvider(
            model=model or os.environ.get("ARC_MODEL", "claude-opus-4-7"),
            api_key=token,
        )
    if name == "openai":
        from arc.providers.openai.provider import OpenAIProvider
        return OpenAIProvider(
            model=model or os.environ.get("ARC_MODEL", "gpt-4.1"),
            api_key=token,
        )
    if name == "openwebui":
        from arc.providers.openwebui.provider import OpenWebUIProvider
        return OpenWebUIProvider(
            base_url=base_url,
            token=token,
            model=model,
        )
    return None


class ResearchWorkflow:
    def __init__(
        self,
        provider_name: str | None = None,
        token: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        session_id: str | None = None,
        workflow_name: str = "research-loop",
        registry: ComponentRegistry | None = None,
    ):
        from arc.session import new_session_id as _new_sid
        self.session_id = session_id or _new_sid()
        self.workflow_name = workflow_name

        # All session files always go to ~/.sim2l/code/<session_id>/.
        paths = session_paths(self.session_id)
        artifact_root   = paths["artifacts"]
        results_root    = paths["runs"]
        provenance_path = paths["provenance"]
        db_path         = paths["db"]
        self._db_path = db_path

        self.adapter = _build_adapter(db_path=db_path, session_id=self.session_id)
        self.artifacts = ArtifactRegistry(root=artifact_root)
        self.results = ResultsStore(root=results_root)
        self.provenance = ProvenanceLog(log_path=provenance_path)
        self.registry = registry or _default_registry()
        self.provider = _build_provider(
            provider_name=provider_name,
            token=token,
            model=model,
            base_url=base_url,
        )

        self._context = AgentContext(
            session_id=self.session_id,
            memory={
                "provider": self.provider,
                "registry": self.artifacts,
                "results": self.results,
                "provenance": self.provenance,
                "adapter": self.adapter,
            },
        )

    def _agent(self, agent_class):
        return agent_class(context=self._context)

    def _dump(self, value):
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if isinstance(value, dict):
            return {k: self._dump(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._dump(v) for v in value]
        return value

    def _resolve_ref(self, ref, state: dict, workflow_config: dict):
        if isinstance(ref, dict):
            return {k: self._resolve_ref(v, state, workflow_config) for k, v in ref.items()}
        if isinstance(ref, list):
            return [self._resolve_ref(v, state, workflow_config) for v in ref]
        if not isinstance(ref, str):
            return ref
        if ref == "user_goal":
            return state["user_goal"]
        roots = {
            "memory": self._context.memory,
            "context": self._context.memory,
            "config": workflow_config,
        }
        parts = ref.split(".")
        if len(parts) >= 2 and parts[0] in state["steps"] and parts[1] == "output":
            value = state["steps"][parts[0]]["output"]
            for part in parts[2:]:
                value = self._get_field(value, part)
            return value
        if parts[0] in roots:
            value = roots[parts[0]]
            for part in parts[1:]:
                value = self._get_field(value, part)
            return value
        return ref

    def _get_field(self, value, field: str):
        if isinstance(value, dict):
            return value.get(field)
        return getattr(value, field, None)

    def _condition_matches(self, expression: str, state: dict) -> bool:
        if "==" not in expression:
            return False
        left, right = [part.strip() for part in expression.split("==", 1)]
        value = self._resolve_ref(left, state, {})
        expected = right.lower()
        if expected in {"false", "true"}:
            return bool(value) is (expected == "true")
        return str(value) == right.strip('"\'')

    async def _execute_skill(self, name: str, input_data, state: dict):
        if name == "validate-sim2l":
            artifact = input_data
            if isinstance(input_data, ArtifactDraft):
                artifact = self.artifacts.register(input_data)
                self._context.memory["current_artifact"] = artifact
            validation = await self.adapter.validate_artifact(artifact)
            if not validation.valid:
                raise ValueError("; ".join(validation.errors))
            return artifact
        if name == "write-artifact":
            if isinstance(input_data, ArtifactDraft):
                artifact = self.artifacts.register(input_data)
                self._context.memory["current_artifact"] = artifact
                return artifact
            if isinstance(input_data, ArtifactRecord):
                self._context.memory["current_artifact"] = input_data
            return input_data
        if name == "improve-artifact":
            return {
                "status": "skipped",
                "reason": "No built-in improve implementation",
                "input": self._dump(input_data),
            }
        skill = self.registry.get_skill(name)
        return await skill.execute(
            input_data if isinstance(input_data, dict) else {"input": self._dump(input_data)},
            self._context,
        )

    async def _execute_workflow_step(self, step: dict, state: dict, workflow_config: dict):
        input_data = self._resolve_ref(step.get("input", "user_goal"), state, workflow_config)
        if "agent" in step:
            agent = self._agent(self.registry.get_agent(step["agent"]))
            if step["agent"] == "reflector" and "run" in state["steps"]:
                return await agent.run(input_data, execution=state["steps"]["run"]["output"])
            return await agent.run(input_data)
        if "skill" in step:
            return await self._execute_skill(step["skill"], input_data, state)
        if "adapter" in step:
            method_name = step.get("method", "run")
            adapter = self._adapter_for_step(step)
            method = getattr(adapter, method_name)
            if isinstance(input_data, dict) and "artifact" in input_data:
                artifact = input_data["artifact"]
                parameters = input_data.get("parameters", {})
                prepared = await adapter.prepare_inputs(artifact, parameters)
                state["prepared_inputs"] = prepared
                result = await method(artifact, prepared)
                if isinstance(result, ExecutionResult):
                    result_path = self.results.save(result)
                    state["result_path"] = result_path
                    self._context.memory.setdefault("run_history", []).append({
                        "run_id": result.run_id,
                        "inputs": prepared,
                        "outputs": result.outputs,
                        "metrics": result.metrics,
                    })
                return result
            return await method(input_data)
        raise ValueError(f"Unsupported workflow step: {step}")

    def _adapter_for_step(self, step: dict):
        adapter_name = step.get("adapter")
        if not adapter_name:
            return self.adapter
        try:
            adapter_class = self.registry.get_adapter(adapter_name)
        except KeyError:
            if adapter_name == type(self.adapter).__name__:
                return self.adapter
            raise
        if isinstance(self.adapter, adapter_class):
            return self.adapter
        return _instantiate_adapter(adapter_class, db_path=self._db_path, session_id=self.session_id)

    async def _execute_step_with_policy(self, step: dict, state: dict, workflow_config: dict):
        retry_max = int(step.get("retry_max", 0) or 0)
        attempts = retry_max + 1 if step.get("on_error") == "retry" else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._execute_workflow_step(step, state, workflow_config)
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
        if step.get("on_error") == "skip":
            return {"status": "skipped", "error": str(last_exc)}
        raise last_exc or RuntimeError(f"Step failed: {step.get('id')}")

    async def _run_workflow_definition(self, workflow: dict, goal: ResearchGoal) -> dict:
        session_id = self._context.session_id
        workflow_config = workflow.get("config", {})
        max_iterations = int(workflow_config.get("max_iterations", 1))
        steps = workflow.get("steps", [])
        step_index = {step["id"]: idx for idx, step in enumerate(steps)}
        conditions = workflow.get("conditions", [])
        state = {"user_goal": goal, "steps": {}, "prepared_inputs": {}, "result_path": None}

        idx = 0
        transitions = 0
        while idx < len(steps):
            if transitions > len(steps) * max_iterations:
                raise RuntimeError("Workflow exceeded max_iterations")
            step = steps[idx]
            step_id = step["id"]
            output = await self._execute_step_with_policy(step, state, workflow_config)
            state["steps"][step_id] = {"definition": step, "output": output}
            self.provenance.record(
                session_id,
                step_id,
                step.get("agent") or step.get("skill") or step.get("adapter", "workflow"),
                outputs=self._dump(output) if not isinstance(output, ArtifactRecord) else output.model_dump(),
            )

            jumped = False
            for condition in conditions:
                if condition.get("after") == step_id and self._condition_matches(condition.get("if", ""), state):
                    goto = condition.get("goto")
                    if goto in step_index:
                        idx = step_index[goto]
                        jumped = True
                        break
            if not jumped:
                idx += 1
            transitions += 1

        execution = self._get_field(state["steps"].get("run", {}), "output")
        review = self._get_field(state["steps"].get("review", {}), "output")
        artifact = (
            self._context.memory.get("current_artifact")
            or self._get_field(state["steps"].get("register", {}), "output")
            or self._get_field(state["steps"].get("validate", {}), "output")
        )
        validation = ValidationResult(valid=True)
        if artifact:
            validation = await self.adapter.validate_artifact(artifact)
        self._context.iteration += 1
        return {
            "status": "completed",
            "session_id": session_id,
            "iteration": self._context.iteration,
            "proposal": self._dump(self._get_field(state["steps"].get("ideate", {}), "output")),
            "plan": self._dump(self._get_field(state["steps"].get("plan", {}), "output")),
            "artifact": self._dump(artifact),
            "validation": validation.model_dump(),
            "execution": self._dump(execution),
            "result_path": state.get("result_path"),
            "review": self._dump(review),
            "reflection": self._dump(self._get_field(state["steps"].get("reflect", {}), "output")),
            "workflow": workflow.get("name"),
            "steps": {k: self._dump(v["output"]) for k, v in state["steps"].items()},
        }

    async def run_once(self, goal: ResearchGoal) -> dict:
        session_id = self._context.session_id
        self.provenance.record(session_id, "start", "orchestrator", inputs=goal.model_dump())

        # Store target in context so reviewer can compare against it each iteration.
        if goal.target:
            self._context.memory["target"] = goal.target

        try:
            workflow = self.registry.get_workflow(self.workflow_name)
            return await self._run_workflow_definition(workflow, goal)
        except KeyError:
            pass

        from arc.packages import (
            load_ideator, load_planner, load_builder, load_reviewer, load_reflector
        )
        IdeatorAgent = load_ideator().IdeatorAgent
        PlannerAgent = load_planner().PlannerAgent
        Sim2LBuilderAgent = load_builder().Sim2LBuilderAgent
        ReviewerAgent = load_reviewer().ReviewerAgent
        ReflectorAgent = load_reflector().ReflectorAgent

        proposal = await self._agent(IdeatorAgent).run(goal)
        self.provenance.record(session_id, "ideate", "ideator", outputs=proposal.model_dump())

        plan = await self._agent(PlannerAgent).run(proposal)
        self._context.memory["current_plan"] = plan
        self.provenance.record(session_id, "plan", "planner", outputs=plan.model_dump())

        draft = await self._agent(Sim2LBuilderAgent).run(plan)
        artifact = self.artifacts.register(draft)
        self._context.memory["current_artifact"] = artifact
        self.provenance.record(session_id, "build", "builder", artifact_id=artifact.artifact_id)

        validation = await self.adapter.validate_artifact(artifact)
        if not validation.valid:
            self.provenance.record(session_id, "validate", "adapter", outputs={"valid": False})
            return {
                "status": "failed_validation",
                "proposal": proposal.model_dump(),
                "plan": plan.model_dump(),
                "artifact": artifact.model_dump(),
                "validation": validation.model_dump(),
            }

        prepared_inputs = await self.adapter.prepare_inputs(artifact, plan.parameters)
        execution = await self.adapter.run(artifact, prepared_inputs)
        result_path = self.results.save(execution)
        self._context.memory.setdefault("run_history", []).append({
            "run_id": execution.run_id,
            "inputs": prepared_inputs,
            "outputs": execution.outputs,
            "metrics": execution.metrics,
        })
        self.provenance.record(
            session_id, "execute", "adapter",
            artifact_id=artifact.artifact_id,
            run_id=execution.run_id,
            outputs=execution.model_dump(),
        )

        review = await self._agent(ReviewerAgent).run(execution)
        self.provenance.record(session_id, "review", "reviewer", outputs=review.model_dump())

        reflection = await self._agent(ReflectorAgent).run(review, execution=execution)

        self._context.iteration += 1

        return {
            "status": "completed",
            "session_id": session_id,
            "iteration": self._context.iteration,
            "proposal": proposal.model_dump(),
            "plan": plan.model_dump(),
            "artifact": artifact.model_dump(),
            "validation": validation.model_dump(),
            "execution": execution.model_dump(),
            "result_path": result_path,
            "review": review.model_dump(),
            "reflection": reflection,
        }
