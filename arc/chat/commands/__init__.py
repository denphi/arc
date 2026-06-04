"""Slash-command handlers.

Each module here declares one or more ``SlashCommand`` records via
``COMMANDS``. ``build_registry()`` aggregates them into a single
``CommandRegistry`` for the chat loop.

Handlers all share the signature::

    async def run(state: ChatState, argv: list[str]) -> None

They mutate ``state`` and print to stdout (Phase 2 will redirect through
the ChatEvent sink). They return ``None`` — control flow signals
(``break``/``continue``) belong to ``chat_loop``.
"""

from __future__ import annotations

from arc.chat.registry import CommandRegistry, SlashCommand


def build_registry() -> CommandRegistry:
    """Construct the canonical chat command registry."""
    from arc.chat.commands import (
        builtins as _builtins,
        artifacts as _artifacts,
        results as _results,
        sessions as _sessions,
        packages as _packages,
        coder as _coder,
        target as _target,
        services as _services,
        exec_cmd as _exec_cmd,
        sweep as _sweep,
        optimize as _optimize,
        iterate as _iterate,
        run_cmd as _run_cmd,
        cont as _cont,
        strategy as _strategy,
        recipe as _recipe,
        clusters as _clusters,
        skills as _skills,
        files as _files,
    )

    reg = CommandRegistry()
    for mod in (_builtins, _artifacts, _results, _sessions, _packages,
                _coder, _target, _services, _exec_cmd, _sweep,
                _optimize, _iterate, _run_cmd, _cont, _strategy, _recipe,
                _clusters, _skills, _files):
        for cmd in mod.COMMANDS:
            reg.register(cmd)
    return reg


__all__ = ["build_registry", "CommandRegistry", "SlashCommand"]
