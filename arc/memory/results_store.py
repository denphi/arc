import json
import re
from pathlib import Path

from arc.schemas.execution import ExecutionResult
from arc.utils.io import atomic_write_text


_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _SAFE_RUN_ID_RE.match(run_id):
        raise ValueError(
            f"Unsafe run_id: {run_id!r}. Must match {_SAFE_RUN_ID_RE.pattern}."
        )
    return run_id


_validate_run_id = validate_run_id


class ResultsStore:
    """File-based store for execution results."""

    def __init__(self, root: str = "workspace/runs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, result: ExecutionResult) -> str:
        validate_run_id(result.run_id)
        path = self.root / f"{result.run_id}.json"
        atomic_write_text(path, json.dumps(result.model_dump(), indent=2))
        return str(path)

    def get(self, run_id: str) -> ExecutionResult:
        validate_run_id(run_id)
        path = self.root / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Result not found: {run_id}")
        return ExecutionResult.model_validate_json(path.read_text())

    def list_all(self) -> list[ExecutionResult]:
        results = []
        for path in sorted(self.root.glob("*.json")):
            try:
                results.append(ExecutionResult.model_validate_json(path.read_text()))
            except Exception:
                pass
        return results
