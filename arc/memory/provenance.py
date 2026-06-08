from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class ProvenanceLog:
    """Append-only JSONL log of all agent actions and state transitions.

    Holds a single ``O_APPEND`` file handle for the lifetime of the instance
    rather than reopening on every record(). Writes are serialized by an
    internal lock so concurrent agents don't interleave partial lines.
    """

    def __init__(self, log_path: str = "workspace/memory/provenance.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: TextIO | None = None

    def _handle(self) -> TextIO:
        # Lazy-open so callers that never write don't create an empty file.
        if self._fh is None:
            self._fh = self.log_path.open("a", buffering=1)  # line-buffered
        return self._fh

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
        line = json.dumps(entry) + "\n"
        with self._lock:
            fh = self._handle()
            fh.write(line)
            # Line-buffered means newline triggers flush, but be explicit
            # so test code that reads the log immediately sees the entry.
            fh.flush()

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

    def close(self) -> None:
        """Release the underlying file handle, if any."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    def __del__(self):
        # Best-effort cleanup; users should call close() explicitly if it
        # matters that the handle is released before GC.
        try:
            self.close()
        except Exception:
            pass
