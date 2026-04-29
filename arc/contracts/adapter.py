from abc import ABC, abstractmethod
from typing import Any

from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult


class RuntimeAdapterContract(ABC):
    """Interface that all execution backends must implement."""

    @abstractmethod
    async def validate_artifact(self, artifact: ArtifactRecord) -> ValidationResult:
        raise NotImplementedError

    @abstractmethod
    async def prepare_inputs(
        self,
        artifact: ArtifactRecord,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def run(
        self,
        artifact: ArtifactRecord,
        inputs: dict[str, Any],
    ) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    async def run_sweep(
        self,
        artifact: ArtifactRecord,
        parameter_space: dict[str, list[Any]],
    ) -> list[ExecutionResult]:
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, run_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def collect_outputs(self, run_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def collect_logs(self, run_id: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def collect_metrics(self, run_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def normalize_errors(self, error: Exception) -> dict[str, Any]:
        raise NotImplementedError
