"""Kubernetes runtime adapter — runs a workflow's ``simulate()`` as a Job.

Core supplies ``BaseSubmitPollAdapter``; this package implements the four
hooks via ``kubectl`` (no Python client dependency — same dep-free policy
as the docker/slurm adapters). A ConfigMap carries the workflow + inputs;
a Job (one pod, restartPolicy=Never) runs them and prints the result JSON
to stdout, which we read back from the pod logs.

Requires ``kubectl`` on PATH + a reachable cluster context. Without them,
``is_runnable`` is False and ``run`` returns a clear error.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from arc.runtime._adapter_common import BaseSubmitPollAdapter
from arc.runtime.remote_runner import runner_script
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)

# Printed to stdout by the pod; we parse the last JSON line from logs.
_RESULT_MARKER = "ARC_RESULT::"
_RUNNER = runner_script(
    workflow_path="/arc/workflow.py",
    inputs_path="/arc/inputs.json",
    stdout_marker=_RESULT_MARKER,
)


class KubernetesRuntimeAdapter(BaseSubmitPollAdapter):
    backend_name = "k8s"
    poll_interval = 5.0

    def __init__(self, db_path=None, session_id=None, **_):
        self.image = os.environ.get("ARC_K8S_IMAGE", "python:3.11-slim")
        self.namespace = os.environ.get("ARC_K8S_NAMESPACE", "default")
        self.kubectl = os.environ.get("ARC_KUBECTL_COMMAND", "kubectl")

    def _kubectl(self, *args, timeout=30, input_text=None):
        return subprocess.run(
            [self.kubectl, "-n", self.namespace, *args],
            capture_output=True, text=True, timeout=timeout, input=input_text,
        )

    def is_runnable(self) -> bool:
        if not shutil.which(self.kubectl):
            return False
        try:
            r = subprocess.run([self.kubectl, "version", "--request-timeout=5s"],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _submit(self, artifact: ArtifactRecord, inputs: dict) -> str:
        art_dir = Path(artifact.path).resolve()
        if not (art_dir / "workflow.py").exists():
            raise FileNotFoundError(f"artifact has no workflow.py at {art_dir}")
        name = f"arc-{uuid.uuid4().hex[:12]}"

        # ConfigMap carries workflow + inputs + runner.
        cm = self._kubectl(
            "create", "configmap", name,
            f"--from-literal=inputs.json={json.dumps(inputs or {})}",
            f"--from-file=workflow.py={art_dir / 'workflow.py'}",
        )
        # runner is added via a second literal so we don't need a temp file.
        self._kubectl("create", "configmap", f"{name}-runner",
                      f"--from-literal=runner.py={_RUNNER}")
        if cm.returncode != 0:
            raise RuntimeError(f"kubectl create configmap failed: {cm.stderr.strip()[:300]}")

        manifest = json.dumps(self._job_manifest(name))
        r = self._kubectl("apply", "-f", "-", input_text=manifest)
        if r.returncode != 0:
            raise RuntimeError(f"kubectl apply job failed: {r.stderr.strip()[:300]}")
        return name

    def _job_manifest(self, name: str) -> dict:
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name},
            "spec": {
                "backoffLimit": 0,
                "template": {"spec": {
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "arc",
                        "image": self.image,
                        "command": ["python", "/arc/runner.py"],
                        "volumeMounts": [
                            {"name": "art", "mountPath": "/arc/workflow.py", "subPath": "workflow.py"},
                            {"name": "art", "mountPath": "/arc/inputs.json", "subPath": "inputs.json"},
                            {"name": "runner", "mountPath": "/arc/runner.py", "subPath": "runner.py"},
                        ],
                    }],
                    "volumes": [
                        {"name": "art", "configMap": {"name": name}},
                        {"name": "runner", "configMap": {"name": f"{name}-runner"}},
                    ],
                }},
            },
        }

    def _poll_status(self, native_id: str) -> str:
        r = self._kubectl("get", "job", native_id, "-o",
                          "jsonpath={.status.succeeded}:{.status.failed}", timeout=15)
        if r.returncode != 0:
            return "pending"
        succeeded, _, failed = r.stdout.strip().partition(":")
        if succeeded and succeeded != "0":
            return "completed"
        if failed and failed != "0":
            return "error"
        return "running"

    def _collect(self, native_id: str) -> tuple[dict, list]:
        outputs, logs = {}, []
        r = self._kubectl("logs", f"job/{native_id}", timeout=30)
        raw = r.stdout or ""
        for line in raw.splitlines():
            if line.startswith(_RESULT_MARKER):
                try:
                    data = json.loads(line[len(_RESULT_MARKER):])
                    outputs = data.get("outputs", {}) if data.get("ok") else {}
                    if not data.get("ok"):
                        logs.append(f"simulate error: {data.get('error')}")
                except ValueError:
                    pass
        if raw:
            logs.append(raw[-2000:])
        self._cleanup(native_id)
        return outputs, logs

    def _cancel(self, native_id: str) -> None:
        self._cleanup(native_id)

    def _cleanup(self, native_id: str) -> None:
        for target in (f"job/{native_id}", f"configmap/{native_id}",
                       f"configmap/{native_id}-runner"):
            try:
                self._kubectl("delete", target, "--ignore-not-found", timeout=20)
            except Exception:  # noqa: BLE001
                pass
