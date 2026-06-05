"""``/packages`` (list) and ``/package enable|disable <name>``."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, YELLOW, c


async def list_handler(state: ChatState, argv: list[str]) -> None:
    from arc.chat.loop import _selected_coder
    pkg_state = state.workflow._context.memory.get("packages", {})
    enabled = set(pkg_state.get("enabled", []))
    disabled = set(pkg_state.get("disabled", []))
    print(c("  Loaded packages:", BOLD))
    for name in state.workflow.registry.list_packages():
        marker = ""
        if name in enabled:
            marker = c(" enabled", GREEN)
        if name in disabled:
            marker = c(" disabled", YELLOW)
        print(f"    {c(name, CYAN)}{marker}")
    load_errors_fn = getattr(state.workflow.registry, "list_load_errors", None)
    load_errors = load_errors_fn() if load_errors_fn else []
    if load_errors:
        print(c("  Package load errors:", YELLOW))
        for item in load_errors:
            name = f" {item['name']}" if item.get("name") else ""
            print(f"    {item['package']} {item['kind']}{name}: {item['error']}")
    print(c(f"  Coder: {_selected_coder(state.workflow)}", DIM))
    print(c("  Use: /coder <backend> | /package enable <package>", DIM))


async def toggle_handler(state: ChatState, argv: list[str]) -> None:
    """``/package enable|disable <name>``.

    argv[0] is the action (enable / disable), argv[1] is the package name.
    """
    from arc.chat.loop import _set_session_package_state
    from arc.chat.session_io import save_session as _save_session
    from arc.chat.ui import err, ok
    if len(argv) != 2 or argv[0] not in {"enable", "disable"}:
        err("Usage: /package enable <name>  or  /package disable <name>")
        return
    action, name = argv[0], argv[1]
    if name not in state.workflow.registry.list_packages():
        err(f"Package '{name}' is not loaded. Add it to arc.toml and restart chat.")
        return
    _set_session_package_state(state.workflow, name, enabled=(action == "enable"))
    _save_session(state.workflow, state.primary_goal)
    ok(f"{'Enabled' if action == 'enable' else 'Disabled'} package {name} for this session.")


COMMANDS = [
    SlashCommand(
        name="packages",
        summary="List loaded packages and session package state",
        handler=list_handler,
    ),
    SlashCommand(
        name="package",
        summary="Enable or disable a package for this session",
        handler=toggle_handler,
        args_help="enable|disable <name>",
    ),
]
