"""Plan mode — dry-run side effects.

When the chat is started with ``--plan`` (or ``ChatState.permission_mode
== "plan"``), code paths that would write files or hit a network gate
themselves through ``check_plan_gate(label)`` and short-circuit:

  * Artifact registration creates no files.
  * Sim2L runs are skipped (the chat shows what *would* happen).
  * Catalog/results pushes are skipped.

The chat layer is the source of truth for the mode flag; non-chat code
asks via ``is_plan_mode()``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


_plan_active: bool = False


def is_plan_mode() -> bool:
    return _plan_active


def set_plan_mode(active: bool) -> bool:
    """Set / unset plan mode. Returns the previous value."""
    global _plan_active
    previous = _plan_active
    _plan_active = active
    return previous


@contextmanager
def plan_mode(active: bool = True) -> Iterator[None]:
    """Test helper. Temporarily set plan-mode for a code block."""
    previous = set_plan_mode(active)
    try:
        yield
    finally:
        set_plan_mode(previous)


class PlanModeBlocked(RuntimeError):
    """Raised by ``check_plan_gate`` when a side-effect would run.

    Code that *can* short-circuit catches this; code that genuinely
    needs the effect lets it propagate so the user sees a clear error.
    """

    def __init__(self, label: str, hint: str = ""):
        self.label = label
        self.hint = hint
        full = f"plan mode is active — refusing {label}"
        if hint:
            full += f"  ({hint})"
        super().__init__(full)


def check_plan_gate(label: str, *, hint: str = "") -> None:
    """Raise ``PlanModeBlocked`` if plan mode is active.

    Use at the top of any function whose side effects shouldn't run in
    plan mode. Example::

        def register(self, draft):
            check_plan_gate("artifact registration",
                            hint="files would be created under workspace/")
            ...
    """
    if _plan_active:
        raise PlanModeBlocked(label, hint=hint)


# ── Decorator for class methods ───────────────────────────────────────────

_NO_DEFAULT = object()


def gated(label: str, *, hint: str = "", return_on_block=_NO_DEFAULT):
    """Decorator: wrap a method so it raises ``PlanModeBlocked`` in plan
    mode. If ``return_on_block`` is supplied (including ``None``), the
    method returns that value instead of raising — handy for "best-effort"
    methods whose callers tolerate failure.
    """
    def decorator(fn):
        import functools

        if _is_coroutine(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                if _plan_active:
                    if return_on_block is not _NO_DEFAULT:
                        from arc.chat.events import emit
                        emit("info", f"[plan-mode] skipped {label}", label=label)
                        return return_on_block
                    raise PlanModeBlocked(label, hint=hint)
                return await fn(*args, **kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            if _plan_active:
                if return_on_block is not _NO_DEFAULT:
                    from arc.chat.events import emit
                    emit("info", f"[plan-mode] skipped {label}", label=label)
                    return return_on_block
                raise PlanModeBlocked(label, hint=hint)
            return fn(*args, **kwargs)
        return sync_wrapper
    return decorator


def _is_coroutine(fn) -> bool:
    import asyncio
    return asyncio.iscoroutinefunction(fn)
