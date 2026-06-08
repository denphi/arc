"""SlashCommand + CommandRegistry.

The chat REPL currently has 18 inline ``if raw.lower()...`` branches.
This module replaces them with a registry where each command is a
self-contained record.

Phase 1 step 2: define the types and the lookup logic.
Phase 1 step N: migrate each command body into a handler.

Design constraints (from review):
  * Argv parsing uses ``shlex.split`` so quoted args work.
  * Aliases dispatch to a single canonical name.
  * Unknown commands and bare slashes (``/``) return ``None`` so the
    caller can render a helpful error.
  * The registry never imports a handler module eagerly — handlers
    can be referenced as ``"module:func"`` strings and resolved lazily.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Awaitable, Callable, Iterable, Optional, Union


# A handler is either a callable or a "module:attr" string for lazy import.
# NB: this is a *module-level* alias evaluated at import time, so PEP 604
# `X | Y` is NOT deferred by `from __future__ import annotations` — it must use
# typing.Union to stay importable on Python 3.9 (the only interpreter the
# legacy-FEniCS conda stack ships for).
HandlerRef = Union[Callable[..., Awaitable[None]], str]


@dataclass(frozen=True)
class SlashCommand:
    """One slash command record.

    Attributes
    ----------
    name
        Canonical form without the leading slash, e.g. ``"services"``.
    aliases
        Additional names that map to this command. Each alias is also
        without a leading slash.
    summary
        One-line description for ``/help``.
    args_help
        Concise usage hint, e.g. ``"[start|stop] [name]"``. Used by
        ``/help`` and by the unknown-args error path.
    handler
        Either an ``async def fn(state, argv)`` callable or a
        ``"arc.chat.commands.foo:run"`` string resolved on first call.
    requires_provider
        If True, the dispatcher refuses to invoke the handler when
        ``state.workflow.provider`` is ``None`` (stub mode).
    requires_artifact
        If True, refuses when ``state.current_artifact`` is ``None``.
    show_in_help
        Set False for command aliases that shouldn't clutter ``/help``.
    """

    name: str
    summary: str
    handler: HandlerRef
    aliases: tuple[str, ...] = ()
    args_help: str = ""
    requires_provider: bool = False
    requires_artifact: bool = False
    show_in_help: bool = True

    def resolve_handler(self) -> Callable[..., Awaitable[None]]:
        """Return the actual callable, importing the module on first call."""
        if callable(self.handler):
            return self.handler
        if not isinstance(self.handler, str) or ":" not in self.handler:
            raise ValueError(
                f"SlashCommand {self.name!r} has invalid handler ref {self.handler!r}; "
                f"expected callable or 'module:attr' string"
            )
        mod_path, attr = self.handler.split(":", 1)
        try:
            mod = import_module(mod_path)
            fn = getattr(mod, attr)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                f"Could not resolve handler {self.handler!r} for /{self.name}: {exc}"
            ) from exc
        if not callable(fn):
            raise TypeError(
                f"Handler {self.handler!r} for /{self.name} is not callable"
            )
        return fn


@dataclass
class CommandLookup:
    """Result of ``CommandRegistry.lookup``."""

    command: Optional[SlashCommand]
    argv: list[str] = field(default_factory=list)
    error: Optional[str] = None     # human-readable reason on miss


class CommandRegistry:
    """Maps slash strings to ``SlashCommand`` records.

    Names and aliases are stored lowercased. Lookup uses ``shlex.split``
    so quoted arguments work correctly (current ``raw.split()`` corrupts
    inputs with spaces).
    """

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(self, cmd: SlashCommand) -> None:
        key = cmd.name.lower()
        if key in self._commands:
            raise ValueError(f"Duplicate command name: {key!r}")
        if key in self._aliases:
            raise ValueError(
                f"Command name {key!r} clashes with existing alias of "
                f"/{self._aliases[key]}"
            )
        self._commands[key] = cmd
        for alias in cmd.aliases:
            alias_key = alias.lower()
            if alias_key in self._commands:
                raise ValueError(
                    f"Alias {alias_key!r} for /{cmd.name} clashes with "
                    f"existing command name"
                )
            if alias_key in self._aliases:
                raise ValueError(
                    f"Alias {alias_key!r} for /{cmd.name} clashes with "
                    f"existing alias of /{self._aliases[alias_key]}"
                )
            self._aliases[alias_key] = key

    def register_many(self, commands: Iterable[SlashCommand]) -> None:
        for cmd in commands:
            self.register(cmd)

    def lookup(self, raw: str) -> CommandLookup:
        """Resolve a raw input string to a ``CommandLookup``.

        Behaviour:
          * Empty input → ``error="empty input"``
          * Input not starting with ``/`` → ``command=None`` so the
            caller can treat it as free text (chit-chat / goal).
          * ``/`` alone (or ``/unknown``) → ``command=None`` with
            ``error`` set.
        """
        stripped = (raw or "").strip()
        if not stripped:
            return CommandLookup(None, error="empty input")
        if not stripped.startswith("/"):
            return CommandLookup(None)  # free text — not an error

        # Strip leading slashes and tokenize. shlex respects quotes.
        try:
            tokens = shlex.split(stripped[1:], posix=True)
        except ValueError as exc:
            # Unmatched quote, etc. Don't crash — return as unknown.
            return CommandLookup(None, error=f"could not parse args: {exc}")
        if not tokens:
            return CommandLookup(None, error="bare slash")

        head = tokens[0].lower()
        argv = tokens[1:]

        if head in self._commands:
            return CommandLookup(self._commands[head], argv=argv)
        if head in self._aliases:
            canonical = self._aliases[head]
            return CommandLookup(self._commands[canonical], argv=argv)

        return CommandLookup(None, error=f"unknown command: /{head}")

    # ── Read-only views ────────────────────────────────────────────────

    def get(self, name: str) -> Optional[SlashCommand]:
        key = name.lower()
        if key in self._commands:
            return self._commands[key]
        if key in self._aliases:
            return self._commands[self._aliases[key]]
        return None

    def all(self) -> list[SlashCommand]:
        """Every command, in registration order."""
        return list(self._commands.values())

    def visible(self) -> list[SlashCommand]:
        """Every command with ``show_in_help=True``."""
        return [c for c in self._commands.values() if c.show_in_help]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._commands)


# ── Help rendering ───────────────────────────────────────────────────────

def format_help_lines(registry: CommandRegistry) -> list[str]:
    """Return one ``/name args_help — summary`` line per visible command.

    The renderer (loop.py) wraps these in colour. Kept pure so it's
    easy to snapshot-test.
    """
    lines = []
    longest = max((len(_signature(c)) for c in registry.visible()), default=0)
    for cmd in registry.visible():
        sig = _signature(cmd)
        lines.append(f"  {sig.ljust(longest)}  {cmd.summary}")
    return lines


def _signature(cmd: SlashCommand) -> str:
    return f"/{cmd.name} {cmd.args_help}".rstrip()
