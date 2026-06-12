from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# Caps applied to recorded inputs/outputs/metadata so one huge agent output
# (a full sweep array, a base64 blob) can't bloat the log. Generous enough
# that normal agent traffic is stored verbatim.
_MAX_STR_LEN = 4000
_MAX_ITEMS = 200
_MAX_DEPTH = 6

# Rotate the JSONL once it crosses this size; one rotated generation
# (``<name>.1``) is kept. Override with ARC_PROVENANCE_MAX_BYTES.
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024

# Entries kept in memory awaiting backend publication (publish_provenance).
# Bounded so a session without an active backend doesn't grow forever.
_MAX_UNPUBLISHED = 2000


def _truncate_value(value: Any, depth: int = 0) -> Any:
    """Recursively cap strings/collections so entries stay log-sized."""
    if depth >= _MAX_DEPTH:
        return "…(max depth)"
    if isinstance(value, str):
        if len(value) <= _MAX_STR_LEN:
            return value
        return value[:_MAX_STR_LEN] + f"…(+{len(value) - _MAX_STR_LEN} chars)"
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): _truncate_value(v, depth + 1) for k, v in items[:_MAX_ITEMS]}
        if len(items) > _MAX_ITEMS:
            out["…truncated"] = f"+{len(items) - _MAX_ITEMS} keys"
        return out
    if isinstance(value, (list, tuple)):
        out = [_truncate_value(v, depth + 1) for v in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            out.append(f"…(+{len(value) - _MAX_ITEMS} items)")
        return out
    return value


class ProvenanceLog:
    """Append-only JSONL log of all agent actions and state transitions.

    Holds a single ``O_APPEND`` file handle for the lifetime of the instance
    rather than reopening on every record(). Writes are serialized by an
    internal lock so concurrent agents don't interleave partial lines.

    Recorded values are size-capped (see :func:`_truncate_value`) and the
    file rotates to ``<name>.1`` once it exceeds ``ARC_PROVENANCE_MAX_BYTES``
    (default 64 MiB); ``read_session`` reads the rotated generation too.

    Entries are additionally buffered (bounded) for backend publication:
    the research loop drains them via :meth:`drain_unpublished` and hands
    them to ``backend.publish_provenance`` so the audit trail can outlive
    the local workspace.
    """

    def __init__(self, log_path: str = "workspace/memory/provenance.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: TextIO | None = None
        self._unpublished: list[dict[str, Any]] = []
        try:
            self._max_bytes = int(
                os.environ.get("ARC_PROVENANCE_MAX_BYTES", _DEFAULT_MAX_BYTES)
            )
        except ValueError:
            self._max_bytes = _DEFAULT_MAX_BYTES

    @property
    def _rotated_path(self) -> Path:
        return self.log_path.with_name(self.log_path.name + ".1")

    def _handle(self) -> TextIO:
        # Lazy-open so callers that never write don't create an empty file.
        if self._fh is None:
            self._fh = self.log_path.open("a", buffering=1)  # line-buffered
        return self._fh

    def _rotate_if_needed(self) -> None:
        """Rotate the current file to ``.1`` when it exceeds the size cap.

        Called with ``self._lock`` held, before a write. One rotated
        generation is kept (the previous ``.1`` is replaced).
        """
        if self._max_bytes <= 0:
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        try:
            os.replace(self.log_path, self._rotated_path)
        except OSError:
            # Rotation is best-effort; keep appending to the current file.
            pass

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
            "inputs": _truncate_value(inputs or {}),
            "outputs": _truncate_value(outputs or {}),
            "metadata": _truncate_value(metadata or {}),
        }
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            self._rotate_if_needed()
            fh = self._handle()
            fh.write(line)
            # Line-buffered means newline triggers flush, but be explicit
            # so test code that reads the log immediately sees the entry.
            fh.flush()
            self._unpublished.append(entry)
            if len(self._unpublished) > _MAX_UNPUBLISHED:
                del self._unpublished[: len(self._unpublished) - _MAX_UNPUBLISHED]

    def drain_unpublished(self) -> list[dict[str, Any]]:
        """Return and clear the entries awaiting backend publication."""
        with self._lock:
            entries = self._unpublished
            self._unpublished = []
        return entries

    def requeue_unpublished(self, entries: list[dict[str, Any]]) -> None:
        """Put drained entries back (publication failed; retry later).

        On overflow the *oldest* entries are evicted — same direction as
        :meth:`record` — so the most recent activity is what survives.
        """
        if not entries:
            return
        with self._lock:
            self._unpublished[:0] = entries
            if len(self._unpublished) > _MAX_UNPUBLISHED:
                del self._unpublished[: len(self._unpublished) - _MAX_UNPUBLISHED]

    def read_session(self, session_id: str) -> list[dict[str, Any]]:
        entries = []
        for path in (self._rotated_path, self.log_path):
            if not path.exists():
                continue
            with path.open() as f:
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
