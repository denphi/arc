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
    """An artifact with a real workflow.py that doubles input_parameter.

    LocalRuntimeAdapter requires a workflow.py — the old "silent value*2"
    fallback was removed in review item #21 because it let downstream
    reviewers think the artifact ran successfully when it had not.
    """
    (tmp_path / "workflow.py").write_text(
        "def simulate(**inputs):\n"
        "    x = inputs.get('input_parameter', 1.0)\n"
        "    return {'result': x * 2}\n"
    )
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
async def test_run_errors_when_workflow_missing(adapter, tmp_path):
    """No workflow.py -> explicit error result, not a fake demo output."""
    bare = ArtifactRecord(
        artifact_id="bare",
        name="bare",
        version="0.1.0",
        state="REGISTERED",
        path=str(tmp_path),
    )
    result = await adapter.run(bare, {"input_parameter": 1.0})
    assert result.status == "error"
    assert result.outputs == {}
    assert any("workflow.py" in line for line in result.logs)


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
async def test_get_status_unknown_without_store(adapter):
    """A bare adapter (no session ResultsStore) can't vouch for a run —
    it must say "unknown", not pretend any id completed."""
    status = await adapter.get_status("any-run-id")
    assert status == "unknown"


@pytest.mark.asyncio
async def test_status_and_collect_answer_from_results_store(artifact, tmp_path):
    """With the session ResultsStore wired in, get_status / collect_*
    answer from the saved run instead of empties."""
    from arc.memory.results_store import ResultsStore

    store = ResultsStore(root=str(tmp_path / "runs"))
    adapter = LocalRuntimeAdapter(results_store=store)
    result = await adapter.run(artifact, {"input_parameter": 2.0})
    store.save(result)

    assert await adapter.get_status(result.run_id) == "completed"
    assert await adapter.collect_outputs(result.run_id) == result.outputs
    assert await adapter.collect_logs(result.run_id) == result.logs
    assert (await adapter.collect_metrics(result.run_id))["execution_success"] is True
    # Unknown ids stay unknown/empty.
    assert await adapter.get_status("nope") == "unknown"
    assert await adapter.collect_outputs("nope") == {}


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


# ── Sim2L adapter: schema indexing + catalog identity ─────────────────────


def _sim2l_artifact(tmp_path, yaml_text):
    (tmp_path / "workflow.py").write_text(
        "def simulate(**inputs):\n    return {'result': 1.0}\n"
    )
    (tmp_path / "sim2l.yaml").write_text(yaml_text)
    return ArtifactRecord(
        artifact_id="schema-test",
        name="schema-test",
        version="0.1.0",
        state="REGISTERED",
        path=str(tmp_path),
    )


def test_schemas_for_artifact_preserve_declared_types(tmp_path):
    """sim2l.yaml types must survive into the indexed schemas — not be
    flattened to Number (harness review, sim2l-persistence finding 1)."""
    sim2l = pytest.importorskip("sim2l")  # noqa: F841
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    artifact = _sim2l_artifact(tmp_path, (
        "inputs:\n"
        "  temperature:\n"
        "    type: Number\n"
        "    default: 300.0\n"
        "    units: K\n"
        "    min: 0.0\n"
        "  element:\n"
        "    type: Text\n"
        "    default: Si\n"
        "  converge:\n"
        "    type: Boolean\n"
        "    default: true\n"
        "outputs:\n"
        "  band_gap:\n"
        "    type: Number\n"
        "  label:\n"
        "    type: Text\n"
    ))
    adapter = Sim2LRuntimeAdapter(db_path=str(tmp_path / "sim.db"))
    in_schema, out_schema = adapter._schemas_for_artifact(artifact)

    in_dict = in_schema.to_dict()
    out_dict = out_schema.to_dict()
    assert in_dict["element"]["type"] == "Text"
    assert in_dict["element"]["default"] == "Si"
    assert in_dict["converge"]["type"] == "Boolean"
    assert in_dict["temperature"]["type"] == "Number"
    assert in_dict["temperature"]["min"] == 0.0
    assert out_dict["label"]["type"] == "Text"
    assert out_dict["band_gap"]["type"] == "Number"


def test_schemas_for_artifact_normalizes_alias_types(tmp_path):
    """Lowercase / pythonic spellings resolve to canonical field types,
    and unknown types fall back to Text (never silently Number)."""
    pytest.importorskip("sim2l")
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    artifact = _sim2l_artifact(tmp_path, (
        "inputs:\n"
        "  a: {type: float, default: 1.5}\n"
        "  b: {type: string, default: hi}\n"
        "  c: {type: WeirdCustomType, default: zz}\n"
        "outputs:\n"
        "  r: {type: int}\n"
    ))
    adapter = Sim2LRuntimeAdapter(db_path=str(tmp_path / "sim.db"))
    in_schema, out_schema = adapter._schemas_for_artifact(artifact)
    in_dict = in_schema.to_dict()
    assert in_dict["a"]["type"] == "Number"
    assert in_dict["b"]["type"] == "Text"
    assert in_dict["c"]["type"] == "Text"
    assert out_schema.to_dict()["r"]["type"] == "Integer"


def test_sim_name_disambiguates_long_names():
    """Two long artifact names sharing a 50-char prefix must not collide
    on the same catalog identity."""
    from arc.runtime.sim2l_adapter import _sim_name_for_artifact

    prefix = "a" * 60
    n1 = _sim_name_for_artifact(prefix + "-variant-one")
    n2 = _sim_name_for_artifact(prefix + "-variant-two")
    assert len(n1) <= 50 and len(n2) <= 50
    assert n1 != n2
    # Deterministic: same input → same name.
    assert n1 == _sim_name_for_artifact(prefix + "-variant-one")
    # Short names pass through unchanged.
    assert _sim_name_for_artifact("short") == "short"


def test_function_workflow_bundle_includes_all_artifact_files(tmp_path):
    """The catalog bundle carries the whole artifact directory — schema,
    tests, provenance snapshot — not just workflow.py."""
    from arc.runtime.sim2l_adapter import _function_workflow_bundle

    (tmp_path / "workflow.py").write_text("def simulate():\n    return {}\n")
    (tmp_path / "sim2l.yaml").write_text("inputs: {}\n")
    (tmp_path / "arc_record.json").write_text("{}")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_workflow.py").write_text("def test_ok(): pass\n")

    bundle = _function_workflow_bundle(
        (tmp_path / "workflow.py").read_text(), artifact_dir=tmp_path,
    )
    paths = {f["path"] for f in bundle["files"]}
    assert paths == {"workflow.py", "sim2l.yaml", "arc_record.json",
                     "tests/test_workflow.py"}
    assert bundle["entrypoint"] == "workflow.py"
    # Every file entry carries a content hash for integrity checking.
    assert all(f.get("sha256") for f in bundle["files"])
