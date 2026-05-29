"""Vector-memory extension — a *persistent* semantic store over artifacts,
results, and reflections (requirements.md §7.2.1).

Core defines ``ExtensionContract``; this implementation is a package. It
is the durable index that was previously missing — distinct from the
``searcher.embeddings`` strategy, which builds *transient* per-call
vectors and forgets them.

Backends are pluggable:
  * ``default`` (shipped, **zero-dependency**) — a JSON-file-backed
    TF/cosine store. Persists to ``persist_path`` so the index survives
    across runs. Good enough to make the feature real without pulling in a
    native vector DB.
  * ``chroma`` (optional) — a Chroma-backed store, imported lazily only
    when selected. Other backends (qdrant/faiss) can slot in the same way.

The extension registers *itself* (via ``registry.register_extension``,
done by the kernel) so consumers reach it with
``registry.get_extension("vector-memory")`` and call ``index`` / ``search``.
The loop wires it at three points (artifact register, result save, review
completion) — those call sites are a follow-up; the store + contract land
here.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from arc.contracts.extension import ExtensionContract

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _tf(tokens: list[str]) -> dict[str, float]:
    counts: dict[str, float] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0.0) + 1.0
    return counts


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class _DefaultStore:
    """Zero-dep persistent TF/cosine store backed by a JSON file."""

    def __init__(self, persist_path: str):
        self._path = Path(persist_path)
        self._docs: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                self._docs = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("vector-memory: could not load %s: %s", self._path, exc)
            self._docs = {}

    def _save(self) -> None:
        try:
            from arc.utils.io import atomic_write_text
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._path, json.dumps(self._docs))
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.debug("vector-memory: could not save %s: %s", self._path, exc)

    def index(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        self._docs[doc_id] = {
            "text": text,
            "tf": _tf(_tokenize(text)),
            "metadata": metadata or {},
        }
        self._save()

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        q = _tf(_tokenize(query))
        scored = [
            {"id": doc_id, "score": _cosine(q, doc["tf"]),
             "text": doc["text"], "metadata": doc["metadata"]}
            for doc_id, doc in self._docs.items()
        ]
        scored.sort(key=lambda d: d["score"], reverse=True)
        return [hit for hit in scored[:k] if hit["score"] > 0.0]

    def count(self) -> int:
        return len(self._docs)


class _ChromaStore:  # pragma: no cover — exercised only when chroma installed
    """Optional Chroma backend, imported lazily."""

    def __init__(self, persist_path: str, collection: str = "arc"):
        import chromadb
        self._client = chromadb.PersistentClient(path=persist_path)
        self._col = self._client.get_or_create_collection(collection)

    def index(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        self._col.upsert(ids=[doc_id], documents=[text], metadatas=[metadata or {}])

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        res = self._col.query(query_texts=[query], n_results=k)
        hits = []
        for i, doc_id in enumerate(res.get("ids", [[]])[0]):
            hits.append({
                "id": doc_id,
                "score": 1.0 - (res.get("distances", [[0]])[0][i] if res.get("distances") else 0),
                "text": (res.get("documents", [[None]])[0][i]),
                "metadata": (res.get("metadatas", [[{}]])[0][i]),
            })
        return hits

    def count(self) -> int:
        return self._col.count()


class VectorMemoryExtension(ExtensionContract):
    name = "vector-memory"

    def __init__(self) -> None:
        self._store: Any = None

    async def initialize(self, config: dict, registry: Any = None) -> None:
        config = config or {}
        backend = (config.get("backend") or "default").lower()
        persist_path = config.get("persist_path") or "workspace/memory/vectors"

        if backend in ("default", "tf", "json"):
            self._store = _DefaultStore(persist_path)
        elif backend == "chroma":
            try:
                self._store = _ChromaStore(persist_path, config.get("collection", "arc"))
            except Exception as exc:  # noqa: BLE001 — fall back rather than fail startup
                logger.warning("vector-memory: chroma backend unavailable (%s); "
                               "falling back to the default store.", exc)
                self._store = _DefaultStore(persist_path)
        else:
            logger.warning("vector-memory: unknown backend %r; using default.", backend)
            self._store = _DefaultStore(persist_path)

        logger.info("vector-memory ready (backend=%s, %d doc(s)).",
                    backend, self._store.count())

    # Public API consumers reach via registry.get_extension("vector-memory").
    def index(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        if self._store is not None:
            self._store.index(doc_id, text, metadata)

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        return self._store.search(query, k)

    def count(self) -> int:
        return self._store.count() if self._store is not None else 0

    async def shutdown(self) -> None:
        self._store = None
