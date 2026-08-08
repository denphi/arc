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
import logging
import multiprocessing as mp
import time
import types
from dataclasses import dataclass
from queue import Empty
from typing import Any

logger = logging.getLogger(__name__)


# Modules from the Python stdlib that we expect to be importable in every
# supported environment. Failure to import any of these is a config bug
# (a stripped-down Python build, broken venv, etc.) and we'd rather fail
# loudly than silently fall back to a workflow that can't `import math`.
# Review item #T20.
_REQUIRED_STDLIB_MODULES: frozenset[str] = frozenset({
    "math", "cmath", "itertools", "functools", "operator",
    "statistics", "random", "decimal", "fractions",
    "collections", "heapq", "bisect", "array",
    "typing", "abc", "dataclasses", "enum",
})


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
    # Gate only on the *source module* — the thing that grants capability.
    #
    #   import a, b.c          → check {a, b}
    #   from m import x, y, *  → check {m}, NOT {x, y, *}
    #
    # For ``ImportFrom`` the imported member names (``x``, ``y``, ``*``) are
    # bindings pulled *from* a module that has already been allow-listed;
    # checking them against a *module* allow-list is a category error. It
    # used to reject ``from dolfin import UnitSquareMesh`` (member name not
    # in the allow-list) and ``from fenics import *`` (``"*"`` not in the
    # allow-list), which made idiomatic FEniCS — and any `from x import y`
    # workflow — impossible even when ``x`` was explicitly permitted. The
    # source module is the only safety-relevant axis: ``from os import *``
    # stays blocked because ``os`` isn't allow-listed, not because of ``*``.
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        # ``from . import x`` (relative, node.module is None) has no module to
        # vet; treat the missing module as disallowed via an empty sentinel.
        names = [node.module or ""]

    for name in names:
        top = name.split(".", 1)[0]
        if top not in allowed:
            allowed_list = ", ".join(sorted(allowed))
            shown = name or "<relative import>"
            raise WorkflowSafetyError(
                f"workflow.py uses disallowed import '{shown}'. "
                f"Only these modules are permitted: {allowed_list}. "
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


def _constant_fold_string(node: ast.AST) -> str | None:
    """Best-effort constant fold to a Python string.

    Catches the basic concat tricks (``"a" + "b"``), repeat (``"x" * 5``),
    and joined strings made entirely of constants — enough to spot
    ``"__cla" + "ss__"`` style escapes in subscript indices without trying
    to be a full constant folder. Returns ``None`` when the value isn't a
    statically-known string.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                # f-string interpolation — can't fold safely; refuse.
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _constant_fold_string(node.left)
            right = _constant_fold_string(node.right)
            if left is not None and right is not None:
                return left + right
            return None
        if isinstance(node.op, ast.Mult):
            for str_node, num_node in ((node.left, node.right), (node.right, node.left)):
                base = _constant_fold_string(str_node)
                if base is None:
                    continue
                if isinstance(num_node, ast.Constant) and isinstance(num_node.value, int):
                    # Cap the repeat so an attacker can't trigger memory blowup
                    # inside the safety checker itself.
                    if num_node.value < 0 or num_node.value > 64:
                        return None
                    return base * num_node.value
            return None
    return None


def _check_subscript(node: ast.Subscript) -> None:
    """Reject ``obj['__class__']`` style escapes.

    Flags constant string subscripts AND simple constant-folded expressions
    that statically evaluate to a string (``"__cla" + "ss__"``, ``"_" * 2 +
    "class" + "_" * 2``, plain-constant f-strings). Variable indexing is
    fine — those values come from validated inputs at runtime.
    """
    folded = _constant_fold_string(node.slice)
    if folded is not None and _looks_unsafe_name(folded):
        raise WorkflowSafetyError(
            f"workflow.py uses disallowed subscript '[{folded!r}]' "
            f"(after constant folding). Dunder subscript access is "
            f"forbidden in sandboxed workflows."
        )


# Module-level dunder names that are safe to *read* as a bare global. They
# carry no capability on their own (unlike ``__class__`` / ``__globals__``,
# which walk the object graph to an escape). Allowing ``__name__`` lets coding
# agents keep the ``if __name__ == "__main__":`` self-test footer they reflexively
# emit in a generated ``workflow.py`` (review item F2). Still blocked as an
# *attribute* (``obj.__name__``) — only the bare-Name read is exempt.
_READABLE_DUNDER_NAMES: frozenset[str] = frozenset({"__name__"})


def _check_name(node: ast.Name) -> None:
    # Bare-name reference like `x = __class__`.
    if node.id in _READABLE_DUNDER_NAMES:
        return
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


def check_workflow_source_safe(
    source: str,
    *,
    allowed_imports: frozenset[str] = BUILDER_ALLOWED_IMPORTS,
) -> tuple[bool, str]:
    """Boolean wrapper around :func:`check_workflow_source`. Returns (ok, reason).

    The default allow-list is the builder's narrow one, because the builder's
    safe-eval probe genuinely can only offer ``math``/``cmath``/``itertools``.
    Callers gating *authoring* (the UI's artifact-create and validate routes)
    must pass ``STRICT_ALLOWED_IMPORTS`` instead — that's what the executor
    enforces at run time, and gating creation more tightly than execution
    rejected workflows that would have run perfectly well
    (``import statistics`` was refused as "Unsafe workflow").
    """
    try:
        check_workflow_source(source, allowed_imports=allowed_imports)
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
        except ImportError as exc:
            # Review item #T20: stdlib modules must be importable. Silently
            # swallowing the error left workflows that used `import math`
            # hitting a NameError deep inside `simulate()` with no signal
            # that the allow-list intended to provide it.
            if mod_name in _REQUIRED_STDLIB_MODULES:
                raise RuntimeError(
                    f"Required stdlib module {mod_name!r} is not importable: {exc}. "
                    "Check the Python build / virtualenv."
                ) from exc
            logger.warning(
                "Optional safe-globals module %r failed to import: %s. "
                "Workflows that reference it will get NameError.",
                mod_name, exc,
            )
    return safe_globals



# ── Subprocess-sandboxed validators ──────────────────────────────────────────
#
# These workers run untrusted workflow source in a child process via
# ``multiprocessing.get_context("spawn")``. Spawn requires the worker function
# and its arguments to be importable/picklable, which is why both workers live
# here in a normally-named module rather than in the hyphenated package
# directories that import them.

WORKFLOW_IMPORT_TIMEOUT_SECONDS = 2.0
SIMULATE_TIMEOUT_SECONDS = 2.0

# How long to wait for a child that has already reported a result to exit on
# its own before escalating to terminate/kill.
_EXIT_GRACE_SECONDS = 5.0


@dataclass
class _WorkerOutcome:
    """What a spawned validator worker reported (or failed to)."""

    result: Any = None
    exitcode: int | None = None
    timed_out: bool = False


def _run_spawn_worker(target, args: tuple, timeout: float) -> _WorkerOutcome:
    """Run ``target(*args, queue)`` in a spawned child and collect one result.

    Both validators previously drove their own ``Process``/``Queue`` pair, and
    each got a different detail wrong:

    * they called ``proc.join(timeout)`` *before* reading the queue. A child
      writing more than a pipe buffer (a workflow whose import dumps a long
      traceback to stderr) blocks in the queue feeder thread until someone
      reads, so ``join`` hit its timeout, the child was terminated, and a
      perfectly diagnosable failure was reported as "import timed out" — with
      the stderr that explained it discarded.
    * ``run_simulate_with_timeout`` then used ``queue.empty()`` before
      ``get()``. ``empty()`` is advisory on a ``multiprocessing.Queue``: the
      child may still be flushing the instant ``join`` returns, so a
      ``simulate()`` that succeeded was intermittently reported as producing no
      result. ``run_in_subprocess`` in ``arc.runtime.executor`` already carried
      a comment explaining exactly this; the fix never made it back here.

    Reading first fixes both. The child is drained (unblocking it), then joined,
    then escalated through terminate → kill only if it is still alive.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(*args, queue))
    proc.start()

    outcome = _WorkerOutcome()
    deadline = time.monotonic() + timeout
    while True:
        try:
            outcome.result = queue.get(timeout=0.05)
            break
        except Empty:
            if not proc.is_alive():
                # The child is gone. Give the feeder thread a final moment to
                # hand over anything already written, then stop waiting.
                try:
                    outcome.result = queue.get(timeout=0.2)
                except Empty:
                    outcome.result = None
                break
            if time.monotonic() >= deadline:
                outcome.timed_out = True
                break

    proc.join(0 if outcome.timed_out else _EXIT_GRACE_SECONDS)
    if proc.is_alive():
        # Escalate: SIGTERM, then SIGKILL if the child traps it.
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
    outcome.exitcode = proc.exitcode
    try:
        queue.close()
        queue.join_thread()
    except Exception:  # noqa: BLE001 — teardown must not mask the result
        pass
    return outcome


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
    outcome = _run_spawn_worker(
        workflow_import_worker,
        (source, module_name, filename),
        WORKFLOW_IMPORT_TIMEOUT_SECONDS,
    )
    if outcome.timed_out:
        raise TimeoutError("workflow.py import timed out")

    result = outcome.result
    if not isinstance(result, dict):
        # Worker died before reporting back (e.g. segfault, hard crash) —
        # surface the exit code so users have something to investigate.
        result = {
            "ok": False,
            "error": (
                f"workflow.py validator subprocess exited "
                f"with code {outcome.exitcode} before reporting status. "
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
    outcome = _run_spawn_worker(simulate_worker, (code, calls, allowed_modules), timeout)
    if outcome.timed_out:
        return {"ok": False, "reason": "simulate() timed out during validation"}
    if isinstance(outcome.result, dict):
        return outcome.result
    if outcome.exitcode:
        return {
            "ok": False,
            "reason": f"simulate() validation process exited {outcome.exitcode}",
        }
    return {"ok": False, "reason": "simulate() validation returned no result"}
