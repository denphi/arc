"""``continue`` / ``resume`` — resume the saved session goal for one iteration.

These are bareword commands (no leading slash) by historical accident.
They're registered with their bare forms as aliases inside the command
registry; the chat loop applies these *before* passing input to the
router so they don't get treated as free-text goals.
"""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import err


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import _run_with_continuation

    goal = state.primary_goal
    if not goal:
        err("No saved goal to continue. Type a goal first, or use /run <goal>.")
        return
    await _run_with_continuation(
        state.workflow, goal,
        max_iterations=1,
        start_artifact=state.current_artifact,
    )


# Note: aliases here are still slash-prefixed (``/continue``, ``/resume``)
# so the registry handles them. The chat loop also pre-translates the
# bareword forms ``continue`` and ``resume`` to ``/continue`` for backwards
# compat with existing tests.
COMMANDS = [
    SlashCommand(
        name="continue",
        summary="Resume the saved session goal for one iteration",
        handler=run,
        aliases=("resume",),
    ),
]
