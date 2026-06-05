"""ARC's standalone local executor (arc/runtime/executor.py) + the
LocalRuntimeAdapter wired to it.

Pins the "ARC has its own execution, no sim2l dependency" guarantee:
  * runs simulate() standalone (subprocess + in-process modes),
  * subprocess isolation — a crashing/hanging workflow doesn't take
    down the parent,
  * timeout terminates a runaway,
  * the local path imports nothing from sim2l.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc.runtime.executor import (
    execute_workflow,
    load_simulate,
    run_in_process,
    run_in_subprocess,
)


pytestmark = pytest.mark.chat


def _artifact_dir(workflow_src: str, schema_yaml: str | None = None) -> str:
    d = tempfile.mkdtemp(prefix="arc_exec_test_")
    Path(d, "workflow.py").write_text(workflow_src)
    Path(d, "sim2l.yaml").write_text(
        schema_yaml
        or "inputs:\n  x: {type: Number, default: 1.0}\n"
           "outputs:\n  z: {type: Number}\n"
    )
    return d


_SIMPLE = "def simulate(x=1.0, y=2.0):\n    return {'z': x + y}\n"


# ── load_simulate ──────────────────────────────────────────────────────


def test_load_simulate_returns_callable():
    d = _artifact_dir(_SIMPLE)
    func = load_simulate(d, "art-1")
    assert callable(func)
    assert func(x=3.0, y=4.0) == {"z": 7.0}


def test_load_simulate_missing_workflow_raises():
    d = tempfile.mkdtemp()
    with pytest.raises(FileNotFoundError):
        load_simulate(d, "art-1")


def test_load_simulate_no_simulate_func_raises():
    """No simulate() defined → rejected (the safety lint requires one,
    raising ValueError before the load even completes)."""
    d = _artifact_dir("def not_simulate():\n    return {}\n")
    with pytest.raises((AttributeError, ValueError)):
        load_simulate(d, "art-1")


def test_load_simulate_does_not_pollute_sys_modules():
    import sys
    d = _artifact_dir(_SIMPLE)
    before = set(sys.modules)
    load_simulate(d, "art-unique-xyz")
    after = set(sys.modules)
    # The throwaway module name must NOT be registered.
    assert not any("arc_local_artifact_art_unique_xyz" in m for m in (after - before))


# ── run_in_process ─────────────────────────────────────────────────────


def test_run_in_process_success():
    func = load_simulate(_artifact_dir(_SIMPLE), "a")
    r = run_in_process(func, {"x": 1.0, "y": 1.0})
    assert r == {"ok": True, "outputs": {"z": 2.0}}


def test_run_in_process_non_dict_return():
    d = _artifact_dir("def simulate(x=1.0):\n    return x * 2\n")
    func = load_simulate(d, "a")
    r = run_in_process(func, {"x": 3.0})
    assert r["ok"] is False
    assert "must return dict" in r["error"]


def test_run_in_process_exception_captured():
    d = _artifact_dir("def simulate(x=1.0):\n    raise ValueError('boom')\n")
    func = load_simulate(d, "a")
    r = run_in_process(func, {"x": 1.0})
    assert r["ok"] is False
    assert "boom" in r["error"]


# ── run_in_subprocess ──────────────────────────────────────────────────


def test_run_in_subprocess_success():
    r = run_in_subprocess(_SIMPLE, "<wf>", {"x": 2.0, "y": 5.0})
    assert r == {"ok": True, "outputs": {"z": 7.0}}


def test_run_in_subprocess_isolates_crash():
    """A workflow that hard-crashes the interpreter must not crash the
    parent — the subprocess absorbs it and we get an error result."""
    src = "def simulate(x=1.0):\n    raise RuntimeError('explode in child')\n"
    r = run_in_subprocess(src, "<wf>", {"x": 1.0})
    assert r["ok"] is False
    assert "explode in child" in r.get("error", "") or "error" in r


def test_run_in_subprocess_timeout_terminates_runaway():
    """A busy-loop is killed at the timeout and reported, not left hanging."""
    src = "def simulate(x=1.0):\n    n = 0\n    while True:\n        n += 1\n    return {'z': x}\n"
    r = run_in_subprocess(src, "<wf>", {"x": 1.0}, timeout=1.0)
    assert r["ok"] is False
    assert "timed out" in r["error"]


def test_run_in_subprocess_normalises_numpy_like_outputs():
    """Outputs round-trip through JSON; objects with .tolist()/.item()
    become plain Python types."""
    # Build a fake numpy-ish return without importing numpy: a class with
    # .item(). math is an allowed import.
    src = (
        "import math\n"
        "def simulate(x=1.0):\n"
        "    return {'z': math.sqrt(x), 'k': 2}\n"
    )
    r = run_in_subprocess(src, "<wf>", {"x": 9.0})
    assert r["ok"] is True
    assert r["outputs"]["z"] == 3.0
    assert r["outputs"]["k"] == 2


# ── execute_workflow (mode dispatch) ────────────────────────────────────


def test_execute_workflow_default_is_subprocess():
    d = _artifact_dir(_SIMPLE)
    r = execute_workflow(d, "a", {"x": 1.0, "y": 1.0})
    assert r == {"ok": True, "outputs": {"z": 2.0}}


def test_execute_workflow_inprocess_mode(monkeypatch):
    monkeypatch.setenv("ARC_LOCAL_EXECUTOR", "inprocess")
    d = _artifact_dir(_SIMPLE)
    r = execute_workflow(d, "a", {"x": 2.0, "y": 2.0})
    assert r == {"ok": True, "outputs": {"z": 4.0}}


def test_execute_workflow_missing_workflow():
    d = tempfile.mkdtemp()
    r = execute_workflow(d, "a", {})
    assert r["ok"] is False
    assert "no workflow.py" in r["error"]


def test_execute_workflow_rejects_unsafe_source():
    """The AST safety lint runs before execution (subprocess mode)."""
    d = _artifact_dir("import os\ndef simulate(x=1.0):\n    os.system('echo hi')\n    return {'z': x}\n")
    r = execute_workflow(d, "a", {"x": 1.0})
    assert r["ok"] is False
    assert "safety" in r["error"].lower() or "disallowed" in r["error"].lower()


# ── allowed_imports seam (domain runtime adapters, e.g. FEniCS) ──────────


_JSON_IMPORT = (
    "def simulate(x=1.0):\n"
    "    import json\n"
    "    return {'z': len(json.dumps([x]))}\n"
)


def test_execute_workflow_default_rejects_non_stdlib_import():
    # `json` is not in the strict stdlib subset → rejected by default.
    d = _artifact_dir(_JSON_IMPORT)
    r = execute_workflow(d, "a", {"x": 1.0})
    assert r["ok"] is False
    assert "disallowed import" in r["error"]


def test_execute_workflow_allowed_imports_widens_the_lint():
    # A caller (a domain adapter) can permit an extra import without editing
    # the default. The dunder/escape checks are unaffected (see below).
    d = _artifact_dir(_JSON_IMPORT)
    r = execute_workflow(d, "a", {"x": 1.0}, allowed_imports=frozenset({"json"}))
    assert r["ok"] is True
    assert "z" in r["outputs"]


def test_execute_workflow_allowed_imports_still_blocks_escapes():
    # Widening the IMPORT allow-list must NOT drop the sandbox-escape lint.
    evil = (
        "def simulate(x=1.0):\n"
        "    return {'z': ().__class__.__bases__[0].__subclasses__()}\n"
    )
    d = _artifact_dir(evil)
    r = execute_workflow(d, "a", {"x": 1.0}, allowed_imports=frozenset({"json", "os"}))
    assert r["ok"] is False
    assert "safety" in r["error"].lower() or "disallowed" in r["error"].lower()


def test_load_simulate_allowed_imports_widens_the_lint():
    d = _artifact_dir(_JSON_IMPORT)
    with pytest.raises(ValueError, match="disallowed import"):
        load_simulate(d, "a")  # strict default rejects `import json`
    func = load_simulate(d, "a", allowed_imports=frozenset({"json"}))
    assert callable(func)


def test_execute_workflow_reads_timeout_env_at_call_time(monkeypatch):
    import arc.runtime.executor as executor

    monkeypatch.setenv("ARC_LOCAL_EXEC_TIMEOUT", "7.5")

    assert executor._default_timeout_seconds() == 7.5


# ── LocalRuntimeAdapter end-to-end ──────────────────────────────────────


def _adapter():
    from arc.runtime.local import LocalRuntimeAdapter
    return LocalRuntimeAdapter()


def _record(path):
    return SimpleNamespace(artifact_id="art-e2e", path=path,
                           name="sim", version="0.1.0", metadata={})


_SCHEMA_XY = (
    "inputs:\n"
    "  x: {type: Number, default: 1.0}\n"
    "  y: {type: Number, default: 2.0}\n"
    "outputs:\n  z: {type: Number}\n"
)


def test_adapter_run_completed():
    d = _artifact_dir(_SIMPLE, _SCHEMA_XY)
    res = asyncio.run(_adapter().run(_record(d), {"x": 3.0, "y": 4.0}))
    assert res.status == "completed"
    assert res.outputs == {"z": 7.0}
    assert res.metrics["execution_success"] is True


def test_adapter_run_applies_schema_defaults():
    """Inputs not supplied fall back to the schema default."""
    d = _artifact_dir(_SIMPLE, _SCHEMA_XY)  # x=1.0, y=2.0 defaults
    res = asyncio.run(_adapter().run(_record(d), {}))
    assert res.status == "completed"
    assert res.outputs["z"] == 3.0


def test_adapter_run_drops_inputs_not_in_schema():
    """A schema declaring only x means caller's y is filtered out and the
    function default (2.0) applies — documents the reconciliation rule."""
    d = _artifact_dir(_SIMPLE)  # default schema: only x
    res = asyncio.run(_adapter().run(_record(d), {"x": 5.0, "y": 99.0}))
    # y dropped → function default 2.0 → z = 5.0 + 2.0
    assert res.outputs["z"] == 7.0


def test_adapter_run_missing_workflow_errors():
    d = tempfile.mkdtemp()
    res = asyncio.run(_adapter().run(_record(d), {}))
    assert res.status == "error"
    assert res.metrics["reason"] == "missing_workflow_py"


def test_adapter_run_workflow_error_is_status_error():
    d = _artifact_dir("def simulate(x=1.0):\n    raise ValueError('nope')\n")
    res = asyncio.run(_adapter().run(_record(d), {"x": 1.0}))
    assert res.status == "error"
    assert any("nope" in line for line in res.logs)


def test_adapter_validate_artifact_ok():
    d = _artifact_dir(_SIMPLE)
    vr = asyncio.run(_adapter().validate_artifact(_record(d)))
    assert vr.valid is True


def test_adapter_validate_artifact_bad_workflow():
    d = _artifact_dir("def not_simulate():\n    return {}\n")
    vr = asyncio.run(_adapter().validate_artifact(_record(d)))
    assert vr.valid is False
    assert any("import error" in e for e in vr.errors)


def test_adapter_run_sweep():
    d = _artifact_dir(_SIMPLE, _SCHEMA_XY)
    results = asyncio.run(_adapter().run_sweep(_record(d), {"x": [1.0, 2.0], "y": [10.0]}))
    assert len(results) == 2
    zs = sorted(r.outputs["z"] for r in results)
    assert zs == [11.0, 12.0]


# ── The "no sim2l import" guarantee ─────────────────────────────────────


def _import_lines(module_relpath: str) -> list[str]:
    """Return the actual import statements of a module (not prose)."""
    src = (
        Path(__file__).resolve().parent.parent / "arc" / module_relpath
    ).read_text()
    return [
        line.strip() for line in src.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def test_executor_module_has_no_sim2l_import():
    """Static check: no import line in executor.py touches sim2l.

    (The docstring mentions sim2l in prose — that's fine; we only care
    about import statements.)
    """
    for line in _import_lines("runtime/executor.py"):
        assert "sim2l" not in line, f"unexpected sim2l import: {line}"


def test_local_adapter_module_has_no_sim2l_adapter_import():
    """LocalRuntimeAdapter must not import from sim2l_adapter anymore.

    (It still imports ``arc.sim2l_schema`` — a pure schema parser with no
    sim2l package dependency — which is allowed; we forbid only the
    ``sim2l_adapter`` runtime module + the old workflow loader.)
    """
    for line in _import_lines("runtime/local.py"):
        assert "sim2l_adapter" not in line, f"unexpected import: {line}"
        assert "_import_workflow_func" not in line, f"unexpected import: {line}"
