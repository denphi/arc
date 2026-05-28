"""``/target`` — show, set, update, or clear the active target."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import err, ok, step


async def run(state: ChatState, argv: list[str]) -> None:
    """Parse the full raw input via ``_parse_target_command`` for parity
    with the legacy behaviour, then dispatch."""
    from arc.chat.parsers import parse_target_command as _parse_target_command
    from arc.chat.session_io import save_session as _save_session

    # Reconstruct the legacy raw form to reuse the parser unchanged.
    raw = "/target" + ("" if not argv else " " + " ".join(argv))
    action, target, detail = _parse_target_command(raw, state.target)

    if action == "show":
        step("Target", target or "none")
    elif action == "clear":
        state.target = {}
        _save_session(state.workflow, state.primary_goal)
        ok("Target cleared.")
    elif action == "set":
        state.target = target
        _save_session(state.workflow, state.primary_goal)
        ok(f"Target set to {target}")
    elif action == "error":
        err(detail or "Unknown /target usage. Try /target [key=value], /target update key=value, or /target clear.")
    else:
        err(f"Unknown /target action: {action}")


COMMANDS = [
    SlashCommand(
        name="target",
        summary="Show, set, update, or clear the target",
        handler=run,
        args_help="[key=value | update key=value | clear]",
    ),
]
