"""``/artifacts`` — list registered artifacts in this session."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import CYAN, DIM, c


async def run(state: ChatState, argv: list[str]) -> None:
    records = state.workflow.artifacts.list_all()
    if not records:
        print(c("  No artifacts registered yet.", DIM))
        return
    for r in records:
        print(f"  {c(r.artifact_id[:8], CYAN)}  {r.name}  {c(r.state, DIM)}")


COMMANDS = [
    SlashCommand(
        name="artifacts",
        summary="List registered artifacts in this session",
        handler=run,
    ),
]
