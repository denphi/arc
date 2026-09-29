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
        artifacts as _artifacts,
    )
    from arc.chat.commands import (
        build_context as _build_context,
    )
    from arc.chat.commands import (
        builtins as _builtins,
    )
    from arc.chat.commands import (
        clusters as _clusters,
    )
    from arc.chat.commands import (
        coder as _coder,
    )
    from arc.chat.commands import (
        cont as _cont,
    )
    from arc.chat.commands import (
        exec_cmd as _exec_cmd,
    )
    from arc.chat.commands import (
        files as _files,
    )
    from arc.chat.commands import (
        iterate as _iterate,
    )
    from arc.chat.commands import (
        optimize as _optimize,
    )
    from arc.chat.commands import (
        packages as _packages,
    )
    from arc.chat.commands import (
        recipe as _recipe,
    )
    from arc.chat.commands import (
        results as _results,
    )
    from arc.chat.commands import (
        run_cmd as _run_cmd,
    )
    from arc.chat.commands import (
        services as _services,
    )
    from arc.chat.commands import (
        sessions as _sessions,
    )
    from arc.chat.commands import (
        skills as _skills,
    )
    from arc.chat.commands import (
        strategy as _strategy,
    )
    from arc.chat.commands import (
        sweep as _sweep,
    )
    from arc.chat.commands import (
        target as _target,
    )

    reg = CommandRegistry()
    for mod in (_builtins, _artifacts, _results, _sessions, _packages,
                _coder, _target, _services, _exec_cmd, _sweep,
                _optimize, _iterate, _run_cmd, _cont, _strategy, _recipe,
                _clusters, _skills, _files, _build_context):
        for cmd in mod.COMMANDS:
            reg.register(cmd)
    return reg


__all__ = ["build_registry", "CommandRegistry", "SlashCommand"]
