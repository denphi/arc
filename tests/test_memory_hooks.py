"""Memory-hook wiring tests (design/todo.md item 1).

Asserts the research loop indexes artifacts/results/reviews into the
vector-memory + knowledge-graph extensions at the expected lifecycle
points, and that a workflow with neither extension is a clean no-op.
"""

from __future__ import annotations

import pytest

from arc.memory.hooks import MemoryHooks
from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.artifact import ArtifactRecord
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ResearchGoal
from arc.schemas.review import ReviewResult


class FakeVectorMemory:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def index(self, doc_id, text, metadata=None):
        self.docs[doc_id] = {"text": text, "metadata": metadata or {}}

    def search(self, query, k=5):
        return [{"id": d, "score": 1.0, **v} for d, v in list(self.docs.items())[:k]]

    def count(self):
        return len(self.docs)


class FakeGraph:
    def __init__(self):
        self.edges_list: list[dict] = []

    def add_edge(self, src, relation, dst, metadata=None):
        self.edges_list.append(
            {"src": src, "relation": relation, "dst": dst, "metadata": metadata or {}}
        )

    def neighbors(self, node, **kwargs):
        return [e for e in self.edges_list if e["src"] == node]

    def edges(self):
        return list(self.edges_list)


def _install_fakes(workflow):
    vec, graph = FakeVectorMemory(), FakeGraph()
    workflow.registry.register_extension("vector-memory", vec)
    workflow.registry.register_extension("knowledge-graph", graph)
    return vec, graph


# ── Unit-level: MemoryHooks directly ────────────────────────────────────


def test_hooks_noop_when_extensions_absent():
    """No extensions registered → every method is a silent no-op."""
    from arc.core.registry import ComponentRegistry

    hooks = MemoryHooks(ComponentRegistry(), "sess-1")
    assert hooks.enabled is False
    # Must not raise.
    hooks.on_artifact_registered(
        ArtifactRecord(artifact_id="a1", name="x", version="1", state="ready", path="/tmp")
    )
    hooks.on_result_saved(None, ExecutionResult(run_id="r1", status="completed"))
    hooks.on_review_completed(None, ReviewResult(approved=True, summary="ok"))
    assert hooks.search("anything") == []
    assert hooks.neighbors("artifact:a1") == []


def test_hooks_index_and_edge_lifecycle():
    from arc.core.registry import ComponentRegistry

    reg = ComponentRegistry()
    vec, graph = FakeVectorMemory(), FakeGraph()
    reg.register_extension("vector-memory", vec)
    reg.register_extension("knowledge-graph", graph)
    hooks = MemoryHooks(reg, "sess-1")
    assert hooks.enabled is True

    artifact = ArtifactRecord(
        artifact_id="a1", name="silicon band gap", description="bandgap calc",
        version="1", state="ready", path="/tmp/a1",
    )
    hooks.on_artifact_registered(artifact)
    assert "artifact:a1" in vec.docs
    assert any(e["relation"] == "produced" and e["dst"] == "artifact:a1"
               for e in graph.edges_list)

    execution = ExecutionResult(
        run_id="r1", status="completed", outputs={"band_gap": 1.1}, metrics={"x": 1},
    )
    hooks.on_result_saved(artifact, execution, {"thickness": 2})
    assert "result:r1" in vec.docs
    assert any(e["relation"] == "ran" and e["dst"] == "result:r1"
               for e in graph.edges_list)
    assert any(e["relation"] == "measured" and e["dst"] == "variable:band_gap"
               for e in graph.edges_list)

    review = ReviewResult(approved=True, summary="looks good", strengths=["a"])
    hooks.on_review_completed(artifact, review, run_id="r1")
    assert "review:r1" in vec.docs
    assert any(e["relation"] == "reviewed_as" for e in graph.edges_list)

    # Search surface returns indexed docs.
    assert hooks.search("band gap")


# ── Integration: full YAML workflow run indexes at lifecycle points ─────


@pytest.mark.asyncio
async def test_workflow_indexes_at_lifecycle_points():
    workflow = ResearchWorkflow()
    vec, graph = _install_fakes(workflow)

    result = await workflow.run_once(
        ResearchGoal(goal="parameter doubling test", target={"result": 2.0})
    )
    assert result["status"] == "completed"

    # An artifact doc, a result doc, and a review doc were all indexed.
    kinds = {d["metadata"].get("kind") for d in vec.docs.values()}
    assert {"artifact", "result", "review"} <= kinds

    relations = {e["relation"] for e in graph.edges_list}
    assert {"produced", "ran", "measured", "reviewed_as"} <= relations


@pytest.mark.asyncio
async def test_workflow_clean_without_extensions():
    """A normal run with no extensions registered must still complete and
    leave memory wiring as a no-op (memory_hooks.enabled is False)."""
    workflow = ResearchWorkflow()
    assert workflow.memory_hooks.enabled is False
    result = await workflow.run_once(
        ResearchGoal(goal="parameter doubling test", target={"result": 2.0})
    )
    assert result["status"] == "completed"


def test_context_exposes_memory_search():
    """Agents reach the search surface through AgentContext.memory."""
    workflow = ResearchWorkflow()
    _install_fakes(workflow)
    assert callable(workflow._context.memory["memory_search"])
    assert workflow._context.memory["memory_hooks"] is workflow.memory_hooks
