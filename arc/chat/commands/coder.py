"""``/coder [backend]`` — show or set the active coder backend."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, DIM, c, err, ok, step


_ALIASES = {
    "builtin": "builder", "built-in": "builder", "sim2l": "builder",
    "codex": "arc-codex:coder",
    "claude": "arc-claude-code:coder", "claude-code": "arc-claude-code:coder",
}


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import (
        _selected_coder, _set_selected_coder,
        _available_coding_backends, _set_session_package_state, _save_session,
    )
    if not argv:
        step("Coder", _selected_coder(state.workflow))
        available = _available_coding_backends(state.workflow)
        step("Available", available)
        seen: dict[str, str] = {}
        for alias, resolved in _ALIASES.items():
            if resolved in available and (resolved not in seen or len(alias) < len(seen[resolved])):
                seen[resolved] = alias
        if seen:
            print(c("  Examples:", BOLD))
            for resolved, alias in seen.items():
                print(f"    /coder {alias:<16} {c(f'→ {resolved}', DIM)}")
        return

    backend = argv[0]
    try:
        selected = _set_selected_coder(state.workflow, backend)
        package_name = selected.split(":", 1)[0] if ":" in selected else None
        if package_name:
            _set_session_package_state(state.workflow, package_name, enabled=True)
        _save_session(state.workflow, state.primary_goal)
        ok(f"Coder backend set to {selected}")
    except ValueError as exc:
        err(str(exc))


COMMANDS = [
    SlashCommand(
        name="coder",
        summary="Show or set the artifact coder (builder, codex, claude-code, ...)",
        handler=run,
        args_help="[backend]",
    ),
]
