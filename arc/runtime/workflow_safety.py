"""Static safety checks for ARC-generated workflow source.

Used by the runtime adapter (`Sim2LRuntimeAdapter`) and the builder agent
(`Sim2LBuilderAgent`) to reject obviously unsafe `simulate(**inputs)` source
before it is imported or executed.

This is intentionally a defence-in-depth layer, not a sandbox. The real
isolation boundary is `IsolatedFunctionExecutor` (subprocess) and the
`mp.get_context("spawn")` validators that run untrusted source in a child
process. The checks here only catch the easy escapes — anything that gets
past them must still be safe under the subprocess sandbox.

Two strictness levels:

- ``STRICT`` (default for runtime adapter): allow only the ARC stdlib subset.
- ``BUILDER``: same allow-list but with a smaller default for the builder
  agent's preflight, which validates LLM-generated code before deployment.

Both reject the same set of unsafe constructs.
"""

from __future__ import annotations

import ast
import cmath
import itertools
import math
import multiprocessing as mp
import types
from typing import Any


# ── Allow-lists ──────────────────────────────────────────────────────────────

# The runtime adapter accepts a richer stdlib subset because deployed artifacts
# may legitimately need more helpers (statistics, fractions, etc.).
STRICT_ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "math", "cmath", "itertools", "functools", "operator",
    "statistics", "random", "decimal", "fractions",
    "collections", "heapq", "bisect", "array",
    "typing", "abc", "dataclasses", "enum",
})

# The builder agent currently constrains generated code more tightly because
# its safe-eval globals only exposes the small subset.
BUILDER_ALLOWED_IMPORTS: frozenset[str] = frozenset({"math", "cmath", "itertools"})


# Names that are never legitimate in a simulate() workflow regardless of how
# they are reached (call, subscript, attribute lookup).
_BLOCKED_NAMES: frozenset[str] = frozenset({
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "type", "super", "object",
    "__class__", "__bases__", "__mro__", "__subclasses__", "__subclasshook__",
    "__dict__", "__globals__", "__builtins__", "__import__",
    "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
    "__init_subclass__", "__loader__", "__spec__", "__file__", "__module__",
    "__base__", "__qualname__", "__name__",
    "mro", "bases", "subclasses",
})


# ── DoS guardrails (covers review item #11) ──────────────────────────────────

MAX_SOURCE_BYTES = 64 * 1024            # 64 KiB of source is plenty for a workflow
MAX_AST_NODES = 5_000                   # any well-formed simulate() is well under this


class WorkflowSafetyError(ValueError):
    """Raised when workflow source fails static safety checks."""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _looks_unsafe_name(name: str) -> bool:
    """Reject obviously sandbox-escaping identifiers.

    - Any name with leading double-underscore (``__class__``, ``__mro__``…).
    - Any name explicitly listed in ``_BLOCKED_NAMES``.
    """
    if not isinstance(name, str):
        return False
    if name.startswith("__"):
        return True
    return name in _BLOCKED_NAMES


def _check_import(node: ast.AST, allowed: frozenset[str]) -> None:
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            names.append(node.module)
        names.extend(alias.name for alias in node.names)

    for name in names:
        top = name.split(".", 1)[0]
        if top not in allowed:
            allowed_list = ", ".join(sorted(allowed))
            raise WorkflowSafetyError(
                f"workflow.py uses disallowed import '{name}'. "
                f"Only standard-library modules are permitted: {allowed_list}. "
                f"Remove the import and rewrite the logic using plain math."
            )


def _check_call(node: ast.Call) -> None:
    """Reject calls that look like sandbox-escape primitives.

    Catches the obvious form ``__import__('os')`` and the slightly less obvious
    ``type(x)``, ``super()`` patterns without trying to be a full taint tracker.
    """
    func = node.func
    if isinstance(func, ast.Name) and _looks_unsafe_name(func.id):
        raise WorkflowSafetyError(
            f"workflow.py calls disallowed built-in '{func.id}()'. "
            f"Remove this call — it is not permitted in sandboxed workflows."
        )
    # `obj.__getattribute__('os')` etc.
    if isinstance(func, ast.Attribute) and _looks_unsafe_name(func.attr):
        raise WorkflowSafetyError(
            f"workflow.py calls disallowed attribute '{func.attr}'. "
            f"Remove this call — it is not permitted in sandboxed workflows."
        )


def _check_attribute(node: ast.Attribute) -> None:
    if _looks_unsafe_name(node.attr):
        raise WorkflowSafetyError(
            f"workflow.py accesses disallowed attribute '{node.attr}'. "
            f"Use normal attribute access or helper functions instead."
        )


def _check_subscript(node: ast.Subscript) -> None:
    """Reject ``obj['__class__']`` style escapes.

    Only flags constant string subscripts — variable indexing is fine because
    those values come from validated inputs at runtime.
    """
    # Python 3.9+: subscript value is the indexed expression directly.
    index = node.slice
    if isinstance(index, ast.Constant) and isinstance(index.value, str):
        if _looks_unsafe_name(index.value):
            raise WorkflowSafetyError(
                f"workflow.py uses disallowed subscript '[{index.value!r}]'. "
                f"Dunder subscript access is forbidden in sandboxed workflows."
            )


def _check_name(node: ast.Name) -> None:
    # Bare-name reference like `x = __class__`.
    if _looks_unsafe_name(node.id):
        raise WorkflowSafetyError(
            f"workflow.py references disallowed name '{node.id}'. "
            f"Remove it — it is not permitted in sandboxed workflows."
        )


# ── Public entry points ──────────────────────────────────────────────────────

def check_workflow_source(
    source: str,
    *,
    allowed_imports: frozenset[str] = STRICT_ALLOWED_IMPORTS,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_ast_nodes: int = MAX_AST_NODES,
) -> None:
    """Raise ``WorkflowSafetyError`` if ``source`` looks unsafe.

    Performs static checks only — does not execute the source. Callers should
    still run the workflow through the subprocess sandbox before trusting it.
    """
    if not isinstance(source, str):
        raise WorkflowSafetyError("workflow source must be a string")

    encoded_len = len(source.encode("utf-8"))
    if encoded_len > max_source_bytes:
        raise WorkflowSafetyError(
            f"workflow.py is too large ({encoded_len} bytes; "
            f"max {max_source_bytes}). Reject before parsing to avoid AST DoS."
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise WorkflowSafetyError(f"SyntaxError: {exc}") from exc

    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > max_ast_nodes:
            raise WorkflowSafetyError(
                f"workflow.py has too many AST nodes (>{max_ast_nodes}). "
                f"Simplify the implementation."
            )

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _check_import(node, allowed_imports)
        elif isinstance(node, ast.Call):
            _check_call(node)
        elif isinstance(node, ast.Attribute):
            _check_attribute(node)
        elif isinstance(node, ast.Subscript):
            _check_subscript(node)
        elif isinstance(node, ast.Name):
            _check_name(node)


def check_workflow_source_safe(source: str) -> tuple[bool, str]:
    """Boolean wrapper used by the builder agent. Returns (ok, reason)."""
    try:
        check_workflow_source(source, allowed_imports=BUILDER_ALLOWED_IMPORTS)
    except WorkflowSafetyError as exc:
        return False, str(exc)
    return True, "ok"


# ── Safe-eval globals for builder probe ──────────────────────────────────────
#
# Used by the builder agent's `_run_simulate_with_timeout` probe to execute
# generated simulate() code with a restricted builtins dict.
#
# Module objects ("math", "cmath", "itertools") are not picklable, so we
# *don't* serialize the globals across the spawn boundary. Instead the worker
# reconstructs them on its own end via `_build_safe_globals(allowed_modules)`.

_BUILDER_ALLOWED_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "pow": pow,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "ValueError": ValueError,
}


def _make_guarded_import(allowed_modules: frozenset[str]):
    """Return a `__builtins__["__import__"]` that allows only `allowed_modules`."""

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if root not in allowed_modules:
            raise ImportError(f"Import not allowed in ARC workflow: {name}")
        return __import__(name, globals, locals, fromlist, level)

    return _guarded_import


def build_safe_globals(allowed_modules: frozenset[str] = BUILDER_ALLOWED_IMPORTS) -> dict:
    """Construct a fresh safe-eval globals dict in the *current* process.

    Modules are imported here so the resulting dict contains live module
    objects, which is what the worker actually needs for code that does
    ``import math`` inside ``simulate()``. The set of allowed module names
    crosses the spawn boundary as a plain frozenset, which pickles fine.
    """
    import importlib

    safe_globals: dict = {
        "__builtins__": {
            **_BUILDER_ALLOWED_BUILTINS,
            "__import__": _make_guarded_import(allowed_modules),
        },
    }
    for mod_name in allowed_modules:
        try:
            safe_globals[mod_name] = importlib.import_module(mod_name)
        except ImportError:
            pass
    return safe_globals


# Convenience: callers in the parent process that only need read access (eg.
# tests) can use this directly. Workers should call `build_safe_globals` again
# inside the spawned child instead of receiving this object across the pickle.
BUILDER_SAFE_GLOBALS: dict = build_safe_globals(BUILDER_ALLOWED_IMPORTS)


# ── Subprocess-sandboxed validators ──────────────────────────────────────────
#
# These workers run untrusted workflow source in a child process via
# ``multiprocessing.get_context("spawn")``. Spawn requires the worker function
# and its arguments to be importable/picklable, which is why both workers live
# here in a normally-named module rather than in the hyphenated package
# directories that import them.

WORKFLOW_IMPORT_TIMEOUT_SECONDS = 2.0
SIMULATE_TIMEOUT_SECONDS = 2.0


def workflow_import_worker(source: str, module_name: str, filename: str, queue) -> None:
    """Spawn-safe worker: import workflow source as ``module_name``.

    Reports success/failure via ``queue`` along with anything written to
    stderr during the import (useful when the workflow raises and prints a
    traceback, or when a top-level call produces a warning). Cannot capture
    a segfault — for that the driver has to inspect ``proc.exitcode``.
    """
    import contextlib
    import io
    import traceback

    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            module = types.ModuleType(module_name)
            module.__file__ = filename
            exec(compile(source, filename, "exec"), module.__dict__)  # noqa: S102
            if not callable(getattr(module, "simulate", None)):
                queue.put({
                    "ok": False,
                    "error": "workflow.py must define simulate(**inputs) -> dict",
                    "stderr": captured.getvalue(),
                })
                return
        queue.put({"ok": True, "stderr": captured.getvalue()})
    except Exception as exc:
        queue.put({
            "ok": False,
            "error": str(exc),
            "stderr": captured.getvalue() + "\n" + traceback.format_exc(),
        })


def validate_workflow_import_timeout(source: str, module_name: str, filename: str) -> None:
    """Run ``workflow_import_worker`` in a spawned subprocess with a timeout.

    Raises ``TimeoutError`` if the import takes longer than
    ``WORKFLOW_IMPORT_TIMEOUT_SECONDS``, or ``ValueError`` on import failure.
    The error message includes captured stderr / a traceback when present.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=workflow_import_worker, args=(source, module_name, filename, queue))
    proc.start()
    proc.join(WORKFLOW_IMPORT_TIMEOUT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise TimeoutError("workflow.py import timed out")

    if not queue.empty():
        result = queue.get()
    else:
        # Worker died before reporting back (e.g. segfault, hard crash) —
        # surface the exit code so users have something to investigate.
        result = {
            "ok": False,
            "error": (
                f"workflow.py validator subprocess exited "
                f"with code {proc.exitcode} before reporting status. "
                "This usually indicates a crash (segfault, OOM, or a "
                "fatal Python interpreter error)."
            ),
        }

    if not result.get("ok"):
        msg = result.get("error", "workflow.py import failed")
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            msg = f"{msg}\nstderr: {stderr}"
        raise ValueError(msg)


def simulate_worker(
    code: str,
    calls: list[dict],
    allowed_modules: frozenset[str],
    queue,
) -> None:
    """Spawn-safe worker: execute simulate() against a series of input dicts.

    Reports the first non-empty dict result, or the last failure reason. Used
    by the builder agent to probe a generated workflow before deployment.

    Reconstructs the safe-eval globals locally — module objects can't cross
    the spawn pickle boundary.
    """
    try:
        safe_globals = build_safe_globals(allowed_modules)
        ns: dict = {}
        exec(compile(code, "<workflow>", "exec"), safe_globals, ns)  # noqa: S102
        func = ns.get("simulate")
        if not callable(func):
            queue.put({"ok": False, "reason": "simulate() function not defined"})
            return

        last_exc: Any = None
        for kwargs in calls:
            try:
                result = func(**kwargs)
                if isinstance(result, dict) and result:
                    queue.put({"ok": True, "result": result})
                    return
                if isinstance(result, dict) and not result:
                    last_exc = "returned empty dict {}"
            except Exception as exc:
                last_exc = str(exc)
        queue.put({"ok": False, "reason": last_exc or "returned empty dict {}"})
    except Exception as exc:
        queue.put({"ok": False, "reason": f"exec error: {exc}"})


def run_simulate_with_timeout(
    code: str,
    calls: list[dict],
    allowed_modules: frozenset[str] = BUILDER_ALLOWED_IMPORTS,
    timeout: float = SIMULATE_TIMEOUT_SECONDS,
) -> dict:
    """Run ``simulate_worker`` in a spawned subprocess with a timeout.

    Returns ``{"ok": True, "result": ...}`` on success or
    ``{"ok": False, "reason": ...}`` on any failure mode.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=simulate_worker, args=(code, calls, allowed_modules, queue)
    )
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return {"ok": False, "reason": "simulate() timed out during validation"}
    if not queue.empty():
        return queue.get()
    if proc.exitcode:
        return {"ok": False, "reason": f"simulate() validation process exited {proc.exitcode}"}
    return {"ok": False, "reason": "simulate() validation returned no result"}
