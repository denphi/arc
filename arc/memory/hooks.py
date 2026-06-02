"""Lifecycle memory hooks — wire the optional vector-memory + knowledge-graph
extensions into the research loop (design/todo.md item 1).

Both stores are implemented as packages (``arc-vector-memory`` /
``arc-knowledge-graph``) and reached through
``registry.get_extension(name)``. Before this module nothing in the
orchestrator/chat/UI paths actually read or wrote them, so enabling an
extension registered a tested-but-empty store.

``MemoryHooks`` is the single call site the workflow uses at three
lifecycle points — artifact registration, result save, and review
completion — plus a ``search`` surface agents can reach through
``AgentContext.memory["memory_search"]``. Every method is a defensive
no-op when the relevant extension is absent or disabled, so a normal run
without the packages behaves exactly as before.

The same instance is shared by the YAML workflow engine and the chat
phase path so memory wiring is not UI-specific.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    """Best-effort conversion of a pydantic model / dict / object to a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _truncate(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


class MemoryHooks:
    """Index lifecycle events into vector-memory + knowledge-graph.

    Construct once per workflow with the active ``ComponentRegistry`` and
    ``session_id``. The extensions are looked up lazily on each call (cheap
    dict lookup) so a registry that gains/loses an extension mid-session is
    handled, and a missing extension is a clean no-op.
    """

    def __init__(self, registry: Any, session_id: str):
        self._registry = registry
        self.session_id = session_id

    # --- extension accessors (None when absent/disabled) ---

    @property
    def vectors(self) -> Any:
        if self._registry is None:
            return None
        try:
            return self._registry.get_extension("vector-memory")
        except Exception:  # noqa: BLE001 — never let memory wiring break a run
            return None

    @property
    def graph(self) -> Any:
        if self._registry is None:
            return None
        try:
            return self._registry.get_extension("knowledge-graph")
        except Exception:  # noqa: BLE001
            return None

    @property
    def enabled(self) -> bool:
        return self.vectors is not None or self.graph is not None

    # --- low-level safe wrappers ---

    def _index(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        store = self.vectors
        if store is None:
            return
        try:
            store.index(doc_id, _truncate(text), metadata or {})
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("memory: vector index failed for %s: %s", doc_id, exc)

    def _edge(self, src: str, relation: str, dst: str, metadata: dict | None = None) -> None:
        store = self.graph
        if store is None:
            return
        try:
            store.add_edge(src, relation, dst, metadata or {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory: graph edge %s-[%s]->%s failed: %s",
                         src, relation, dst, exc)

    # --- lifecycle hooks ---

    def on_artifact_registered(self, artifact: Any) -> None:
        """Index artifact name/description/file metadata; record session→artifact edge."""
        if not self.enabled:
            return
        data = _as_dict(artifact)
        artifact_id = data.get("artifact_id") or data.get("name")
        if not artifact_id:
            return
        name = data.get("name", "")
        description = data.get("description", "")
        files = data.get("files") or {}
        file_names = ", ".join(files.keys()) if isinstance(files, dict) else ""
        text = "\n".join(p for p in (name, description, file_names) if p)
        self._index(
            f"artifact:{artifact_id}", text,
            {"kind": "artifact", "artifact_id": artifact_id, "name": name,
             "session_id": self.session_id},
        )
        self._edge(f"session:{self.session_id}", "produced", f"artifact:{artifact_id}",
                   {"name": name})

    def on_result_saved(
        self, artifact: Any, execution: Any, inputs: dict | None = None,
    ) -> None:
        """Index outputs/metrics/log summary; record artifact→result→variable edges."""
        if not self.enabled:
            return
        exec_data = _as_dict(execution)
        run_id = exec_data.get("run_id")
        if not run_id:
            return
        art = _as_dict(artifact)
        artifact_id = art.get("artifact_id") or art.get("name") or "unknown"
        outputs = exec_data.get("outputs") or {}
        metrics = exec_data.get("metrics") or {}
        logs = exec_data.get("logs") or []
        log_summary = " ".join(str(line) for line in logs[-5:])
        text = "\n".join(filter(None, [
            f"run {run_id} of artifact {artifact_id}",
            f"inputs: {inputs}" if inputs else "",
            f"outputs: {outputs}" if outputs else "",
            f"metrics: {metrics}" if metrics else "",
            log_summary,
        ]))
        self._index(
            f"result:{run_id}", text,
            {"kind": "result", "run_id": run_id, "artifact_id": artifact_id,
             "status": exec_data.get("status"), "session_id": self.session_id},
        )
        self._edge(f"artifact:{artifact_id}", "ran", f"result:{run_id}",
                   {"status": exec_data.get("status")})
        # One edge per output variable so an agent can find which runs
        # touched a given quantity.
        if isinstance(outputs, dict):
            for var in outputs:
                self._edge(f"result:{run_id}", "measured", f"variable:{var}")

    def on_review_completed(self, artifact: Any, review: Any, run_id: str | None = None) -> None:
        """Index the review summary/feedback; record result/artifact→review edge."""
        if not self.enabled:
            return
        rev = _as_dict(review)
        if not rev:
            return
        art = _as_dict(artifact)
        artifact_id = art.get("artifact_id") or art.get("name") or "unknown"
        doc_id = f"review:{run_id or artifact_id}"
        text = "\n".join(filter(None, [
            rev.get("summary", ""),
            "strengths: " + "; ".join(rev.get("strengths", [])) if rev.get("strengths") else "",
            "weaknesses: " + "; ".join(rev.get("weaknesses", [])) if rev.get("weaknesses") else "",
            "recommendations: " + "; ".join(rev.get("recommendations", []))
            if rev.get("recommendations") else "",
        ]))
        self._index(
            doc_id, text,
            {"kind": "review", "artifact_id": artifact_id, "run_id": run_id,
             "approved": rev.get("approved"), "session_id": self.session_id},
        )
        src = f"result:{run_id}" if run_id else f"artifact:{artifact_id}"
        self._edge(src, "reviewed_as", doc_id, {"approved": rev.get("approved")})

    # --- agent-facing search surface ---

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Semantic search over indexed memory. Empty list when disabled."""
        store = self.vectors
        if store is None:
            return []
        try:
            return store.search(query, k)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory: search failed: %s", exc)
            return []

    def neighbors(self, node: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Graph neighbours of ``node``. Empty list when disabled."""
        store = self.graph
        if store is None:
            return []
        try:
            return store.neighbors(node, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory: neighbors failed: %s", exc)
            return []
