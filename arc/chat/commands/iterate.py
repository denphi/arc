"""``/iterate [N]`` — continue iterating on the current goal."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import err


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import _run_with_continuation

    n = int(argv[0]) if argv and argv[0].isdigit() else 3
    goal = state.primary_goal
    if not goal:
        err("No goal set. Type a goal first, or use /run <goal>.")
        return

    current_artifact = state.current_artifact
    if current_artifact is None and state.workflow.provider is None:
        err("No artifact and no LLM provider — set a goal first with a provider configured.")
        return

    await _run_with_continuation(
        state.workflow, goal,
        max_iterations=n,
        start_artifact=current_artifact,
    )


COMMANDS = [
    SlashCommand(
        name="iterate",
        summary="Continue iterating on the current goal (default 3 steps)",
        handler=run,
        args_help="[N]",
    ),
]
