"""``/run [goal]`` — start a fresh research goal."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import err


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import _run_with_continuation

    goal_text = " ".join(argv).strip() or state.primary_goal
    if not goal_text:
        err("No goal. Provide one after /run or type a goal first.")
        return

    state.reset_for_new_goal(goal_text)
    await _run_with_continuation(
        state.workflow, goal_text,
        max_iterations=state.max_iterations,
    )


COMMANDS = [
    SlashCommand(
        name="run",
        summary="Start a fresh research goal (resets the session)",
        handler=run,
        args_help="[goal]",
    ),
]
