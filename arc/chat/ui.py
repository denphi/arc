"""ANSI colour codes and terminal-output helpers.

Public API:

  * ``c(text, *codes)`` — wrap text in ANSI codes
  * ``header / step / ok / warn / err / hr`` — emit a structured event
    if a sink is active; otherwise render straight to stdout.

The underlying renderers (``_render_*``) are exposed for the AnsiSink
in ``events.py``. New call sites should prefer ``emit()`` directly.
"""

from __future__ import annotations


RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BLUE   = "\033[34m"
GREY   = "\033[90m"


def c(text, *codes: str) -> str:
    """Wrap ``text`` with the given ANSI codes."""
    return "".join(codes) + str(text) + RESET


# ── Renderers ────────────────────────────────────────────────────────────
# These do the actual printing. ``AnsiSink`` calls them; the public
# wrappers call them when no sink is active.

def _render_header(text) -> None:
    print(f"\n{c('●', BOLD, CYAN)} {c(text, BOLD)}")


def _render_step(label, value) -> None:
    label_str = c(f"  {label:<14}", DIM)
    print(f"{label_str} {value}")


def _render_ok(text) -> None:
    print(f"  {c('✓', GREEN, BOLD)} {text}")


def _render_warn(text) -> None:
    print(f"  {c('!', YELLOW, BOLD)} {text}")


def _render_err(text) -> None:
    print(f"  {c('✗', RED, BOLD)} {text}")


def _render_hr() -> None:
    print(c("  " + "─" * 56, DIM))


# ── Public helpers — route through emit() when a sink is set ─────────────

def header(text) -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_header(text)
    else:
        emit("header", str(text))


def step(label, value) -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_step(label, value)
    else:
        emit("step", str(value), label=str(label))


def ok(text) -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_ok(text)
    else:
        emit("ok", str(text))


def warn(text) -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_warn(text)
    else:
        emit("warn", str(text))


def err(text) -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_err(text)
    else:
        emit("err", str(text))


def hr() -> None:
    from arc.chat.events import current_sink, emit
    if current_sink() is None:
        _render_hr()
    else:
        emit("hr")
