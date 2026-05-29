"""Slurm + Kubernetes runtime adapters (Item 6).

Both reuse core's BaseSubmitPollAdapter; these tests mock the respective
CLI (sbatch/sacct, kubectl) to drive submit→poll→collect without a live
cluster, plus the not-runnable path.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from arc.core.loader import _import_class

pytestmark = pytest.mark.chat

_Slurm = _import_class("arc.packages.arc-slurm.adapter:SlurmRuntimeAdapter")
_K8s = _import_class("arc.packages.arc-k8s.adapter:KubernetesRuntimeAdapter")
_slurm_mod = sys.modules[_Slurm.__module__]
_k8s_mod = sys.modules[_K8s.__module__]


def _artifact(tmp_path):
    art = tmp_path / "art" / "0.1.0"
    art.mkdir(parents=True)
    (art / "workflow.py").write_text("def simulate(x=1.0, y=2.0):\n    return {'z': x + y}\n")
    (art / "sim2l.yaml").write_text(
        "inputs:\n  x: {type: Number, default: 1.0}\n  y: {type: Number, default: 2.0}\n"
        "outputs:\n  z: {type: Number}\n"
    )
    return SimpleNamespace(artifact_id="r1", name="sim", version="0.1.0",
                           path=str(art), metadata={})


# ── Slurm ───────────────────────────────────────────────────────────────


class _FakeSbatch:
    def __init__(self):
        self._polls = 0
        self._job_dir = None

    def run(self, cmd, capture_output=True, text=False, timeout=None, input=None):
        exe = cmd[0]
        if exe == "sbatch":
            # last arg is the script path; its dir is the job dir.
            from pathlib import Path
            self._job_dir = Path(cmd[-1]).parent
            # The "job" runs immediately: write the result the runner would.
            (self._job_dir / "result.json").write_text(
                json.dumps({"ok": True, "outputs": {"z": 7.0}}))
            (self._job_dir / "slurm.out").write_text("job log")
            return SimpleNamespace(returncode=0, stdout="12345\n", stderr="")
        if exe == "sacct":
            self._polls += 1
            state = "RUNNING" if self._polls < 2 else "COMPLETED"
            return SimpleNamespace(returncode=0, stdout=state + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch_slurm(monkeypatch, fake, *, present=True):
    monkeypatch.setattr(_slurm_mod.subprocess, "run", fake.run)
    monkeypatch.setattr(_slurm_mod.shutil, "which",
                        lambda n: "/usr/bin/" + n if present else None)
    monkeypatch.setattr(_Slurm, "poll_interval", 0.0, raising=False)


def test_slurm_run_completes(monkeypatch, tmp_path):
    fake = _FakeSbatch()
    _patch_slurm(monkeypatch, fake)
    monkeypatch.setenv("ARC_SLURM_SCRATCH", str(tmp_path / "scratch"))
    adapter = _Slurm()
    art = _artifact(tmp_path)
    inputs = asyncio.run(adapter.prepare_inputs(art, {"x": 3.0, "y": 4.0}))
    result = asyncio.run(adapter.run(art, inputs))
    assert result.status == "completed"
    assert result.outputs == {"z": 7.0}
    assert result.metrics["backend"] == "slurm"


def test_slurm_not_runnable_without_cli(monkeypatch, tmp_path):
    _patch_slurm(monkeypatch, _FakeSbatch(), present=False)
    result = asyncio.run(_Slurm().run(_artifact(tmp_path), {"x": 1.0}))
    assert result.status == "error"
    assert result.metrics["reason"] == "backend_unavailable"


# ── Kubernetes ──────────────────────────────────────────────────────────


class _FakeKubectl:
    def __init__(self):
        self._polls = 0

    def run(self, cmd, capture_output=True, text=False, timeout=None, input=None):
        joined = " ".join(str(c) for c in cmd)
        if "version" in joined:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "create configmap" in joined or "apply" in joined or "delete" in joined:
            return SimpleNamespace(returncode=0, stdout="created", stderr="")
        if "get job" in joined:
            self._polls += 1
            # status.succeeded:failed
            out = "0:0" if self._polls < 2 else "1:0"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if "logs" in joined:
            line = "ARC_RESULT::" + json.dumps({"ok": True, "outputs": {"z": 7.0}})
            return SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch_k8s(monkeypatch, fake, *, present=True):
    monkeypatch.setattr(_k8s_mod.subprocess, "run", fake.run)
    monkeypatch.setattr(_k8s_mod.shutil, "which",
                        lambda n: "/usr/bin/kubectl" if present else None)
    monkeypatch.setattr(_K8s, "poll_interval", 0.0, raising=False)


def test_k8s_run_completes(monkeypatch, tmp_path):
    _patch_k8s(monkeypatch, _FakeKubectl())
    adapter = _K8s()
    art = _artifact(tmp_path)
    inputs = asyncio.run(adapter.prepare_inputs(art, {"x": 3.0, "y": 4.0}))
    result = asyncio.run(adapter.run(art, inputs))
    assert result.status == "completed"
    assert result.outputs == {"z": 7.0}
    assert result.metrics["backend"] == "k8s"


def test_k8s_not_runnable_without_kubectl(monkeypatch, tmp_path):
    _patch_k8s(monkeypatch, _FakeKubectl(), present=False)
    result = asyncio.run(_K8s().run(_artifact(tmp_path), {"x": 1.0}))
    assert result.status == "error"


def test_both_adapters_resolve_via_env(monkeypatch):
    """ARC_RUNTIME_ADAPTER=slurm|k8s resolves the package adapters."""
    from arc.orchestrator.workflow import _build_adapter
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "slurm")
    assert type(_build_adapter()).__name__ == "SlurmRuntimeAdapter"
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "k8s")
    assert type(_build_adapter()).__name__ == "KubernetesRuntimeAdapter"
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "kubernetes")
    assert type(_build_adapter()).__name__ == "KubernetesRuntimeAdapter"
