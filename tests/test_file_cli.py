import inspect
import json

from typer.testing import CliRunner

from arc.cli.main import app


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def test_file_cli_add_list_show_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    source = tmp_path / "paper.txt"
    source.write_text("hello arc", encoding="utf-8")
    runner = _runner()

    add = runner.invoke(
        app,
        ["file", "add", str(source), "--role", "text", "--session", "s1"],
    )
    assert add.exit_code == 0, add.output
    file_id = add.output.split()[0]
    assert file_id.startswith("file_")

    listed = runner.invoke(app, ["file", "list", "--session", "s1"])
    assert listed.exit_code == 0, listed.output
    assert file_id in listed.output
    assert "paper.txt" in listed.output

    shown = runner.invoke(app, ["file", "show", file_id, "--session", "s1"])
    assert shown.exit_code == 0, shown.output
    metadata = json.loads(shown.output)
    assert metadata["id"] == file_id
    assert metadata["role"] == "text"

    loaded = runner.invoke(
        app,
        ["file", "load", file_id, "--loader", "text_loader", "--session", "s1"],
    )
    assert loaded.exit_code == 0, loaded.output
    assert "normalized_text" in loaded.output
