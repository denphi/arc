"""Parity guard: every remote backend's in-job runner must JSON-normalize
non-native outputs (numpy scalars/arrays) the same way.

The docker/slurm/k8s adapters each embed a ``_RUNNER`` string that is
exec'd *inside* the container/job/pod. A workflow that returns a numpy
scalar (``.item()``) or array (``.tolist()``) must not crash the in-job
``json.dump``/``json.dumps``. Docker always normalized; slurm + k8s did
not until parity was restored — this test locks all three together.

We execute each runner string in a real subprocess (the same way the
backend would), feeding a workflow whose ``simulate`` returns a stub
numpy-like object, and assert the emitted JSON carries the normalized
value.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arc.core.loader import _import_class

pytestmark = pytest.mark.chat

_docker_mod = sys.modules[_import_class("arc.packages.arc-docker.adapter:DockerRuntimeAdapter").__module__]
_slurm_mod = sys.modules[_import_class("arc.packages.arc-slurm.adapter:SlurmRuntimeAdapter").__module__]
_k8s_mod = sys.modules[_import_class("arc.packages.arc-k8s.adapter:KubernetesRuntimeAdapter").__module__]

# A workflow whose simulate() returns objects that are NOT natively JSON
# serializable but expose numpy's .item()/.tolist() — the exact shape the
# runners' default= hook handles.
_NUMPY_LIKE_WORKFLOW = """
class _Scalar:
    def __init__(self, v): self._v = v
    def item(self): return self._v
class _Array:
    def __init__(self, v): self._v = v
    def tolist(self): return self._v
def simulate(x=1.0):
    return {"scalar": _Scalar(42.0), "array": _Array([1, 2, 3])}
"""


def _run_docker_runner(tmp_path: Path) -> dict:
    """Drive the docker _RUNNER with its /arc/* layout under tmp_path."""
    root = tmp_path / "docker"
    (root / "art").mkdir(parents=True)
    (root / "in").mkdir(parents=True)
    (root / "out").mkdir(parents=True)
    (root / "art" / "workflow.py").write_text(_NUMPY_LIKE_WORKFLOW)
    (root / "in" / "inputs.json").write_text(json.dumps({"x": 1.0}))
    # The runner hardcodes /arc/* paths; rewrite to the tmp layout.
    runner = _docker_mod._RUNNER.replace("/arc/", str(root) + "/")
    script = root / "runner.py"
    script.write_text(runner)
    subprocess.run([sys.executable, str(script)], check=True, timeout=30)
    return json.loads((root / "out" / "result.json").read_text())


def _run_slurm_runner(tmp_path: Path) -> dict:
    job = tmp_path / "slurm"
    job.mkdir(parents=True)
    (job / "workflow.py").write_text(_NUMPY_LIKE_WORKFLOW)
    (job / "inputs.json").write_text(json.dumps({"x": 1.0}))
    script = job / "runner.py"
    script.write_text(_slurm_mod._RUNNER)
    subprocess.run([sys.executable, str(script)], check=True, timeout=30,
                   env={"ARC_JOB_DIR": str(job), "PATH": "/usr/bin:/bin"})
    return json.loads((job / "result.json").read_text())


def _run_k8s_runner(tmp_path: Path) -> dict:
    root = tmp_path / "k8s"
    (root / "arc").mkdir(parents=True)
    (root / "arc" / "workflow.py").write_text(_NUMPY_LIKE_WORKFLOW)
    (root / "arc" / "inputs.json").write_text(json.dumps({"x": 1.0}))
    runner = _k8s_mod._RUNNER.replace("/arc/", str(root / "arc") + "/")
    script = root / "runner.py"
    script.write_text(runner)
    proc = subprocess.run([sys.executable, str(script)], check=True, timeout=30,
                          capture_output=True, text=True)
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("ARC_RESULT::"))
    return json.loads(line[len("ARC_RESULT::"):])


@pytest.mark.parametrize("runner", [_run_docker_runner, _run_slurm_runner, _run_k8s_runner])
def test_runner_normalizes_numpy_like_outputs(runner, tmp_path):
    data = runner(tmp_path)
    assert data["ok"] is True, data
    assert data["outputs"] == {"scalar": 42.0, "array": [1, 2, 3]}
