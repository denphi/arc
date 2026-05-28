"""Core research workflow orchestrator.

Wires together agents, adapter, registry, and stores for a single iteration.
The provider is optional — without it, agents use deterministic stub logic.
"""

import logging
import os
from typing import Any

from arc.contracts.agent import AgentContext
from arc.core.config import load_arc_toml, resolve_package_paths
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
    """Build the runtime adapter requested by ARC_RUNTIME_ADAPTER."""
    adapter_name = os.environ.get("ARC_RUNTIME_ADAPTER", "local").lower()
    if adapter_name in {"local", "python"}:
        from arc.runtime.local import LocalRuntimeAdapter
        logger.info("Using LocalRuntimeAdapter")
        return LocalRuntimeAdapter()
    if adapter_name in {"sim2l", "sim2l-local"}:
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)
    if adapter_name in {"service", "services", "sim2l-service", "sim2l-services"}:
        os.environ.setdefault("ARC_STORAGE_MODE", "required")
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter with required service persistence")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)

    if adapter_name in {"auto"}:
        return _build_auto_adapter(db_path=db_path, session_id=session_id)

    logger.warning("Unknown ARC_RUNTIME_ADAPTER=%s; falling back to local", adapter_name)
    from arc.runtime.local import LocalRuntimeAdapter
    return LocalRuntimeAdapter()


def _build_auto_adapter(db_path: str | None = None, session_id: str | None = None):
    """Auto-select Sim2LRuntimeAdapter when sim2l is importable, else fall back to local.

    Used by ``ARC_RUNTIME_ADAPTER=auto``. Useful for environments where the
    same code runs both with and without sim2l installed (CI, test fixtures,
    light demos). Prefer an explicit ``local``/``sim2l``/``service`` value
    in production deployments so the chosen runtime is unambiguous.
    """
    try:
        import sim2l  # noqa: F401
        from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
        logger.info("Using Sim2LRuntimeAdapter (auto-detected)")
        return Sim2LRuntimeAdapter(db_path=db_path, session_id=session_id)
    except ImportError:
        from arc.runtime.local import LocalRuntimeAdapter
        logger.info("sim2l not found — using LocalRuntimeAdapter (auto fallback)")
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
    """Build the registry used when a caller doesn't inject one.

    Loads the same ``arc.toml`` the kernel uses (cached via
    ``arc.core.config.load_arc_toml``), resolves its package paths, and
    loads them. The orchestrator does NOT apply enabled/disabled filtering —
    it loads everything declared in the config.
    """
    registry = ComponentRegistry()
    config_path, config = load_arc_toml()
    package_paths = resolve_package_paths(config, config_path) if config else []
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
        # The backend handles the loop's publish actions (register /
        # persist / record). Resolves to a sim2l backend when sim2l is
        # active + the adapter supports it, else a silent no-op so ARC
        # runs fully local with no shared persistence. See
        # arc/runtime/backend.py and design/architecture.md.
        from arc.runtime.backend import resolve_backend
        self.backend = resolve_backend(self.adapter)
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
        """Look up ``field`` on ``value`` for workflow-YAML reference resolution.

        Dict access is unrestricted (callers control their own payload), but
        attribute access is filtered: leading-underscore names — including
        every dunder — are rejected. This stops a malicious workflow YAML
        from walking the Python object graph via references like
        ``review.output.__class__.__init__.__globals__`` (review item #A9).
        """
        if isinstance(value, dict):
            return value.get(field)
        if not isinstance(field, str) or field.startswith("_"):
            return None
        return getattr(value, field, None)

    # Operators must be ordered longest-first so two-character forms (>=, <=, !=, ==)
    # are matched before single-character (>, <).
    _CONDITION_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")

    @staticmethod
    def _parse_condition_literal(text: str) -> Any:
        """Parse the right-hand side of a workflow condition into a Python value.

        Accepts: ``true``/``false`` (case-insensitive), integers, floats, and
        single- or double-quoted strings. Bare identifiers are returned as
        strings so legacy ``foo == bar`` style still works.
        """
        text = text.strip()
        lower = text.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if (text.startswith("'") and text.endswith("'")) or (
            text.startswith('"') and text.endswith('"')
        ):
            return text[1:-1]
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return text  # bare identifier — treat as string literal

    def _condition_matches(self, expression: str, state: dict) -> bool:
        """Evaluate a workflow ``if:`` expression like ``review.output.approved == false``.

        Supports the operators listed in ``_CONDITION_OPERATORS``. Raises
        ``ValueError`` on unsupported syntax rather than silently returning
        False, so workflow authors get a clear error instead of a confusing
        no-op.
        """
        expression = (expression or "").strip()
        if not expression:
            return False

        # Pick the first operator that appears. Longer operators come first in
        # _CONDITION_OPERATORS so ``>=`` is matched before ``>``.
        for op in self._CONDITION_OPERATORS:
            idx = expression.find(op)
            if idx >= 0:
                left = expression[:idx].strip()
                right = expression[idx + len(op):].strip()
                break
        else:
            raise ValueError(
                f"Unsupported workflow condition: {expression!r}. "
                f"Use one of {', '.join(self._CONDITION_OPERATORS)}."
            )

        lhs = self._resolve_ref(left, state, {})
        rhs = self._parse_condition_literal(right)

        # Booleans need identity-style comparison so e.g. ``approved == false``
        # behaves the same when the resolved value is None vs. False.
        if isinstance(rhs, bool):
            lhs_bool = bool(lhs)
            return (op == "==") == (lhs_bool is rhs)
        if op == "==":
            return lhs == rhs or str(lhs) == str(rhs)
        if op == "!=":
            return not (lhs == rhs or str(lhs) == str(rhs))
        # Ordering operators require numeric LHS — fall back to False on
        # type mismatch rather than raising, since workflows may legitimately
        # have a step that doesn't run yet.
        try:
            lhs_num = float(lhs)  # type: ignore[arg-type]
            rhs_num = float(rhs)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if op == ">":
            return lhs_num > rhs_num
        if op == "<":
            return lhs_num < rhs_num
        if op == ">=":
            return lhs_num >= rhs_num
        if op == "<=":
            return lhs_num <= rhs_num
        return False

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

    # Methods that workflow YAML files are allowed to invoke on an adapter.
    # Any other ``method:`` value in a step is rejected — this keeps a
    # malicious / typo'd workflow file from calling private helpers (eg.
    # ``_get_repo``, ``_push_to_catalog``) via ``getattr(adapter, …)``.
    # Review item #A6.
    _ALLOWED_ADAPTER_METHODS = frozenset({
        "run",
        "run_sweep",
        "validate_artifact",
        "prepare_inputs",
        "get_status",
        "collect_outputs",
        "collect_logs",
        "collect_metrics",
        "register_artifact",
    })

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
            if method_name not in self._ALLOWED_ADAPTER_METHODS:
                allowed = ", ".join(sorted(self._ALLOWED_ADAPTER_METHODS))
                raise ValueError(
                    f"Workflow step requested disallowed adapter method "
                    f"{method_name!r}. Allowed methods: {allowed}."
                )
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
        status = "completed"

        idx = 0
        transitions = 0
        while idx < len(steps):
            if transitions >= len(steps) * max_iterations:
                status = "iteration_limit"
                break
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
            "status": status,
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
        except KeyError as exc:
            # Previously this path fell through to a 60-line hard-coded
            # pipeline that duplicated the YAML workflow. That hid real
            # registration bugs (eg. a missing arc.toml or a package that
            # failed to load). Surface the error instead — the available
            # workflows are listed so users can pick a registered name.
            available = self.registry.list_workflows()
            raise KeyError(
                f"Workflow {self.workflow_name!r} is not registered. "
                f"Available workflows: {available}. "
                f"Check arc.toml [packages].paths and any errors during "
                f"package loading."
            ) from exc

        return await self._run_workflow_definition(workflow, goal)
