"""Router tests (Phase 1).

The router is the only place intent classification lives. These tests
pin its public contract: input → Route. The classifier functions are
injected so we can exercise every code path without an LLM."""

import pytest

from arc.chat.registry import CommandRegistry, SlashCommand
from arc.chat.router import route_input, Route


pytestmark = pytest.mark.chat


async def _noop(state, argv):
    pass


def make_registry():
    reg = CommandRegistry()
    reg.register(SlashCommand("help", "show help", _noop, aliases=("?",)))
    reg.register(SlashCommand("services", "manage services", _noop,
                              args_help="[start|stop] [name]"))
    reg.register(SlashCommand("exec", "run artifact", _noop))
    reg.register(SlashCommand("quit", "exit", _noop, aliases=("exit", "q")))
    return reg


# ── Injection helpers ─────────────────────────────────────────────────────

def heuristic(value):
    """Return a sync ``_is_question`` that always returns ``value``."""
    def _h(text):
        return value
    return _h


def llm_returns(intent):
    """Return an awaitable ``_llm_classify_intent`` that always returns ``intent``."""
    async def _llm(provider, text, *, has_active_goal):
        return intent
    return _llm


# ── Empty / noop ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_input_is_noop():
    reg = make_registry()
    route = await route_input("", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(False))
    assert route.kind == "noop"


@pytest.mark.asyncio
async def test_whitespace_only_is_noop():
    reg = make_registry()
    route = await route_input("   ", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(False))
    assert route.kind == "noop"


# ── Slash commands ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slash_command_resolves():
    reg = make_registry()
    route = await route_input("/help", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command"
    assert route.command.name == "help"
    assert route.argv == []


@pytest.mark.asyncio
async def test_slash_command_with_args():
    reg = make_registry()
    route = await route_input("/services start catalog", registry=reg,
                              provider=None, has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command"
    assert route.command.name == "services"
    assert route.argv == ["start", "catalog"]


@pytest.mark.asyncio
async def test_backslash_normalised_to_slash():
    reg = make_registry()
    route = await route_input("\\help", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command"
    assert route.command.name == "help"


@pytest.mark.asyncio
async def test_alias_resolves():
    reg = make_registry()
    route = await route_input("/q", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command"
    assert route.command.name == "quit"


@pytest.mark.asyncio
async def test_unknown_command_returns_command_error():
    reg = make_registry()
    route = await route_input("/banana", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command_error"
    assert "/banana" in route.error


@pytest.mark.asyncio
async def test_bare_slash_is_command_error():
    reg = make_registry()
    route = await route_input("/", registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command_error"


@pytest.mark.asyncio
async def test_quoted_args_preserved():
    reg = make_registry()
    route = await route_input('/exec my-art "value with spaces"',
                              registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(None))
    assert route.kind == "command"
    assert route.argv == ["my-art", "value with spaces"]


# ── Free text — questions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heuristic_true_routes_to_question():
    reg = make_registry()
    route = await route_input("what is bandgap?",
                              registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(True))
    assert route.kind == "question"
    assert route.text == "what is bandgap?"


@pytest.mark.asyncio
async def test_heuristic_false_routes_to_goal():
    reg = make_registry()
    route = await route_input("simulate silicon",
                              registry=reg, provider=None,
                              has_active_goal=False,
                              is_question=heuristic(False))
    assert route.kind == "goal"


# ── Ambiguous → LLM fallback ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ambiguous_with_llm_question_routes_to_question():
    reg = make_registry()
    route = await route_input("bandgap of GaN",
                              registry=reg, provider="not-none",
                              has_active_goal=False,
                              is_question=heuristic(None),
                              llm_classify_intent=llm_returns("question"))
    assert route.kind == "question"


@pytest.mark.asyncio
async def test_ambiguous_with_llm_goal_routes_to_goal():
    reg = make_registry()
    route = await route_input("bandgap of GaN",
                              registry=reg, provider="x",
                              has_active_goal=False,
                              is_question=heuristic(None),
                              llm_classify_intent=llm_returns("goal"))
    assert route.kind == "goal"


@pytest.mark.asyncio
async def test_ambiguous_refinement_with_active_goal_routes_to_refinement():
    reg = make_registry()
    route = await route_input("make it bigger",
                              registry=reg, provider="x",
                              has_active_goal=True,
                              is_question=heuristic(None),
                              llm_classify_intent=llm_returns("refinement"))
    assert route.kind == "refinement"


@pytest.mark.asyncio
async def test_ambiguous_refinement_without_active_goal_falls_back_to_goal():
    """LLM may say 'refinement' but if nothing's active to refine, treat
    as a fresh goal."""
    reg = make_registry()
    route = await route_input("make it bigger",
                              registry=reg, provider="x",
                              has_active_goal=False,
                              is_question=heuristic(None),
                              llm_classify_intent=llm_returns("refinement"))
    assert route.kind == "goal"


# ── has_active_goal does NOT affect heuristic decisions ───────────────────

@pytest.mark.asyncio
async def test_question_routes_to_question_even_with_active_goal():
    reg = make_registry()
    route = await route_input("what is bandgap?", registry=reg,
                              provider="x", has_active_goal=True,
                              is_question=heuristic(True))
    assert route.kind == "question"


@pytest.mark.asyncio
async def test_strong_research_verb_routes_to_goal_even_with_active_goal():
    """When the user types a fresh imperative, treat as competing goal."""
    reg = make_registry()
    route = await route_input("simulate something new", registry=reg,
                              provider="x", has_active_goal=True,
                              is_question=heuristic(False))
    assert route.kind == "goal"


# ── Route dataclass is immutable ───────────────────────────────────────────

def test_route_is_frozen():
    route = Route(kind="noop")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        route.kind = "question"  # type: ignore[misc]


def test_route_defaults():
    route = Route(kind="question", text="hi")
    assert route.argv == []
    assert route.command is None
    assert route.args == {}
    assert route.error is None


def test_route_attributes_immutable_but_collections_are_not():
    """R3-6: pin the (slightly surprising) contract.

    ``frozen=True`` prevents reassignment of fields, but Python dataclass
    'frozen' doesn't recurse into mutable defaults. Document the
    contract so callers know not to mutate route.args / route.argv.
    """
    route = Route(kind="set_target", args={"key": "x", "value": 1.0})
    # Reassignment IS blocked
    with pytest.raises(Exception):
        route.kind = "noop"  # type: ignore[misc]
    # In-place dict / list mutation is NOT blocked by Python — that's
    # the contract caveat. We document it; we don't enforce it.
    route.args["key"] = "mutated-out-of-band"
    assert route.args["key"] == "mutated-out-of-band"


def test_no_chat_handler_mutates_route_args():
    """Static check: grep loop.py for any pattern that would mutate the
    incoming Route's args/argv. Catches a future commit that violates
    the read-only contract."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "arc" / "chat" / "loop.py").read_text()
    # Patterns that would mutate the route's collections in place
    forbidden = [
        "route.args[",      # e.g. route.args["x"] = ...
        "route.args.pop",   # e.g. route.args.pop(...)
        "route.args.update",
        "route.argv.append",
        "route.argv.pop",
    ]
    for pat in forbidden:
        # Read-only access like "route.args.get(" is fine; mutations are not.
        assert pat + "(" not in text and pat + " =" not in text, (
            f"loop.py mutates route field in place via {pat!r}"
        )


# ── on_llm_call budget hook (R3-3) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_llm_call_fires_when_heuristic_uncertain():
    """Heuristic returns None → LLM is consulted → hook fires."""
    reg = make_registry()
    calls = []
    route = await route_input(
        "ambiguous text", registry=reg, provider="x", has_active_goal=False,
        is_question=heuristic(None),
        llm_classify_intent=llm_returns("goal"),
        on_llm_call=lambda: calls.append(1),
    )
    assert calls == [1]
    assert route.kind == "goal"


@pytest.mark.asyncio
async def test_on_llm_call_skipped_when_heuristic_decisive():
    """Heuristic returns True/False → no LLM call → no hook fire."""
    reg = make_registry()
    calls = []
    await route_input(
        "what is bandgap?", registry=reg, provider="x", has_active_goal=False,
        is_question=heuristic(True),
        on_llm_call=lambda: calls.append(1),
    )
    assert calls == []


@pytest.mark.asyncio
async def test_on_llm_call_can_refuse_by_raising():
    """The hook can raise to refuse the call (budget enforcement)."""
    reg = make_registry()

    def deny():
        raise RuntimeError("budget exhausted")

    with pytest.raises(RuntimeError, match="budget"):
        await route_input(
            "ambiguous", registry=reg, provider="x", has_active_goal=False,
            is_question=heuristic(None),
            llm_classify_intent=llm_returns("question"),
            on_llm_call=deny,
        )
