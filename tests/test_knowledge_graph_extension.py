"""arc-knowledge-graph extension (Item 7).

Zero-dep directed multigraph: add_edge / neighbors / edges, with JSON
persistence and kernel loading.
"""

from __future__ import annotations

import asyncio

import pytest

from arc.core.loader import _import_class
from arc.core.registry import ComponentRegistry

pytestmark = pytest.mark.chat

_Ext = _import_class("arc.packages.arc-knowledge-graph.extension:KnowledgeGraphExtension")


def _ext(tmp_path):
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({"persist_path": str(tmp_path / "graph.json")}, reg))
    return ext


def test_add_edge_and_neighbors_out(tmp_path):
    ext = _ext(tmp_path)
    ext.add_edge("artifact:A", "produced", "result:R1", {"score": 0.9})
    ext.add_edge("artifact:A", "uses", "variable:bandgap")

    out = ext.neighbors("artifact:A")
    nodes = {(n["node"], n["relation"]) for n in out}
    assert nodes == {("result:R1", "produced"), ("variable:bandgap", "uses")}


def test_neighbors_filtered_by_relation_and_direction(tmp_path):
    ext = _ext(tmp_path)
    ext.add_edge("A", "derived_from", "B")
    only = ext.neighbors("B", relation="derived_from", direction="in")
    assert only == [{"node": "A", "relation": "derived_from", "direction": "in", "metadata": {}}]
    # No outgoing derived_from edges from B.
    assert ext.neighbors("B", relation="derived_from", direction="out") == []


def test_add_edge_dedupes_triples(tmp_path):
    ext = _ext(tmp_path)
    ext.add_edge("A", "rel", "B", {"v": 1})
    ext.add_edge("A", "rel", "B", {"v": 2})   # same triple → updates metadata
    assert len(ext.edges()) == 1
    assert ext.edges()[0]["metadata"] == {"v": 2}


def test_persists_across_instances(tmp_path):
    e1 = _ext(tmp_path)
    e1.add_edge("X", "links", "Y")
    e2 = _ext(tmp_path)
    assert e2.edges() == [{"src": "X", "relation": "links", "dst": "Y", "metadata": {}}]


def test_idle_before_init():
    ext = _Ext()
    assert ext.neighbors("anything") == []
    assert ext.edges() == []


def test_kernel_loads_knowledge_graph(tmp_path):
    from arc.core.kernel import Kernel
    from pathlib import Path
    config = tmp_path / "arc.toml"
    pkg = Path(__file__).resolve().parents[1] / "arc" / "packages" / "arc-knowledge-graph"
    config.write_text(
        f"""
[packages]
paths = ["{pkg}"]

[extensions.knowledge-graph]
enabled = true
entrypoint = "arc.packages.arc-knowledge-graph.extension:KnowledgeGraphExtension"
persist_path = "{tmp_path / 'g.json'}"
"""
    )
    kernel = Kernel(config_path=str(config))
    asyncio.run(kernel.startup())
    kg = kernel.registry.get_extension("knowledge-graph")
    assert kg is not None
    kg.add_edge("a", "r", "b")
    assert kg.neighbors("a")[0]["node"] == "b"
    asyncio.run(kernel.shutdown())
