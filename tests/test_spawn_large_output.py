"""The spawned executor must return large results instead of timing out.

A child that put more than a pipe buffer's worth of data on its queue
can't exit until the parent reads, so joining before reading deadlocked.
"""

from __future__ import annotations

from arc.runtime.executor import run_in_subprocess
from arc.runtime.workflow_safety import run_simulate_with_timeout

_SRC = "def simulate(n=10):\n    return {'y': list(range(n))}\n"


def test_large_output_is_returned():
    result = run_in_subprocess(_SRC, "wf.py", {"n": 200_000}, timeout=20)
    assert result.get("ok") is True, result
    assert len(result["outputs"]["y"]) == 200_000


def test_timeout_still_enforced():
    src = "def simulate():\n    while True:\n        pass\n"
    result = run_in_subprocess(src, "wf.py", {}, timeout=1)
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_crash_without_result_is_reported():
    src = "import os\ndef simulate():\n    os._exit(3)\n"
    result = run_in_subprocess(src, "wf.py", {}, timeout=10)
    assert result == {"ok": False, "error": "worker exited with code 3"}


def test_builder_probe_handles_large_output():
    code = "def simulate(n=10):\n    return {'y': list(range(n))}\n"
    result = run_simulate_with_timeout(code, [{"n": 200_000}], timeout=20)
    assert result["ok"] is True, result
