"""Built-in slash commands: /help, /quit, /clear.

Pinned by the Phase-0 tests in ``tests/test_chat_dispatch.py``.
"""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, c


# ── /help ─────────────────────────────────────────────────────────────────

async def help_handler(state: ChatState, argv: list[str]) -> None:
    """Render the help text from the live registry.

    The legacy hand-written f-string is gone — anything visible in
    ``/help`` is just whatever is currently registered.
    """
    from arc.chat.commands import build_registry
    from arc.chat.registry import format_help_lines

    # Rebuild a registry view every call so commands added at runtime
    # (Phase 3+ skills) appear immediately.
    reg = build_registry()
    print()
    print(c("ARC Commands", BOLD, CYAN))
    print(c("─" * 60, DIM))
    for line in format_help_lines(reg):
        print(line)
    print(c("─" * 60, DIM))
    print(c("Input keys:", BOLD), end=" ")
    print(c("Up/Down history, Ctrl+A start, Ctrl+E end, Ctrl+K kill line", DIM))
    print(c("─" * 60, DIM))

    ctx = state.workflow._context
    print(f"{c('Session:', BOLD)}        {c(state.session_id, CYAN)}")
    print(f"{c('Primary goal:', BOLD)}   "
          f"{c(state.primary_goal or 'none', DIM)}")
    target = ctx.memory.get("target") or {}
    print(f"{c('Target:', BOLD)}         {c(target or 'none', DIM)}")
    refs = state.refinements
    print(f"{c('Refinements:', BOLD)}    {c(f'{len(refs)} constraint(s)', DIM)}")
    artifact = state.current_artifact
    art_label = f"{artifact.name}  ({artifact.artifact_id[:8]}...)" if artifact else "none"
    print(f"{c('Current artifact:', BOLD)} {c(art_label, DIM)}")
    print(f"{c('Iteration:', BOLD)}      {c(ctx.iteration, DIM)}")
    print()


# ── /quit ─────────────────────────────────────────────────────────────────

class _QuitRequested(Exception):
    """Raised by /quit to ask chat_loop to break out cleanly.

    Defined as an exception so a deeply-nested handler can request exit
    without having to thread a return value back up. ``chat_loop``
    catches it.
    """


async def quit_handler(state: ChatState, argv: list[str]) -> None:
    print(c("Goodbye.", DIM))
    raise _QuitRequested()


# ── /clear ────────────────────────────────────────────────────────────────

async def clear_handler(state: ChatState, argv: list[str]) -> None:
    state.primary_goal = None
    state.clear_refinements()
    print(c("  Goal cleared. Next input will set a new primary goal.", DIM))


# ── Registry contribution ─────────────────────────────────────────────────

COMMANDS = [
    SlashCommand(
        name="help",
        summary="Show this help text",
        handler=help_handler,
        aliases=("?",),
    ),
    SlashCommand(
        name="quit",
        summary="Exit the chat",
        handler=quit_handler,
        aliases=("exit", "q"),
    ),
    SlashCommand(
        name="clear",
        summary="Forget current goal (keeps artifact and history)",
        handler=clear_handler,
    ),
]
