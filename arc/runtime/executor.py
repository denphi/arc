"""ARC's own standalone workflow executor.

Runs an artifact's ``simulate(**inputs) -> dict`` with **no dependency
on sim2l**. This is what makes "ARC has its own execution, sim2l is
opt-in" true: the local runtime adapter uses this module, and sim2l is
only reached when ``ARC_RUNTIME_ADAPTER=sim2l`` selects the
sim2l-backed adapter instead.

Two execution modes:

  * **subprocess** (default) — runs ``simulate()`` in a spawned child
    process with a wall-clock timeout. A runaway or hanging workflow is
    terminated; a crash is captured rather than taking down the
    orchestrator. This mirrors what sim2l's ``IsolatedFunctionExecutor``
    provides, but is arc-owned and sim2l-free.
  * **in-process** — execs + calls ``simulate()`` in the current
    process. Faster, no isolation. Used for trusted source / tests, or
    when subprocess spawn isn't available. Selected via
    ``ARC_LOCAL_EXECUTOR=inprocess``.

THREAT MODEL — same caveat as sim2l's isolated executor: this provides
**process isolation, not code sandboxing**. The child runs the
submitted source with full privileges (it can read files, open
sockets, etc.). What it buys you: a fresh interpreter, a timeout, and a
crash that doesn't corrupt the parent. For true sandboxing use a
container / nsjail / gVisor at the adapter boundary.

The AST safety pre-check (``check_workflow_source``) still runs before
execution and rejects the most obvious dangerous patterns, but it is a
lint, not a sandbox.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import types
from pathlib import Path
from typing import Any

from arc.runtime.workflow_safety import (
    check_workflow_source,
    validate_workflow_import_timeout,
)

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = float(os.getenv("ARC_LOCAL_EXEC_TIMEOUT", "30"))


# ── Workflow loading (arc-owned, no sim2l import) ──────────────────────


def load_simulate(artifact_path: str, artifact_id: str):
    """Load ``simulate`` from an artifact's ``workflow.py``.

    Execs the source in a throwaway module namespace (never registered
    in ``sys.modules``, never mutates ``sys.path``) and returns the
    ``simulate`` callable. Runs the AST safety lint + import-timeout
    guard first.

    This is the arc-core replacement for what used to be imported from
    ``arc.runtime.sim2l_adapter._import_workflow_func`` — keeping the
    local execution path free of any sim2l-adjacent import.
    """
    artifact_dir = Path(artifact_path)
    wf_path = artifact_dir / "workflow.py"
    if not wf_path.exists():
        raise FileNotFoundError(f"workflow.py not found at {wf_path}")
    source = wf_path.read_text()
    check_workflow_source(source)

    module_name = f"arc_local_artifact_{artifact_id.replace('-', '_')}"
    validate_workflow_import_timeout(source, module_name, str(wf_path))

    module = types.ModuleType(module_name)
    module.__file__ = str(wf_path)
    exec(compile(source, str(wf_path), "exec"), module.__dict__)  # noqa: S102

    func = getattr(module, "simulate", None)
    if not callable(func):
        raise AttributeError(
            "workflow.py must define a function named 'simulate(**inputs) -> dict'"
        )
    return func


# ── Subprocess worker ──────────────────────────────────────────────────


def _exec_worker(source: str, filename: str, inputs: dict, queue) -> None:
    """Spawn-safe worker: exec the source, call simulate(**inputs), report.

    Runs in a child process. Puts a JSON-round-trippable result dict on
    the queue: ``{"ok": True, "outputs": {...}}`` or
    ``{"ok": False, "error": "...", "traceback": "..."}``. We round-trip
    outputs through JSON (with a numpy ``.tolist()`` / ``.item()``
    fallback) so the parent gets plain Python types regardless of what
    the workflow returns.
    """
    import traceback

    def _json_default(value):
        if hasattr(value, "tolist"):
            return value.tolist()
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(f"{type(value).__name__} is not JSON serializable")

    try:
        module = types.ModuleType("arc_exec_worker")
        module.__file__ = filename
        exec(compile(source, filename, "exec"), module.__dict__)  # noqa: S102
        func = getattr(module, "simulate", None)
        if not callable(func):
            queue.put({"ok": False, "error": "simulate() function not defined"})
            return
        outputs = func(**inputs)
        if not isinstance(outputs, dict):
            queue.put({
                "ok": False,
                "error": f"simulate() must return dict, got {type(outputs).__name__}",
            })
            return
        # Normalise through JSON so numpy/Pint/etc. become plain types
        # and the result survives the spawn pickle boundary cleanly.
        normalised = json.loads(json.dumps(outputs, default=_json_default))
        queue.put({"ok": True, "outputs": normalised})
    except Exception as exc:  # noqa: BLE001
        queue.put({
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })


def run_in_subprocess(
    source: str,
    filename: str,
    inputs: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``simulate(**inputs)`` in a spawned child with a timeout.

    Returns ``{"ok": True, "outputs": {...}}`` or
    ``{"ok": False, "error": "..."}``. A workflow exceeding ``timeout``
    is terminated and reported as a timeout. Never raises.
    """
    try:
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        proc = ctx.Process(target=_exec_worker, args=(source, filename, inputs, queue))
        proc.start()
        proc.join(timeout)
        if proc.is_alive():
            # Escalate: SIGTERM, then SIGKILL if the child ignores it, so a
            # workflow that traps SIGTERM can't hang the parent forever.
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join()
            return {"ok": False, "error": f"simulate() timed out after {timeout}s"}
        # The worker exited within the timeout. Read its result with a short
        # blocking get rather than empty()+get(): the child may still be
        # flushing the queue's feeder thread the instant join() returns, so a
        # bare empty() check can race and report "no result" for a run that
        # actually succeeded.
        try:
            return queue.get(timeout=1.0)
        except Exception:  # noqa: BLE001 — genuinely empty / closed queue
            pass
        if proc.exitcode:
            return {"ok": False, "error": f"worker exited with code {proc.exitcode}"}
        return {"ok": False, "error": "worker produced no result"}
    except Exception as exc:  # noqa: BLE001 — subprocess machinery failed
        logger.debug("subprocess execution failed, no result: %s", exc)
        return {"ok": False, "error": f"subprocess execution failed: {exc}"}


# ── In-process path ─────────────────────────────────────────────────────


def run_in_process(func, inputs: dict) -> dict[str, Any]:
    """Call an already-loaded ``simulate`` in the current process.

    No isolation, no timeout — used for trusted source / tests, or when
    ``ARC_LOCAL_EXECUTOR=inprocess``. Returns the same result shape as
    the subprocess path.
    """
    try:
        outputs = func(**inputs)
        if not isinstance(outputs, dict):
            return {
                "ok": False,
                "error": f"simulate() must return dict, got {type(outputs).__name__}",
            }
        return {"ok": True, "outputs": outputs}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── Public entry point ──────────────────────────────────────────────────


def execute_workflow(
    artifact_path: str,
    artifact_id: str,
    inputs: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute an artifact's ``simulate(**inputs)`` and return its outputs.

    The default subprocess mode gives isolation + a timeout. Set
    ``ARC_LOCAL_EXECUTOR=inprocess`` to run in-process (faster, no
    isolation). Returns ``{"ok": bool, "outputs"|"error": ...}``; never
    raises for workflow-level failures (only for a genuinely-missing
    workflow.py, which surfaces as a FileNotFoundError to the caller's
    validate step).
    """
    wf_path = Path(artifact_path) / "workflow.py"
    if not wf_path.exists():
        return {
            "ok": False,
            "error": f"artifact has no workflow.py at {artifact_path}",
        }

    mode = os.getenv("ARC_LOCAL_EXECUTOR", "subprocess").lower()
    if mode in {"inprocess", "in-process", "local"}:
        # In-process: load + call directly. The loader runs the safety
        # lint; a failure there propagates as an error result.
        try:
            func = load_simulate(artifact_path, artifact_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return run_in_process(func, inputs)

    # Subprocess (default): lint in-parent, then exec in the child.
    source = wf_path.read_text()
    try:
        check_workflow_source(source)
    except Exception as exc:  # noqa: BLE001 — safety lint rejected the source
        return {"ok": False, "error": f"workflow safety check failed: {exc}"}
    return run_in_subprocess(source, str(wf_path), inputs, timeout=timeout)
