import pytest

from arc.assets.input_scan import scan_inputs_from_env
from arc.assets import FileStore
from arc.core.registry import ComponentRegistry
from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.research import ResearchGoal


class EchoSkill:
    async def execute(self, inputs, context):
        return inputs


def test_scan_inputs_from_env_registers_session_assets(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "paper.pdf").write_bytes(b"%PDF-1.4 pretend")
    (inputs / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    store = FileStore(tmp_path / "store")

    monkeypatch.setenv("ARC_INPUTS_DIR", str(inputs))

    assets = scan_inputs_from_env(store, session_id="s1")

    assert [asset.name for asset in assets] == ["data.csv", "paper.pdf"]
    assert {asset.role for asset in assets} == {"data", "paper"}
    assert all(asset.session_id == "s1" for asset in assets)
    assert all(asset.metadata["indexed"] is True for asset in assets)


def test_scan_inputs_index_mode_does_not_hash_or_copy(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "notes.txt").write_text("hello", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    monkeypatch.setenv("ARC_INPUTS_DIR", str(inputs))
    monkeypatch.setenv("ARC_INPUTS_IMPORT_MODE", "index")

    def _boom(path):
        raise AssertionError("startup scan index mode should not hash content")

    monkeypatch.setattr(store, "_hash_file", _boom)

    assets = scan_inputs_from_env(store, session_id="s1")

    assert [asset.name for asset in assets] == ["notes.txt"]
    assert assets[0].sha256 == ""
    assert not any(store.blob_root.iterdir())


def test_scan_inputs_uses_default_data_folder(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.txt").write_text("hello", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    monkeypatch.delenv("ARC_INPUTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assets = scan_inputs_from_env(store, session_id="s1")

    assert [asset.name for asset in assets] == ["notes.txt"]
    assert assets[0].role == "text"
    assert assets[0].metadata["source"] == "./data"


def test_research_workflow_scans_inputs_and_registers_default_loaders(
    tmp_path,
    monkeypatch,
):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "paper.pdf").write_bytes(b"%PDF-1.4 pretend")
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARC_INPUTS_DIR", str(inputs))

    workflow = ResearchWorkflow(registry=ComponentRegistry(), session_id="scan-session")

    assert "pdf_loader" in workflow.registry.list_loaders()
    assets = workflow._context.memory["input_assets"]
    assert len(assets) == 1
    assert assets[0].name == "paper.pdf"
    assert workflow._context.files is workflow.file_store


@pytest.mark.asyncio
async def test_workflow_file_input_auto_binds_and_loads_required_derivative(
    tmp_path,
    monkeypatch,
):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "paper.pdf").write_bytes(b"%PDF-1.4 pretend")
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARC_INPUTS_DIR", str(inputs))

    registry = ComponentRegistry()
    registry.register_skill("echo", EchoSkill())
    workflow = ResearchWorkflow(
        registry=registry,
        session_id="file-workflow",
        workflow_name="file-loop",
    )
    workflow.registry.register_workflow(
        "file-loop",
        {
            "name": "file-loop",
            "inputs": {
                "paper": {
                    "type": "file",
                    "role": "paper",
                    "media_type": "application/pdf",
                    "required": True,
                    "required_derivatives": [
                        {"role": "extracted_text", "media_type": "text/markdown"}
                    ],
                },
            },
            "steps": [
                {
                    "id": "echo",
                    "skill": "echo",
                    "input": {
                        "paper": "inputs.paper",
                        "paper_text": "inputs.paper_text",
                    },
                }
            ],
        },
    )

    result = await workflow.run_once(ResearchGoal(goal="Use the paper"))

    output = result["steps"]["echo"]
    assert output["paper"].startswith("file_")
    assert output["paper_text"].startswith("file_")
    text_asset = workflow.file_store.get(output["paper_text"])
    assert text_asset.derived_from == output["paper"]
    assert text_asset.role == "extracted_text"
