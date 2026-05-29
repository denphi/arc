"""Runtime-agnostic helpers shared by every ``RuntimeAdapter``.

The input/output schema handling is identical regardless of *where* a
workflow runs (locally, in a container, on a cluster), so it lives here
rather than being copied into each adapter. ``LocalRuntimeAdapter`` uses
these today; the Docker/Slurm/K8s adapters (package-provided, see
design/todo.md items 5-6) reuse the same functions so reconciliation
stays consistent across backends.

These are pure functions — no I/O, no async — so they're trivially
testable and safe to call from inside ``asyncio.to_thread`` workers.
"""

from __future__ import annotations

from typing import Any


def reconcile_inputs(
    input_schema: dict[str, Any],
    inputs: dict[str, Any],
    *,
    default_value: float = 1.0,
) -> dict[str, Any]:
    """Overlay caller ``inputs`` onto the artifact's declared defaults.

    Starts from each declared field's ``default`` (``default_value`` when a
    field declares none), then overlays the caller's values — but only for
    keys the schema declares, when a schema exists. This prevents an
    "unexpected fields" error when an LLM-generated ``simulate`` uses
    different parameter names than the caller supplied, while still letting
    a schema-less artifact accept arbitrary inputs.
    """
    defaults = {
        key: (field.get("default", default_value) if isinstance(field, dict) else default_value)
        for key, field in (input_schema or {}).items()
    }
    return {
        **defaults,
        **{
            key: value
            for key, value in (inputs or {}).items()
            if not input_schema or key in input_schema
        },
    }


def filter_outputs(
    output_schema: dict[str, Any],
    raw_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project ``raw_outputs`` onto the schema-declared output keys.

    When the artifact declares an output schema, keep exactly those keys
    (filling missing ones with ``None`` so the shape is predictable).
    With no declared schema, pass the raw outputs through unchanged.
    """
    raw_outputs = raw_outputs or {}
    if not output_schema:
        return raw_outputs
    return {key: raw_outputs.get(key) for key in output_schema}


# ── Fire-and-poll adapter scaffolding ───────────────────────────────────
#
# LocalRuntimeAdapter / Sim2LRuntimeAdapter are *synchronous* — run()
# blocks until done. Container/cluster backends are *fire-and-poll*:
# submit a job, get an id, poll until terminal, then collect. This base
# class implements the RuntimeAdapterContract shape once (the submit→poll→
# collect orchestration, the timeout + cancel, the schema reconciliation,
# the ExecutionResult shaping) so Docker/Slurm/K8s only implement four
# backend hooks. It lives in core because it's shared infrastructure; the
# concrete backends live in packages.

import asyncio
import logging
import os
import uuid
from abc import abstractmethod

from arc.contracts.adapter import RuntimeAdapterContract
from arc.schemas.artifact import ArtifactRecord, ValidationResult
from arc.schemas.execution import ExecutionResult

_log = logging.getLogger(__name__)

# Terminal states the poller stops on.
_TERMINAL = {"completed", "error", "cancelled"}


class BaseSubmitPollAdapter(RuntimeAdapterContract):
    """Reusable submit → poll → collect adapter for remote backends.

    Subclasses implement:
      * ``is_runnable() -> bool`` — is the backend usable here (daemon up /
        CLI present / config loadable)?
      * ``_submit(artifact, inputs) -> native_id`` — start the job, return
        the backend's native id.
      * ``_poll_status(native_id) -> str`` — map backend state to one of
        ``pending|running|completed|error``.
      * ``_collect(native_id) -> (outputs: dict, logs: list[str])`` — gather
        results once terminal.
      * ``_cancel(native_id)`` — best-effort terminate on timeout.

    Subclasses set ``backend_name`` and may set ``poll_interval`` /
    ``default_timeout``. All backend hooks are sync (they shell out / use a
    client) and are run off the event loop via ``asyncio.to_thread``.
    """

    backend_name: str = "remote"
    poll_interval: float = 2.0
    default_timeout: float = float(os.getenv("ARC_REMOTE_EXEC_TIMEOUT", "600"))

    # ── backend hooks ──
    @abstractmethod
    def is_runnable(self) -> bool: ...

    @abstractmethod
    def _submit(self, artifact: ArtifactRecord, inputs: dict) -> str: ...

    @abstractmethod
    def _poll_status(self, native_id: str) -> str: ...

    @abstractmethod
    def _collect(self, native_id: str) -> tuple[dict, list]: ...

    def _cancel(self, native_id: str) -> None:  # pragma: no cover - override
        pass

    def _load_schema(self, artifact: ArtifactRecord) -> tuple[dict, dict]:
        from arc.sim2l_schema import load_sim2l_schema
        return load_sim2l_schema(artifact.path)

    # ── contract ──
    async def validate_artifact(self, artifact: ArtifactRecord) -> ValidationResult:
        from pathlib import Path
        wf = Path(artifact.path) / "workflow.py"
        if not wf.exists():
            return ValidationResult(valid=False, errors=[f"no workflow.py at {artifact.path}"])
        try:
            from arc.runtime.workflow_safety import check_workflow_source
            await asyncio.to_thread(check_workflow_source, wf.read_text())
        except Exception as exc:  # noqa: BLE001
            return ValidationResult(valid=False, errors=[f"safety check failed: {exc}"])
        return ValidationResult(valid=True)

    async def prepare_inputs(self, artifact: ArtifactRecord, parameters: dict) -> dict:
        input_schema, _ = await asyncio.to_thread(self._load_schema, artifact)
        return reconcile_inputs(input_schema, parameters)

    async def run(self, artifact: ArtifactRecord, inputs: dict) -> ExecutionResult:
        run_id = str(uuid.uuid4())
        if not await asyncio.to_thread(self.is_runnable):
            return ExecutionResult(
                run_id=run_id, status="error", outputs={},
                logs=[f"{self.backend_name} backend not runnable in this environment"],
                metrics={"execution_success": False, "reason": "backend_unavailable"},
            )
        _, output_schema = await asyncio.to_thread(self._load_schema, artifact)
        timeout = self.default_timeout
        try:
            native_id = await asyncio.to_thread(self._submit, artifact, inputs)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(run_id=run_id, status="error", outputs={},
                                   logs=[f"submit failed: {exc}"],
                                   metrics={"execution_success": False})

        # Poll until terminal or timeout. (asyncio.wait_for for py3.10 compat.)
        async def _poll_to_terminal() -> str:
            status = "pending"
            while status not in _TERMINAL:
                await asyncio.sleep(self.poll_interval)
                status = await asyncio.to_thread(self._poll_status, native_id)
            return status

        try:
            status = await asyncio.wait_for(_poll_to_terminal(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await asyncio.to_thread(self._cancel, native_id)
            return ExecutionResult(
                run_id=native_id or run_id, status="error", outputs={},
                logs=[f"{self.backend_name} job timed out after {timeout}s"],
                metrics={"execution_success": False, "reason": "timeout"},
            )

        if status != "completed":
            return ExecutionResult(run_id=native_id or run_id, status="error", outputs={},
                                   logs=[f"{self.backend_name} job ended: {status}"],
                                   metrics={"execution_success": False})

        raw_outputs, logs = await asyncio.to_thread(self._collect, native_id)
        return ExecutionResult(
            run_id=native_id or run_id, status="completed",
            outputs=filter_outputs(output_schema, raw_outputs or {}),
            logs=list(logs or []),
            metrics={"execution_success": True, "backend": self.backend_name, **inputs},
        )

    async def run_sweep(self, artifact: ArtifactRecord, parameter_space: dict) -> list:
        from itertools import product
        keys = list(parameter_space)
        if not keys:
            return [await self.run(artifact, {})]
        results = []
        for values in product(*(parameter_space[k] for k in keys)):
            results.append(await self.run(artifact, dict(zip(keys, values))))
        return results

    async def get_status(self, run_id: str) -> str:
        try:
            return await asyncio.to_thread(self._poll_status, run_id)
        except Exception:  # noqa: BLE001
            return "unknown"

    async def collect_outputs(self, run_id: str) -> dict:
        try:
            outputs, _ = await asyncio.to_thread(self._collect, run_id)
            return outputs or {}
        except Exception:  # noqa: BLE001
            return {}

    async def collect_logs(self, run_id: str) -> list:
        try:
            _, logs = await asyncio.to_thread(self._collect, run_id)
            return logs or []
        except Exception:  # noqa: BLE001
            return []

    async def collect_metrics(self, run_id: str) -> dict:
        return {}

    async def normalize_errors(self, error: Exception) -> dict:
        return {"type": error.__class__.__name__, "message": str(error)}
