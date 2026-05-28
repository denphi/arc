"""``/sessions`` — list past sessions for this user."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import CYAN, DIM, GREEN, c
from arc.session import list_sessions


async def run(state: ChatState, argv: list[str]) -> None:
    sessions = list_sessions()
    if not sessions:
        print(c("  No sessions yet.", DIM))
        return
    for s in sessions:
        marker = c(" ◀ current", GREEN) if s["session_id"] == state.session_id else ""
        goal_preview = (s["goal"] or "")[:50]
        print(
            f"  {c(s['session_id'], CYAN)}  iter={s['iteration']}  "
            f"{c(goal_preview, DIM)}{marker}"
        )
    print()
    print(c("  Resume with:     arc chat --session <id>", DIM))
    print(c("  Delete one:      arc chat --delete-session <id>", DIM))
    print(c("  Delete all:      arc chat --delete-all-sessions", DIM))


COMMANDS = [
    SlashCommand(
        name="sessions",
        summary="List all past sessions",
        handler=run,
    ),
]
