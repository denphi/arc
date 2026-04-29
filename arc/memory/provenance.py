import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProvenanceLog:
    """Append-only JSONL log of all agent actions and state transitions."""

    def __init__(self, log_path: str = "workspace/memory/provenance.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        session_id: str,
        action: str,
        agent: str,
        artifact_id: str | None = None,
        run_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "action": action,
            "agent": agent,
            "artifact_id": artifact_id,
            "run_id": run_id,
            "inputs": inputs or {},
            "outputs": outputs or {},
            "metadata": metadata or {},
        }
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_session(self, session_id: str) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        entries = []
        with self.log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        entries.append(entry)
                except json.JSONDecodeError:
                    pass
        return entries
