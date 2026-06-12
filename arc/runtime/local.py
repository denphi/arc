import asyncio
import uuid
from itertools import product
from pathlib import Path
from typing import Any

from arc.contracts.adapter import RuntimeAdapterContract
from arc.runtime._adapter_common import filter_outputs, reconcile_inputs
from arc.runtime.executor import execute_workflow, load_simulate
from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult
from arc.sim2l_schema import load_sim2l_schema


class LocalRuntimeAdapter(RuntimeAdapterContract):
    """ARC's own local runtime adapter.

    Runs an artifact's ``simulate(**inputs) -> dict`` via the arc-owned
    executor (``arc/runtime/executor.py``) — subprocess-isolated with a
    timeout by default, fully standalone with **no sim2l dependency**.
    This is the default adapter; ``Sim2LRuntimeAdapter`` is used only
    when ``ARC_RUNTIME_ADAPTER=sim2l``.

    ``results_store`` (the session :class:`~arc.memory.results_store.
    ResultsStore`, wired by ``_build_adapter``) backs the post-hoc query
    methods — ``get_status`` / ``collect_*`` answer from the saved run
    instead of pretending every id is "completed". Without a store they
    answer "unknown"/empty.
    """

    def __init__(self, results_store: Any = None):
        self._results = results_store

    def _saved_result(self, run_id: str) -> ExecutionResult | None:
        if self._results is None:
            return None
        try:
            return self._results.get(run_id)
        except Exception:  # noqa: BLE001 — unknown/unsafe id → not found
            return None

    async def validate_artifact(self, artifact: ArtifactRecord) -> ValidationResult:
        artifact_path = Path(artifact.path)
        errors: list[str] = []
        warnings: list[str] = []

        if not artifact_path.exists():
            errors.append(f"Artifact path does not exist: {artifact.path}")

        required_files = ["sim2l.yaml", "workflow.py"]
        for fname in required_files:
            if not (artifact_path / fname).exists():
                warnings.append(f"Expected file not found: {fname}")

        if (artifact_path / "workflow.py").exists():
            try:
                # Loads + safety-lints workflow.py without running it.
                load_simulate(str(artifact_path), artifact.artifact_id)
            except Exception as exc:
                errors.append(f"workflow.py import error: {exc}")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    async def prepare_inputs(
        self,
        artifact: ArtifactRecord,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        return parameters

    async def run(
        self,
        artifact: ArtifactRecord,
        inputs: dict[str, Any],
    ) -> ExecutionResult:
        run_id = str(uuid.uuid4())
        workflow_path = Path(artifact.path) / "workflow.py"
        if not workflow_path.exists():
            # No workflow.py — explicit error rather than a misleading demo
            # result that would let reviewers think the artifact ran.
            return ExecutionResult(
                run_id=run_id,
                status="error",
                outputs={},
                logs=[
                    f"Run {run_id}: artifact has no workflow.py at "
                    f"{artifact.path}. LocalRuntimeAdapter requires a "
                    "workflow.py defining simulate(**inputs) -> dict.",
                ],
                metrics={"execution_success": False, "reason": "missing_workflow_py"},
            )

        # Reconcile inputs against the artifact's declared schema: start
        # from declared defaults, overlay the caller's inputs (dropping
        # keys the schema doesn't declare, when a schema exists).
        input_schema, output_schema = await asyncio.to_thread(
            load_sim2l_schema, artifact.path
        )
        reconciled = reconcile_inputs(input_schema, inputs)

        # Execute via the arc-owned executor (subprocess + timeout by
        # default). Returns {"ok": bool, "outputs"|"error": ...}.
        # ``execute_workflow`` blocks (spawns + joins a child process), so
        # run it off the event loop.
        result = await asyncio.to_thread(
            execute_workflow, artifact.path, artifact.artifact_id, reconciled
        )

        if not result.get("ok"):
            return ExecutionResult(
                run_id=run_id,
                status="error",
                inputs=reconciled,
                outputs={},
                logs=[result.get("error", "execution failed")],
                # Inputs first so a parameter named execution_success can't
                # overwrite the metric.
                metrics={**reconciled, "execution_success": False},
            )

        outputs = filter_outputs(output_schema, result.get("outputs") or {})
        return ExecutionResult(
            run_id=run_id,
            status="completed",
            inputs=reconciled,
            outputs=outputs,
            logs=[
                f"Run {run_id} started.",
                f"Inputs: {reconciled}",
                "Execution completed via LocalRuntimeAdapter (arc executor).",
            ],
            metrics={**reconciled, "execution_success": True},
        )

    async def run_sweep(
        self,
        artifact: ArtifactRecord,
        parameter_space: dict[str, list[Any]],
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        keys = list(parameter_space)
        if not keys:
            return [await self.run(artifact, {})]
        for values in product(*(parameter_space[key] for key in keys)):
            result = await self.run(artifact, dict(zip(keys, values)))
            results.append(result)
        return results

    async def get_status(self, run_id: str) -> str:
        result = self._saved_result(run_id)
        return result.status if result is not None else "unknown"

    async def collect_outputs(self, run_id: str) -> dict[str, Any]:
        result = self._saved_result(run_id)
        return dict(result.outputs or {}) if result is not None else {}

    async def collect_logs(self, run_id: str) -> list[str]:
        result = self._saved_result(run_id)
        return list(result.logs or []) if result is not None else []

    async def collect_metrics(self, run_id: str) -> dict[str, Any]:
        result = self._saved_result(run_id)
        return dict(result.metrics or {}) if result is not None else {}

    async def normalize_errors(self, error: Exception) -> dict[str, Any]:
        return {"type": error.__class__.__name__, "message": str(error)}
