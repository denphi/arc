"""Slurm runtime adapter — runs a workflow's ``simulate()`` as a batch job.

Core supplies ``BaseSubmitPollAdapter`` (submit→poll→collect, timeout,
schema handling); this package implements the four Slurm hooks via the
Slurm CLI (`sbatch` / `sacct` / `scancel`). Stages the artifact + an input
JSON to a scratch dir, renders an sbatch script that runs the workflow and
writes a result JSON, then polls `sacct` for the job state.

Requires the Slurm client commands on PATH (a submission host). When they
aren't present, ``is_runnable`` is False and ``run`` returns a clear
error rather than hanging.
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
from arc.runtime.workflow_safety import check_workflow_source
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)

# Job body: run the staged workflow, call simulate(**inputs), write result.
_RUNNER = """
import os
job = os.environ["ARC_JOB_DIR"]
""" + runner_script(
    workflow_path="workflow.py",
    inputs_path="inputs.json",
    result_path="result.json",
).replace("open('workflow.py')", 'open(os.path.join(job, "workflow.py"))') \
 .replace("open('inputs.json')", 'open(os.path.join(job, "inputs.json"))') \
 .replace("open('result.json', \"w\")", 'open(os.path.join(job, "result.json"), "w")')

_SLURM_STATE_MAP = {
    "PENDING": "pending", "CONFIGURING": "pending", "RUNNING": "running",
    "COMPLETING": "running", "COMPLETED": "completed",
    "FAILED": "error", "CANCELLED": "error", "TIMEOUT": "error",
    "NODE_FAIL": "error", "OUT_OF_MEMORY": "error", "BOOT_FAIL": "error",
}


class SlurmRuntimeAdapter(BaseSubmitPollAdapter):
    backend_name = "slurm"
    poll_interval = 5.0

    def __init__(self, db_path=None, session_id=None, **_):
        self.partition = os.environ.get("ARC_SLURM_PARTITION", "")
        self.account = os.environ.get("ARC_SLURM_ACCOUNT", "")
        self.time_limit = os.environ.get("ARC_SLURM_TIME", "00:30:00")
        self.scratch = Path(os.environ.get("ARC_SLURM_SCRATCH", str(Path.home() / ".arc" / "slurm")))
        self.python = os.environ.get("ARC_SLURM_PYTHON", "python")
        self._jobdirs: dict[str, Path] = {}

    def is_runnable(self) -> bool:
        return bool(shutil.which("sbatch") and shutil.which("sacct"))

    def _submit(self, artifact: ArtifactRecord, inputs: dict) -> str:
        art_dir = Path(artifact.path).resolve()
        workflow_path = art_dir / "workflow.py"
        if not workflow_path.exists():
            raise FileNotFoundError(f"artifact has no workflow.py at {art_dir}")
        source = workflow_path.read_text(encoding="utf-8")
        check_workflow_source(source)
        self.scratch.mkdir(parents=True, exist_ok=True)
        job_dir = Path(self.scratch) / f"arc-{uuid.uuid4().hex[:12]}"
        job_dir.mkdir(parents=True)
        shutil.copy2(workflow_path, job_dir / "workflow.py")
        (job_dir / "inputs.json").write_text(json.dumps(inputs or {}))
        (job_dir / "runner.py").write_text(_RUNNER)

        script = job_dir / "job.sbatch"
        lines = ["#!/bin/bash", f"#SBATCH --job-name=arc-{job_dir.name}",
                 f"#SBATCH --time={self.time_limit}",
                 f"#SBATCH --output={job_dir / 'slurm.out'}"]
        if self.partition:
            lines.append(f"#SBATCH --partition={self.partition}")
        if self.account:
            lines.append(f"#SBATCH --account={self.account}")
        lines += [f"export ARC_JOB_DIR={job_dir}", f"{self.python} {job_dir / 'runner.py'}"]
        script.write_text("\n".join(lines) + "\n")

        proc = subprocess.run(["sbatch", "--parsable", str(script)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"sbatch failed: {proc.stderr.strip()[:300]}")
        job_id = proc.stdout.strip().split(";")[0]   # --parsable → "<jobid>[;cluster]"
        self._jobdirs[job_id] = job_dir
        return job_id

    def _poll_status(self, native_id: str) -> str:
        try:
            r = subprocess.run(
                ["sacct", "-j", native_id, "--format=State", "--noheader", "--parsable2"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:  # noqa: BLE001
            return "running"
        if r.returncode != 0 or not r.stdout.strip():
            return "pending"   # job not yet in the accounting db
        # First line is the job's primary state (steps follow).
        state = r.stdout.strip().splitlines()[0].split()[0].strip().rstrip("+")
        return _SLURM_STATE_MAP.get(state, "running")

    def _collect(self, native_id: str) -> tuple[dict, list]:
        job_dir = self._jobdirs.get(native_id)
        outputs, logs = {}, []
        if job_dir:
            rf = job_dir / "result.json"
            if rf.exists():
                try:
                    data = json.loads(rf.read_text())
                    outputs = data.get("outputs", {}) if data.get("ok") else {}
                    if not data.get("ok"):
                        logs.append(f"simulate error: {data.get('error')}")
                except (OSError, ValueError) as exc:
                    logs.append(f"could not read result.json: {exc}")
            slurm_out = job_dir / "slurm.out"
            if slurm_out.exists():
                try:
                    logs.append(slurm_out.read_text()[-2000:])
                except OSError:
                    pass
        return outputs, logs

    def _cancel(self, native_id: str) -> None:
        try:
            subprocess.run(["scancel", native_id], capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass
