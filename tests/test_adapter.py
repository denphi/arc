"""Unit tests for the LocalRuntimeAdapter."""

import pytest

from arc.runtime.local import LocalRuntimeAdapter
from arc.runtime.sim2l_adapter import _import_workflow_func
from arc.schemas.artifact import ArtifactRecord


@pytest.fixture
def adapter():
    return LocalRuntimeAdapter()


@pytest.fixture
def artifact(tmp_path):
    return ArtifactRecord(
        artifact_id="test-id",
        name="test",
        version="0.1.0",
        state="REGISTERED",
        path=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_run_returns_completed_status(adapter, artifact):
    result = await adapter.run(artifact, {"input_parameter": 2.0})
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_run_doubles_input(adapter, artifact):
    result = await adapter.run(artifact, {"input_parameter": 3.0})
    assert result.outputs["result"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_run_sweep_returns_multiple_results(adapter, artifact):
    results = await adapter.run_sweep(artifact, {"input_parameter": [1.0, 2.0, 3.0]})
    assert len(results) == 3
    assert all(r.status == "completed" for r in results)


@pytest.mark.asyncio
async def test_run_sweep_uses_cartesian_product(adapter, artifact):
    results = await adapter.run_sweep(
        artifact,
        {"input_parameter": [1.0, 2.0], "other": [10.0, 20.0, 30.0]},
    )
    assert len(results) == 6
    assert all("other" in r.logs[1] for r in results)


@pytest.mark.asyncio
async def test_validate_artifact_missing_path(adapter):
    bad_artifact = ArtifactRecord(
        artifact_id="bad",
        name="bad",
        version="0.1.0",
        state="DRAFT",
        path="/nonexistent/path/to/artifact",
    )
    result = await adapter.validate_artifact(bad_artifact)
    assert not result.valid
    assert len(result.errors) > 0


@pytest.mark.asyncio
async def test_get_status_returns_completed(adapter):
    status = await adapter.get_status("any-run-id")
    assert status == "completed"


def test_import_workflow_func_reloads_changed_source(tmp_path):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("def simulate(**inputs):\n    return {'result': 1}\n")
    func = _import_workflow_func(str(tmp_path), "same-artifact")
    assert func()["result"] == 1

    workflow.write_text("def simulate(**inputs):\n    return {'result': 2}\n")
    func = _import_workflow_func(str(tmp_path), "same-artifact")
    assert func()["result"] == 2


def test_import_workflow_func_times_out_top_level_hang(tmp_path):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("while True:\n    pass\n\ndef simulate(**inputs):\n    return {'result': 1}\n")
    with pytest.raises(TimeoutError):
        _import_workflow_func(str(tmp_path), "hanging-artifact")
