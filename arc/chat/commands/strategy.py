"""``/strategy`` — inspect or override which implementation backs a research role.

Usage:
    /strategy                    list every known role + current pick
    /strategy <role>             list available strategies for that role
    /strategy <role> <impl>      switch the active strategy for this session
    /strategy <role> reset       drop the session override
"""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, YELLOW, c, err, header, ok, step, warn


def _current_override(state: ChatState, role: str) -> str | None:
    overrides = state.memory.get("strategy_overrides") or {}
    return overrides.get(role)


def _set_override(state: ChatState, role: str, impl: str | None) -> None:
    overrides = state.memory.get("strategy_overrides") or {}
    if impl is None:
        overrides.pop(role, None)
    else:
        overrides[role] = impl
    if overrides:
        state.memory["strategy_overrides"] = overrides
    else:
        state.memory.pop("strategy_overrides", None)


def _resolved_name(role: str, overrides: dict[str, str] | None) -> str:
    from arc.core.config import load_arc_toml
    from arc.core.strategies import resolve_strategy_name
    try:
        _path, config = load_arc_toml()
    except Exception:
        config = {}
    return resolve_strategy_name(role, overrides=overrides, config=config)


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.core.strategies import known_roles, list_strategies, default_strategy

    overrides = state.memory.get("strategy_overrides") or {}

    if not argv:
        header("Research-loop strategies")
        for role in known_roles():
            picked = _resolved_name(role, overrides)
            default = default_strategy(role) or ""
            tag = ""
            if role in overrides:
                tag = c("  [session override]", YELLOW)
            elif picked != default:
                tag = c("  [arc.toml or env]", DIM)
            step(role, c(picked, CYAN) + tag)
        print()
        print(c("  /strategy <role> to see options, /strategy <role> <impl> to switch.", DIM))
        return

    role = argv[0].lower()
    if role not in known_roles():
        err(f"Unknown role: {role}. Known: {', '.join(known_roles())}")
        return

    if len(argv) == 1:
        header(f"Strategies for {role}")
        picked = _resolved_name(role, overrides)
        for spec in list_strategies(role):
            marker = c(" ← active", GREEN, BOLD) if spec.name == picked else ""
            label = c(spec.name, CYAN, BOLD) + marker
            print(f"  {label}")
            if spec.description:
                print(f"    {c(spec.description, DIM)}")
        if not list_strategies(role):
            warn(f"No strategies registered for {role}.")
        return

    impl = argv[1].lower()
    if impl in ("reset", "clear"):
        _set_override(state, role, None)
        ok(f"Cleared session override for {c(role, CYAN)}. Now using "
           f"{c(_resolved_name(role, state.memory.get('strategy_overrides')), CYAN)}.")
        state.persist()
        return

    available = {s.name for s in list_strategies(role)}
    if impl not in available:
        err(f"Unknown strategy {impl!r} for {role}. "
            f"Available: {', '.join(sorted(available))}")
        return

    _set_override(state, role, impl)
    ok(f"{c(role, CYAN)} → {c(impl, CYAN, BOLD)} for this session.")
    state.persist()


COMMANDS = [
    SlashCommand(
        name="strategy",
        summary="Inspect or override which implementation backs a research role.",
        handler=run,
        args_help="[<role> [<impl>|reset]]",
    ),
]
