"""arc-vector-memory extension: persistent semantic store (Item 4b).

Exercises the zero-dep default backend: index → search ranking, JSON
persistence across instances, and kernel loading via the entrypoint.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from arc.core.loader import _import_class
from arc.core.registry import ComponentRegistry

pytestmark = pytest.mark.chat

_Ext = _import_class("arc.packages.arc-vector-memory.extension:VectorMemoryExtension")
_mod = sys.modules[_Ext.__module__]


def _ext(tmp_path, backend="default"):
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize(
        {"backend": backend, "persist_path": str(tmp_path / "vectors.json")}, reg,
    ))
    return ext


def test_index_and_search_ranks_by_similarity(tmp_path):
    ext = _ext(tmp_path)
    ext.index("a", "silicon band gap semiconductor", {"kind": "artifact"})
    ext.index("b", "thermal conductivity heat transfer", {"kind": "artifact"})
    ext.index("c", "band gap of gallium arsenide", {"kind": "result"})

    hits = ext.search("band gap material", k=2)
    ids = [h["id"] for h in hits]
    # The two band-gap docs should rank above the thermal one.
    assert set(ids) <= {"a", "c"}
    assert all(h["score"] > 0 for h in hits)
    assert hits[0]["metadata"]  # metadata round-trips


def test_search_returns_empty_when_no_match(tmp_path):
    ext = _ext(tmp_path)
    ext.index("a", "silicon band gap", {})
    assert ext.search("completely unrelated xyzzy terms") == []


def test_persists_across_instances(tmp_path):
    ext1 = _ext(tmp_path)
    ext1.index("doc1", "persistent vector memory test", {"n": 1})
    assert ext1.count() == 1

    # A fresh extension pointed at the same path must see the indexed doc.
    ext2 = _ext(tmp_path)
    assert ext2.count() == 1
    hits = ext2.search("vector memory", k=1)
    assert hits and hits[0]["id"] == "doc1"


def test_idle_search_before_init_returns_empty():
    ext = _Ext()
    assert ext.search("anything") == []
    assert ext.count() == 0


def test_unknown_backend_falls_back_to_default(tmp_path):
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize(
        {"backend": "nonexistent", "persist_path": str(tmp_path / "v.json")}, reg,
    ))
    ext.index("x", "hello world", {})
    assert ext.count() == 1


def test_kernel_loads_vector_memory_and_registers_extension(tmp_path):
    from arc.core.kernel import Kernel
    config = tmp_path / "arc.toml"
    config.write_text(
        f"""
[packages]
paths = ["{__import__('pathlib').Path(__file__).resolve().parents[1] / 'arc' / 'packages' / 'arc-vector-memory'}"]

[extensions.vector-memory]
enabled = true
entrypoint = "arc.packages.arc-vector-memory.extension:VectorMemoryExtension"
backend = "default"
persist_path = "{tmp_path / 'v.json'}"
"""
    )
    kernel = Kernel(config_path=str(config))
    asyncio.run(kernel.startup())
    ext = kernel.registry.get_extension("vector-memory")
    assert ext is not None
    ext.index("k", "kernel loaded vector memory", {})
    assert ext.search("vector memory")[0]["id"] == "k"
    asyncio.run(kernel.shutdown())
