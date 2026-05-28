"""``/results`` — list saved runs in this session."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import CYAN, DIM, GREEN, RED, c


async def run(state: ChatState, argv: list[str]) -> None:
    runs = state.workflow.results.list_all()
    if not runs:
        print(c("  No runs yet.", DIM))
        return
    for r in runs:
        status_col = GREEN if r.status == "completed" else RED
        print(f"  {c(r.run_id[:8], CYAN)}  {c(r.status, status_col)}  outputs={r.outputs}")


COMMANDS = [
    SlashCommand(
        name="results",
        summary="List saved runs in this session",
        handler=run,
    ),
]
