"""SlashCommand + CommandRegistry tests (Phase 1)."""

import pytest

from arc.chat.registry import (
    SlashCommand,
    CommandRegistry,
    format_help_lines,
)


pytestmark = pytest.mark.chat


# ── Construction & registration ────────────────────────────────────────────

def test_registry_starts_empty():
    reg = CommandRegistry()
    assert len(reg) == 0
    assert reg.all() == []


async def _noop(state, argv):
    pass


def test_register_single_command():
    reg = CommandRegistry()
    cmd = SlashCommand(name="help", summary="Show help", handler=_noop)
    reg.register(cmd)
    assert "help" in reg
    assert reg.get("help") is cmd
    assert len(reg) == 1


def test_register_with_aliases_routes_to_canonical():
    reg = CommandRegistry()
    cmd = SlashCommand(name="quit", aliases=("exit", "q"), summary="Exit",
                       handler=_noop)
    reg.register(cmd)
    assert reg.get("exit") is cmd
    assert reg.get("q") is cmd
    # Aliases don't add to length
    assert len(reg) == 1


def test_register_duplicate_name_raises():
    reg = CommandRegistry()
    reg.register(SlashCommand("help", "x", _noop))
    with pytest.raises(ValueError, match="Duplicate"):
        reg.register(SlashCommand("help", "y", _noop))


def test_register_alias_clashing_with_name_raises():
    reg = CommandRegistry()
    reg.register(SlashCommand("quit", "exit", _noop))
    with pytest.raises(ValueError, match="clashes with existing command name"):
        reg.register(SlashCommand("foo", "bar", _noop, aliases=("quit",)))


def test_register_name_clashing_with_alias_raises():
    reg = CommandRegistry()
    reg.register(SlashCommand("quit", "x", _noop, aliases=("q",)))
    with pytest.raises(ValueError, match="clashes with existing alias"):
        reg.register(SlashCommand("q", "y", _noop))


def test_register_alias_clashing_with_alias_raises():
    reg = CommandRegistry()
    reg.register(SlashCommand("quit", "x", _noop, aliases=("q",)))
    with pytest.raises(ValueError, match="clashes with existing alias"):
        reg.register(SlashCommand("exit", "y", _noop, aliases=("q",)))


def test_register_many():
    reg = CommandRegistry()
    cmds = [SlashCommand("a", "x", _noop), SlashCommand("b", "y", _noop)]
    reg.register_many(cmds)
    assert len(reg) == 2


# ── Lookup ─────────────────────────────────────────────────────────────────

def test_lookup_empty_string():
    reg = CommandRegistry()
    result = reg.lookup("")
    assert result.command is None
    assert "empty" in result.error.lower()


def test_lookup_free_text_returns_no_command_no_error():
    reg = CommandRegistry()
    result = reg.lookup("simulate silicon")
    assert result.command is None
    assert result.error is None


def test_lookup_bare_slash_is_an_error():
    reg = CommandRegistry()
    result = reg.lookup("/")
    assert result.command is None
    assert result.error is not None


def test_lookup_unknown_command_returns_error_with_name():
    reg = CommandRegistry()
    reg.register(SlashCommand("help", "x", _noop))
    result = reg.lookup("/nonexistent")
    assert result.command is None
    assert "/nonexistent" in result.error


def test_lookup_simple_command_no_args():
    reg = CommandRegistry()
    cmd = SlashCommand("help", "show help", _noop)
    reg.register(cmd)
    result = reg.lookup("/help")
    assert result.command is cmd
    assert result.argv == []
    assert result.error is None


def test_lookup_command_with_args():
    reg = CommandRegistry()
    cmd = SlashCommand("services", "x", _noop)
    reg.register(cmd)
    result = reg.lookup("/services start catalog")
    assert result.command is cmd
    assert result.argv == ["start", "catalog"]


def test_lookup_via_alias():
    reg = CommandRegistry()
    cmd = SlashCommand("quit", "exit", _noop, aliases=("exit", "q"))
    reg.register(cmd)
    assert reg.lookup("/exit").command is cmd
    assert reg.lookup("/q").command is cmd
    assert reg.lookup("/QUIT").command is cmd  # case-insensitive


def test_lookup_respects_quoted_arguments():
    """Critical for /exec my-art \"param with spaces\". The current
    raw.split() corrupts this; shlex.split must preserve it."""
    reg = CommandRegistry()
    cmd = SlashCommand("exec", "x", _noop)
    reg.register(cmd)
    result = reg.lookup('/exec my-art "param with spaces"')
    assert result.argv == ["my-art", "param with spaces"]


def test_lookup_returns_error_on_unmatched_quote():
    """A malformed input shouldn't crash — return as parse error."""
    reg = CommandRegistry()
    reg.register(SlashCommand("exec", "x", _noop))
    result = reg.lookup('/exec "unclosed')
    assert result.command is None
    assert "parse" in result.error.lower() or "args" in result.error.lower()


def test_lookup_strips_leading_whitespace():
    reg = CommandRegistry()
    reg.register(SlashCommand("help", "x", _noop))
    assert reg.lookup("  /help  ").command is not None


# ── Lazy handler resolution ────────────────────────────────────────────────

def test_resolve_handler_callable_returns_callable():
    cmd = SlashCommand("help", "x", _noop)
    assert cmd.resolve_handler() is _noop


def test_resolve_handler_string_imports_lazily():
    # Use an existing function as the target
    cmd = SlashCommand("printf", "x", "builtins:print")
    fn = cmd.resolve_handler()
    assert fn is print  # builtins.print


def test_resolve_handler_invalid_string_raises():
    cmd = SlashCommand("bad", "x", "nonexistent.module:fn")
    with pytest.raises(RuntimeError, match="Could not resolve"):
        cmd.resolve_handler()


def test_resolve_handler_missing_attr_raises():
    cmd = SlashCommand("bad", "x", "arc.chat.registry:nonexistent_attr")
    with pytest.raises(RuntimeError, match="Could not resolve"):
        cmd.resolve_handler()


def test_resolve_handler_non_callable_raises():
    # Point at a clearly non-callable module attribute.
    cmd = SlashCommand("bad", "x", "sys:version")  # str, not callable
    with pytest.raises((TypeError, RuntimeError)):
        cmd.resolve_handler()


# ── show_in_help / visibility ──────────────────────────────────────────────

def test_visible_excludes_hidden_commands():
    reg = CommandRegistry()
    reg.register(SlashCommand("public", "x", _noop, show_in_help=True))
    reg.register(SlashCommand("hidden", "y", _noop, show_in_help=False))
    visible = reg.visible()
    assert len(visible) == 1
    assert visible[0].name == "public"


def test_format_help_lines_is_aligned():
    reg = CommandRegistry()
    reg.register(SlashCommand("a", "first", _noop))
    reg.register(SlashCommand("longerone", "second", _noop, args_help="[arg]"))
    lines = format_help_lines(reg)
    assert len(lines) == 2
    # Both lines should have the summary at the same column
    summary_cols = [line.index("first") if "first" in line else line.index("second")
                    for line in lines]
    assert summary_cols[0] == summary_cols[1], (
        f"help lines not column-aligned:\n{lines!r}"
    )


def test_format_help_lines_uses_args_help_in_signature():
    reg = CommandRegistry()
    reg.register(SlashCommand("services", "show services", _noop,
                              args_help="[start|stop] [name]"))
    lines = format_help_lines(reg)
    assert "/services [start|stop] [name]" in lines[0]


def test_format_help_lines_empty_registry():
    reg = CommandRegistry()
    assert format_help_lines(reg) == []
