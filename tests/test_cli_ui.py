"""CLI integration tests for ``arc ui``."""

import inspect

from typer.testing import CliRunner

from arc.cli.main import app
from arc.ui.__main__ import DEFAULT_HOST, DEFAULT_PORT


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def test_arc_ui_uses_browser_ui_default_port(monkeypatch):
    calls = []

    def fake_run_server(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("arc.ui.__main__.run_server", fake_run_server)

    result = _runner().invoke(app, ["ui"])

    assert result.exit_code == 0
    assert calls == [{"host": DEFAULT_HOST, "port": DEFAULT_PORT, "reload": False}]
    assert DEFAULT_PORT == 8080


def test_arc_ui_forwards_host_port_and_reload(monkeypatch):
    calls = []

    def fake_run_server(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("arc.ui.__main__.run_server", fake_run_server)

    result = _runner().invoke(
        app,
        ["ui", "--host", "0.0.0.0", "--port", "9090", "--reload"],
    )

    assert result.exit_code == 0
    assert calls == [{"host": "0.0.0.0", "port": 9090, "reload": True}]


def test_arc_ui_help_lists_default_port():
    result = _runner().invoke(app, ["ui", "--help"])

    assert result.exit_code == 0
    assert "8080" in result.stdout
    assert "standalone ARC browser UI" in result.stdout
