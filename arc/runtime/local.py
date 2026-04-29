import uuid
from itertools import product
from pathlib import Path
from typing import Any

from arc.contracts.adapter import RuntimeAdapterContract
from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult


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
        value = inputs.get("input_parameter", 1.0)

        return ExecutionResult(
            run_id=run_id,
            status="completed",
            outputs={"result": value * 2},
            logs=[
                f"Run {run_id} started.",
                f"Inputs: {inputs}",
                "Execution completed via LocalRuntimeAdapter.",
            ],
            metrics={"execution_success": True, "input_parameter": value},
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
