"""Shared in-job runner script generation for remote runtime adapters."""

from __future__ import annotations


def runner_script(
    *,
    workflow_path: str,
    inputs_path: str,
    result_path: str | None = None,
    stdout_marker: str | None = None,
) -> str:
    """Return Python source that execs workflow.py and emits normalized JSON."""
    sink = (
        f'json.dump(out, open({result_path!r}, "w"))'
        if result_path
        else f'print({stdout_marker!r} + json.dumps(out))'
    )
    return f"""
import json, types, traceback
src = open({workflow_path!r}).read()
inputs = json.load(open({inputs_path!r}))
mod = types.ModuleType("wf")
out = {{"ok": False, "error": "no result"}}
try:
    exec(compile(src, "workflow.py", "exec"), mod.__dict__)
    fn = getattr(mod, "simulate", None)
    if not callable(fn):
        out = {{"ok": False, "error": "simulate() not defined"}}
    else:
        r = fn(**inputs)
        if not isinstance(r, dict):
            out = {{"ok": False, "error": "simulate() must return dict"}}
        else:
            def _d(v):
                if hasattr(v, "tolist"): return v.tolist()
                if hasattr(v, "item"): return v.item()
                raise TypeError(type(v).__name__)
            out = {{"ok": True, "outputs": json.loads(json.dumps(r, default=_d))}}
except Exception as exc:
    out = {{"ok": False, "error": str(exc), "traceback": traceback.format_exc()}}
{sink}
"""
