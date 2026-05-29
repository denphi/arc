"""Docker runtime adapter (Item 5) + the shared submit→poll→collect base.

No live Docker daemon — ``subprocess.run`` and ``shutil.which`` are
mocked to drive the adapter through its states. Verifies the full
round-trip (submit → poll until exited(0) → collect outputs), the
not-runnable path, and a non-zero exit → error.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc.core.loader import _import_class

pytestmark = pytest.mark.chat

_Adapter = _import_class("arc.packages.arc-docker.adapter:DockerRuntimeAdapter")


def _artifact(tmp_path):
    art = tmp_path / "art" / "0.1.0"
    art.mkdir(parents=True)
    (art / "workflow.py").write_text("def simulate(x=1.0, y=2.0):\n    return {'z': x + y}\n")
    (art / "sim2l.yaml").write_text(
        "inputs:\n  x: {type: Number, default: 1.0}\n  y: {type: Number, default: 2.0}\n"
        "outputs:\n  z: {type: Number}\n"
    )
    return SimpleNamespace(artifact_id="d1", name="sim", version="0.1.0",
                           path=str(art), metadata={})


class _FakeDocker:
    """Scripts `docker` CLI behaviour through subprocess.run mocking.

    Simulates: `docker info` ok, `run -d` returns a container, `inspect`
    reports running once then exited:<code>, and writes the result file
    the way a real container would (so _collect finds it).
    """

    def __init__(self, *, exit_code="0", emit_result=True):
        self.exit_code = exit_code
        self.emit_result = emit_result
        self._polls = 0
        self._out_dir = None

    def run(self, cmd, capture_output=True, text=False, timeout=None):
        c = cmd[1] if len(cmd) > 1 else ""
        if c == "info":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if c == "run":
            # Find the mounted output dir (-v <staging>/out:/arc/out) and
            # write the container's result there immediately.
            for tok in cmd:
                if isinstance(tok, str) and tok.endswith("/arc/out"):
                    self._out_dir = Path(tok.split(":")[0])
            if self.emit_result and self._out_dir:
                (self._out_dir).mkdir(parents=True, exist_ok=True)
                (self._out_dir / "result.json").write_text(
                    json.dumps({"ok": True, "outputs": {"z": 7.0}})
                )
            return SimpleNamespace(returncode=0, stdout="container123\n", stderr="")
        if c == "inspect":
            self._polls += 1
            state = "running:0" if self._polls < 2 else f"exited:{self.exit_code}"
            return SimpleNamespace(returncode=0, stdout=state + "\n", stderr="")
        if c == "logs":
            return SimpleNamespace(returncode=0, stdout="container log line", stderr="")
        if c in ("rm", "kill"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


_mod = sys.modules[_Adapter.__module__]  # the loaded (synthetic-name) module


def _patch(monkeypatch, fake, *, which=True):
    monkeypatch.setattr(_mod.subprocess, "run", fake.run)
    monkeypatch.setattr(_mod.shutil, "which", lambda name: "/usr/bin/docker" if which else None)
    monkeypatch.setattr(_Adapter, "poll_interval", 0.0, raising=False)


def test_run_completes_via_submit_poll_collect(monkeypatch, tmp_path):
    fake = _FakeDocker()
    _patch(monkeypatch, fake)
    adapter = _Adapter()
    art = _artifact(tmp_path)
    inputs = asyncio.run(adapter.prepare_inputs(art, {"x": 3.0, "y": 4.0}))
    result = asyncio.run(adapter.run(art, inputs))
    assert result.status == "completed"
    assert result.outputs == {"z": 7.0}
    assert result.metrics["execution_success"] is True
    assert result.metrics["backend"] == "docker"


def test_not_runnable_without_daemon(monkeypatch, tmp_path):
    fake = _FakeDocker()
    _patch(monkeypatch, fake, which=False)   # docker not on PATH
    adapter = _Adapter()
    result = asyncio.run(adapter.run(_artifact(tmp_path), {"x": 1.0}))
    assert result.status == "error"
    assert result.metrics["reason"] == "backend_unavailable"


def test_nonzero_exit_is_error(monkeypatch, tmp_path):
    fake = _FakeDocker(exit_code="1", emit_result=False)
    _patch(monkeypatch, fake)
    adapter = _Adapter()
    result = asyncio.run(adapter.run(_artifact(tmp_path), {"x": 1.0}))
    assert result.status == "error"


def test_validate_artifact_ok(monkeypatch, tmp_path):
    fake = _FakeDocker()
    _patch(monkeypatch, fake)
    vr = asyncio.run(_Adapter().validate_artifact(_artifact(tmp_path)))
    assert vr.valid is True


def test_validate_artifact_missing_workflow(tmp_path):
    rec = SimpleNamespace(artifact_id="x", name="x", version="0.1.0",
                          path=str(tmp_path), metadata={})
    vr = asyncio.run(_Adapter().validate_artifact(rec))
    assert vr.valid is False
