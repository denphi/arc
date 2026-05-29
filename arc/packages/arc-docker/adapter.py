"""Docker runtime adapter — runs a workflow's ``simulate()`` in a container.

Core defines ``RuntimeAdapterContract`` and the reusable
``BaseSubmitPollAdapter`` (submit→poll→collect orchestration, timeout,
schema reconciliation). This package supplies the Docker *backend*: the
four hooks that talk to the daemon. Per the project's threat model this
gives **real isolation** (a fresh container, ``--network none`` by
default) — stronger than the local executor's process-only isolation.

Implementation detail: we shell out to the ``docker`` CLI rather than
requiring the ``docker`` Python SDK, so the package has no hard
dependency. The artifact dir is mounted read-only; inputs go in via a
mounted JSON file (not argv) to dodge ARG_MAX; the container writes its
result JSON to a mounted output dir.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from arc.runtime._adapter_common import BaseSubmitPollAdapter
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)

# Runs inside the container: exec the workflow, call simulate(**inputs),
# write the JSON-normalised outputs to /arc/out/result.json.
_RUNNER = r"""
import json, sys, types, traceback
src = open("/arc/art/workflow.py").read()
inputs = json.load(open("/arc/in/inputs.json"))
mod = types.ModuleType("wf")
out = {"ok": False, "error": "no result"}
try:
    exec(compile(src, "workflow.py", "exec"), mod.__dict__)
    fn = getattr(mod, "simulate", None)
    if not callable(fn):
        out = {"ok": False, "error": "simulate() not defined"}
    else:
        r = fn(**inputs)
        if not isinstance(r, dict):
            out = {"ok": False, "error": "simulate() must return dict"}
        else:
            def _d(v):
                if hasattr(v, "tolist"): return v.tolist()
                if hasattr(v, "item"): return v.item()
                raise TypeError(type(v).__name__)
            out = {"ok": True, "outputs": json.loads(json.dumps(r, default=_d))}
except Exception as exc:
    out = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
json.dump(out, open("/arc/out/result.json", "w"))
"""


class DockerRuntimeAdapter(BaseSubmitPollAdapter):
    backend_name = "docker"

    def __init__(self, db_path=None, session_id=None, **_):
        self.image = os.environ.get("ARC_DOCKER_IMAGE", "python:3.11-slim")
        self.network = os.environ.get("ARC_DOCKER_NETWORK", "none")
        self.cpus = os.environ.get("ARC_DOCKER_CPUS", "")
        self.memory = os.environ.get("ARC_DOCKER_MEMORY", "")
        self._docker = os.environ.get("ARC_DOCKER_COMMAND", "docker")
        # native_id (container name) -> staging dir, kept so _collect can
        # read the output the container wrote.
        self._staging: dict[str, Path] = {}

    # ── backend hooks ──
    def is_runnable(self) -> bool:
        exe = shutil.which(self._docker)
        if not exe:
            return False
        try:
            r = subprocess.run([self._docker, "info"], capture_output=True, timeout=10)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _submit(self, artifact: ArtifactRecord, inputs: dict) -> str:
        art_dir = Path(artifact.path).resolve()
        if not (art_dir / "workflow.py").exists():
            raise FileNotFoundError(f"artifact has no workflow.py at {art_dir}")

        staging = Path(tempfile.mkdtemp(prefix="arc_docker_"))
        (staging / "in").mkdir()
        (staging / "out").mkdir()
        (staging / "in" / "inputs.json").write_text(json.dumps(inputs or {}))
        (staging / "runner.py").write_text(_RUNNER)

        name = f"arc-{uuid.uuid4().hex[:12]}"
        cmd = [
            self._docker, "run", "-d", "--name", name,
            "--network", self.network,
            "-v", f"{art_dir}:/arc/art:ro",
            "-v", f"{staging / 'in'}:/arc/in:ro",
            "-v", f"{staging / 'out'}:/arc/out",
            "-v", f"{staging / 'runner.py'}:/arc/runner.py:ro",
        ]
        if self.cpus:
            cmd += ["--cpus", self.cpus]
        if self.memory:
            cmd += ["--memory", self.memory]
        cmd += [self.image, "python", "/arc/runner.py"]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()[:300]}")
        self._staging[name] = staging
        return name

    def _poll_status(self, native_id: str) -> str:
        try:
            r = subprocess.run(
                [self._docker, "inspect", "-f", "{{.State.Status}}:{{.State.ExitCode}}", native_id],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:  # noqa: BLE001
            return "running"
        if r.returncode != 0:
            return "error"
        state, _, code = r.stdout.strip().partition(":")
        if state in ("created", "running"):
            return "running"
        if state == "exited":
            return "completed" if code.strip() == "0" else "error"
        return "error"

    def _collect(self, native_id: str) -> tuple[dict, list]:
        staging = self._staging.get(native_id)
        outputs: dict = {}
        logs: list[str] = []
        if staging:
            result_file = staging / "out" / "result.json"
            if result_file.exists():
                try:
                    data = json.loads(result_file.read_text())
                    outputs = data.get("outputs", {}) if data.get("ok") else {}
                    if not data.get("ok"):
                        logs.append(f"simulate error: {data.get('error')}")
                except (OSError, ValueError) as exc:
                    logs.append(f"could not read result.json: {exc}")
        try:
            r = subprocess.run([self._docker, "logs", native_id],
                               capture_output=True, text=True, timeout=10)
            if r.stdout:
                logs.append(r.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        self._cleanup(native_id)
        return outputs, logs

    def _cancel(self, native_id: str) -> None:
        try:
            subprocess.run([self._docker, "kill", native_id], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._cleanup(native_id)

    def _cleanup(self, native_id: str) -> None:
        try:
            subprocess.run([self._docker, "rm", "-f", native_id], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        staging = self._staging.pop(native_id, None)
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
