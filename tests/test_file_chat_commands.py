import pytest

from arc.chat.commands import build_registry
from arc.chat.commands.files import file_handler
from arc.chat.state import ChatState
from arc.core.registry import ComponentRegistry
from arc.orchestrator.workflow import ResearchWorkflow


@pytest.mark.asyncio
async def test_file_chat_command_add_list_and_load(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    source = tmp_path / "notes.txt"
    source.write_text("hello chat", encoding="utf-8")
    workflow = ResearchWorkflow(registry=ComponentRegistry(), session_id="chat-files")
    state = ChatState(workflow)

    await file_handler(state, ["add", str(source), "text"])
    added = capsys.readouterr().out
    assert "Attached notes.txt as file_" in added
    file_id = added.split(" as ", 1)[1].split(".", 1)[0]

    await file_handler(state, ["list"])
    listed = capsys.readouterr().out
    assert file_id in listed
    assert "notes.txt" in listed

    await file_handler(state, ["load", file_id, "text_loader"])
    loaded = capsys.readouterr().out
    assert "derived asset" in loaded
    assert "normalized_text" in loaded


def test_file_chat_command_registered():
    registry = build_registry()
    lookup = registry.lookup("/file list")
    assert lookup.command is not None
    assert lookup.command.name == "file"
    assert lookup.argv == ["list"]
