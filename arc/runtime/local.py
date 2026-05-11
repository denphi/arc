import uuid
from itertools import product
from pathlib import Path
from typing import Any

from arc.contracts.adapter import RuntimeAdapterContract
from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult
from arc.sim2l_schema import load_sim2l_schema


class LocalRuntimeAdapter(RuntimeAdapterContract):
    """Executes artifact workflows in the local process.

    Placeholder implementation. A production version would invoke the Sim2L
    execution APIs or run the artifact notebook via Papermill.
    """

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
                from arc.runtime.sim2l_adapter import _import_workflow_func
                _import_workflow_func(str(artifact_path), artifact.artifact_id)
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
        if workflow_path.exists():
            try:
                from arc.runtime.sim2l_adapter import _import_workflow_func

                input_schema, output_schema = load_sim2l_schema(artifact.path)
                defaults = {
                    key: field.get("default", 1.0)
                    for key, field in input_schema.items()
                }
                reconciled = {
                    **defaults,
                    **{key: value for key, value in inputs.items() if not input_schema or key in input_schema},
                }
                func = _import_workflow_func(artifact.path, artifact.artifact_id)
                raw_outputs = func(**reconciled) or {}
                if not isinstance(raw_outputs, dict):
                    raise ValueError(f"simulate() must return dict, got {type(raw_outputs).__name__}")
                outputs = {
                    key: raw_outputs.get(key)
                    for key in output_schema
                } if output_schema else raw_outputs
                return ExecutionResult(
                    run_id=run_id,
                    status="completed",
                    outputs=outputs,
                    logs=[
                        f"Run {run_id} started.",
                        f"Inputs: {reconciled}",
                        "Execution completed via LocalRuntimeAdapter.",
                    ],
                    metrics={"execution_success": True, **reconciled},
                )
            except Exception as exc:
                normalized = await self.normalize_errors(exc)
                return ExecutionResult(
                    run_id=run_id,
                    status="error",
                    logs=[normalized["message"]],
                )

        # No workflow.py — return an explicit error rather than a misleading
        # "value * 2" demo result, which would let downstream reviewers think
        # the artifact ran successfully and pollute caches with bogus outputs.
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
        return "completed"

    async def collect_outputs(self, run_id: str) -> dict[str, Any]:
        return {}

    async def collect_logs(self, run_id: str) -> list[str]:
        return []

    async def collect_metrics(self, run_id: str) -> dict[str, Any]:
        return {}

    async def normalize_errors(self, error: Exception) -> dict[str, Any]:
        return {"type": error.__class__.__name__, "message": str(error)}
