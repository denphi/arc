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
