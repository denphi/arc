"""``/exec <id> [k=v ...]`` — run an existing artifact with given params."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import err


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import run_artifact

    if not argv:
        err("Usage: /exec <artifact_id_or_name> [key=value ...]")
        return
    art_id = argv[0]
    params: dict = {}
    for kv in argv[1:]:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        try:
            params[k] = float(v)
        except ValueError:
            params[k] = v
    await run_artifact(state.workflow, art_id, params)


COMMANDS = [
    SlashCommand(
        name="exec",
        summary="Run an artifact directly with given parameters",
        handler=run,
        args_help="<artifact_id_or_name> [key=value ...]",
    ),
]
