import json
from pathlib import Path

from arc.schemas.execution import ExecutionResult


class ResultsStore:
    """File-based store for execution results."""

    def __init__(self, root: str = "workspace/runs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, result: ExecutionResult) -> str:
        path = self.root / f"{result.run_id}.json"
        path.write_text(json.dumps(result.model_dump(), indent=2))
        return str(path)

    def get(self, run_id: str) -> ExecutionResult:
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
