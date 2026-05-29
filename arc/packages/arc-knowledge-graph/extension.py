"""Knowledge-graph extension — relationships between experiments,
artifacts, and variables (requirements.md §7.2.2).

Core defines ``ExtensionContract``; this implementation is a package. A
zero-dependency directed-graph store with JSON persistence, exposing
``add_edge`` / ``neighbors`` / ``edges``. Consumers reach it via
``registry.get_extension("knowledge-graph")``.

This was the lowest-priority TODO item ("deferred until a concrete
consumer exists"). It ships as a minimal, self-contained store so the
capability is *real and tested*; richer graph queries / a Neo4j backend
can slot in behind the same extension later if a consumer needs them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arc.contracts.extension import ExtensionContract

logger = logging.getLogger(__name__)


class _GraphStore:
    """Persistent directed multigraph: (src, relation, dst, metadata)."""

    def __init__(self, persist_path: str):
        self._path = Path(persist_path)
        self._edges: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                self._edges = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("knowledge-graph: could not load %s: %s", self._path, exc)
            self._edges = []

    def _save(self) -> None:
        try:
            from arc.utils.io import atomic_write_text
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._path, json.dumps(self._edges))
        except Exception as exc:  # noqa: BLE001
            logger.debug("knowledge-graph: could not save %s: %s", self._path, exc)

    def add_edge(self, src: str, relation: str, dst: str, metadata: dict | None = None) -> None:
        edge = {"src": src, "relation": relation, "dst": dst, "metadata": metadata or {}}
        # De-dupe identical (src, relation, dst) triples; refresh metadata.
        for e in self._edges:
            if e["src"] == src and e["relation"] == relation and e["dst"] == dst:
                e["metadata"] = edge["metadata"]
                self._save()
                return
        self._edges.append(edge)
        self._save()

    def neighbors(self, node: str, *, relation: str | None = None,
                  direction: str = "out") -> list[dict[str, Any]]:
        out = []
        for e in self._edges:
            if relation is not None and e["relation"] != relation:
                continue
            if direction in ("out", "both") and e["src"] == node:
                out.append({"node": e["dst"], "relation": e["relation"],
                            "direction": "out", "metadata": e["metadata"]})
            if direction in ("in", "both") and e["dst"] == node:
                out.append({"node": e["src"], "relation": e["relation"],
                            "direction": "in", "metadata": e["metadata"]})
        return out

    def edges(self) -> list[dict[str, Any]]:
        return list(self._edges)


class KnowledgeGraphExtension(ExtensionContract):
    name = "knowledge-graph"

    def __init__(self) -> None:
        self._store: _GraphStore | None = None

    async def initialize(self, config: dict, registry: Any = None) -> None:
        config = config or {}
        persist_path = config.get("persist_path") or "workspace/memory/graph.json"
        self._store = _GraphStore(persist_path)
        logger.info("knowledge-graph ready (%d edge(s)).", len(self._store.edges()))

    # Public API via registry.get_extension("knowledge-graph").
    def add_edge(self, src: str, relation: str, dst: str, metadata: dict | None = None) -> None:
        if self._store is not None:
            self._store.add_edge(src, relation, dst, metadata)

    def neighbors(self, node: str, *, relation: str | None = None,
                  direction: str = "out") -> list[dict[str, Any]]:
        if self._store is None:
            return []
        return self._store.neighbors(node, relation=relation, direction=direction)

    def edges(self) -> list[dict[str, Any]]:
        return self._store.edges() if self._store is not None else []

    async def shutdown(self) -> None:
        self._store = None
