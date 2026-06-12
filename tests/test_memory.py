"""Tests for the memory layer."""

import pytest

from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.results_store import ResultsStore
from arc.memory.provenance import ProvenanceLog
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.execution import ExecutionResult
from arc.session import session_paths


def test_artifact_registry_register_and_get(tmp_path):
    registry = ArtifactRegistry(root=str(tmp_path / "artifacts"))
    draft = ArtifactDraft(
        name="test-artifact",
        description="A test",
        files={"workflow.py": "result = 1", "sim2l.yaml": "name: test"},
    )
    record = registry.register(draft)
    assert record.artifact_id
    assert record.state == "REGISTERED"
    assert record.description == "A test"
    assert record.metadata["description"] == "A test"

    retrieved = registry.get(record.artifact_id, record.version)
    assert retrieved.artifact_id == record.artifact_id
    assert retrieved.name == "test-artifact"
    assert retrieved.description == "A test"


def test_artifact_registry_rejects_path_traversal(tmp_path):
    registry = ArtifactRegistry(root=str(tmp_path / "artifacts"))
    draft = ArtifactDraft(
        name="bad-artifact",
        description="A bad path",
        files={"../../escape.py": "print('no')"},
    )
    with pytest.raises(ValueError):
        registry.register(draft)


def test_session_paths_reject_path_traversal():
    with pytest.raises(ValueError):
        session_paths("../escape")


def test_artifact_registry_list(tmp_path):
    registry = ArtifactRegistry(root=str(tmp_path / "artifacts"))
    for i in range(3):
        draft = ArtifactDraft(name=f"artifact-{i}", description="test", files={})
        registry.register(draft)
    all_records = registry.list_all()
    assert len(all_records) == 3


def test_results_store_save_and_get(tmp_path):
    store = ResultsStore(root=str(tmp_path / "runs"))
    result = ExecutionResult(
        run_id="test-run-id",
        status="completed",
        outputs={"result": 2.0},
        metrics={"execution_success": True},
    )
    path = store.save(result)
    assert path

    retrieved = store.get("test-run-id")
    assert retrieved.run_id == "test-run-id"
    assert retrieved.outputs["result"] == 2.0


def test_results_store_rejects_unsafe_run_id(tmp_path):
    store = ResultsStore(root=str(tmp_path / "runs"))
    with pytest.raises(ValueError):
        store.get("../escape")


def test_provenance_log_record_and_read(tmp_path):
    log = ProvenanceLog(log_path=str(tmp_path / "provenance.jsonl"))
    log.record(
        session_id="test-session",
        action="execute",
        agent="adapter",
        run_id="run-123",
        outputs={"result": 2.0},
    )
    entries = log.read_session("test-session")
    assert len(entries) == 1
    assert entries[0]["action"] == "execute"
    assert entries[0]["run_id"] == "run-123"


def test_provenance_log_truncates_oversized_values(tmp_path):
    """A huge agent output must not be stored verbatim — strings and
    collections are size-capped at record time."""
    log = ProvenanceLog(log_path=str(tmp_path / "provenance.jsonl"))
    log.record(
        session_id="s",
        action="execute",
        agent="adapter",
        outputs={
            "blob": "x" * 100_000,
            "sweep": list(range(10_000)),
        },
    )
    entry = log.read_session("s")[0]
    assert len(entry["outputs"]["blob"]) < 5_000
    assert "chars" in entry["outputs"]["blob"]  # truncation marker
    assert len(entry["outputs"]["sweep"]) < 1_000


def test_provenance_log_rotates_at_size_cap(tmp_path, monkeypatch):
    """Past the size cap the file rotates to ``.1`` (one generation kept —
    the durable copy is whatever the backend published). read_session sees
    both generations and the current file never grows unbounded."""
    monkeypatch.setenv("ARC_PROVENANCE_MAX_BYTES", "500")
    log = ProvenanceLog(log_path=str(tmp_path / "provenance.jsonl"))
    for i in range(20):
        log.record(session_id="s", action=f"step-{i}", agent="agent")

    rotated = tmp_path / "provenance.jsonl.1"
    assert rotated.exists() and rotated.stat().st_size > 0
    # The current file stays near the cap instead of growing unbounded.
    assert (tmp_path / "provenance.jsonl").stat().st_size < 1500

    entries = log.read_session("s")
    assert 0 < len(entries) <= 20
    # The most recent entry is always readable.
    assert entries[-1]["action"] == "step-19"


def test_provenance_log_drain_and_requeue(tmp_path):
    log = ProvenanceLog(log_path=str(tmp_path / "provenance.jsonl"))
    log.record(session_id="s", action="a", agent="x")
    log.record(session_id="s", action="b", agent="x")

    drained = log.drain_unpublished()
    assert [e["action"] for e in drained] == ["a", "b"]
    assert log.drain_unpublished() == []

    # Requeued entries come back first, before newer ones.
    log.requeue_unpublished(drained)
    log.record(session_id="s", action="c", agent="x")
    assert [e["action"] for e in log.drain_unpublished()] == ["a", "b", "c"]


def test_load_sim2l_schema_preserves_field_keys_and_omits_defaults(tmp_path):
    """units/min/max/choices survive normalization, and a field with no
    declared default gets none fabricated."""
    from arc.sim2l_schema import load_sim2l_schema

    (tmp_path / "sim2l.yaml").write_text(
        "inputs:\n"
        "  temperature:\n"
        "    type: Number\n"
        "    default: 300.0\n"
        "    units: K\n"
        "    min: 0.0\n"
        "    max: 5000.0\n"
        "  element:\n"
        "    type: Text\n"
        "  shorthand: 2.5\n"
        "outputs:\n"
        "  band_gap:\n"
        "    type: Number\n"
        "    units: eV\n"
    )
    inputs, outputs = load_sim2l_schema(tmp_path)

    t = inputs["temperature"]
    assert (t["units"], t["min"], t["max"], t["default"]) == ("K", 0.0, 5000.0, 300.0)
    # No declared default → none fabricated (the old code injected 1.0).
    assert "default" not in inputs["element"]
    assert inputs["element"]["type"] == "Text"
    # Shorthand entries keep the value as default with an inferred type.
    assert inputs["shorthand"] == {
        "type": "Number", "description": "shorthand", "default": 2.5,
    }
    assert outputs["band_gap"]["units"] == "eV"
    assert "default" not in outputs["band_gap"]


def test_results_store_list_page_most_recent_first(tmp_path):
    import time

    store = ResultsStore(root=str(tmp_path / "runs"))
    for i in range(5):
        store.save(ExecutionResult(run_id=f"run-{i}", status="completed"))
        time.sleep(0.01)  # distinct mtimes

    page = store.list_page(limit=2)
    assert [r.run_id for r in page] == ["run-4", "run-3"]
    page2 = store.list_page(limit=2, offset=2)
    assert [r.run_id for r in page2] == ["run-2", "run-1"]
    # limit <= 0 → everything from the offset.
    assert len(store.list_page(limit=0)) == 5


def test_execution_result_carries_inputs():
    """inputs is first-class on ExecutionResult (sweep points carry their
    own parameter combination through bookkeeping)."""
    r = ExecutionResult(run_id="r", status="completed", inputs={"x": 2.0})
    assert r.inputs == {"x": 2.0}
    # Default stays an empty dict for old payloads.
    assert ExecutionResult(run_id="r2", status="completed").inputs == {}
