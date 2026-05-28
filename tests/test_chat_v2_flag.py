"""ARC_CHAT_V2 feature flag wiring (P4-1 fix)."""

import pytest

from arc.chat.commands import build_registry
from arc.chat.loop import _route_via_v2
from arc.chat.router import Route
from tests.fakes import FakeProvider


pytestmark = pytest.mark.chat


# ── adapter routes free text through v2 ──────────────────────────────────


@pytest.mark.asyncio
async def test_route_via_v2_routes_question_to_question():
    provider = FakeProvider(replies=[
        '{"tool": "answer_question", "args": {"text": "what is bandgap?"}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "what is bandgap?",
        registry=registry, provider=provider, has_active_goal=False,
    )
    assert isinstance(route, Route)
    assert route.kind == "question"


@pytest.mark.asyncio
async def test_route_via_v2_routes_goal_to_goal():
    provider = FakeProvider(replies=[
        '{"tool": "start_research_goal", "args": {"goal": "simulate Si"}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "simulate Si", registry=registry, provider=provider, has_active_goal=False,
    )
    assert route.kind == "goal"
    assert route.text == "simulate Si"


@pytest.mark.asyncio
async def test_route_via_v2_refinement_requires_active_goal():
    """LLM says refinement but no goal active → fall back to question."""
    provider = FakeProvider(replies=[
        '{"tool": "refine_goal", "args": {"refinement": "smaller thickness"}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "smaller thickness",
        registry=registry, provider=provider, has_active_goal=False,
    )
    assert route.kind == "question"  # fallback


@pytest.mark.asyncio
async def test_route_via_v2_refinement_when_active():
    provider = FakeProvider(replies=[
        '{"tool": "refine_goal", "args": {"refinement": "smaller thickness"}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "smaller thickness",
        registry=registry, provider=provider, has_active_goal=True,
    )
    assert route.kind == "refinement"


@pytest.mark.asyncio
async def test_route_via_v2_slash_commands_use_heuristic_path():
    """Slash commands MUST still go through the registry router — the v2
    LLM call would just be wasted latency."""
    provider = FakeProvider(replies=[])  # would crash if v2 invoked it
    registry = build_registry()
    route = await _route_via_v2(
        "/help", registry=registry, provider=provider, has_active_goal=False,
    )
    assert route.kind == "command"
    assert provider.calls == []  # LLM not consulted


@pytest.mark.asyncio
async def test_route_via_v2_set_target_emits_typed_route():
    """Finding #5: set_target produces a typed Route with args, NOT a
    refinement with the value round-tripped through text."""
    provider = FakeProvider(replies=[
        '{"tool": "set_target", "args": {"key": "bandgap_ev", "value": 1.1}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "set the target to 1.1",
        registry=registry, provider=provider, has_active_goal=True,
    )
    assert route.kind == "set_target"
    assert route.args == {"key": "bandgap_ev", "value": 1.1}


@pytest.mark.asyncio
async def test_route_via_v2_reuses_provided_tool_registry():
    """Finding #2: when chat_loop passes its cached tool_registry, the
    adapter must use it rather than build a fresh one.

    We verify by passing a registry with ONLY a non-default tool and
    asserting the v2 router routes through it.
    """
    from arc.chat.tools import build_tool_registry
    cached = build_tool_registry()
    # Sentinel: capture the registry id used by the v2 router
    seen_ids = []
    from arc.chat import router_v2 as v2_mod
    orig = v2_mod.route_via_tools
    async def spy(text, *, provider, registry, has_active_goal,
                  fallback_tool="answer_question", max_tokens=256):
        seen_ids.append(id(registry))
        return await orig(text, provider=provider, registry=registry,
                          has_active_goal=has_active_goal,
                          fallback_tool=fallback_tool, max_tokens=max_tokens)
    v2_mod.route_via_tools = spy
    try:
        provider = FakeProvider(replies=[
            '{"tool": "answer_question", "args": {"text": "hi"}}'
        ])
        await _route_via_v2(
            "hi", registry=build_registry(),
            provider=provider, has_active_goal=False,
            tool_registry=cached,
        )
    finally:
        v2_mod.route_via_tools = orig
    assert seen_ids == [id(cached)]


@pytest.mark.asyncio
async def test_route_via_v2_set_target_works_without_active_goal():
    """Targets can be set before a goal exists — they configure the
    intended outcome for the NEXT goal."""
    provider = FakeProvider(replies=[
        '{"tool": "set_target", "args": {"key": "bandgap_ev", "value": 1.1}}'
    ])
    registry = build_registry()
    route = await _route_via_v2(
        "I want bandgap = 1.1",
        registry=registry, provider=provider, has_active_goal=False,
    )
    assert route.kind == "set_target"


# ── ARC_CHAT_V2 env var read by chat_loop module load ───────────────────


def test_arc_chat_v2_flag_recognised(monkeypatch):
    """The flag is read inside chat_loop. We don't enter the REPL here —
    just confirm the env var lookup succeeds."""
    import os

    # The lookup happens *inside* chat_loop; we just verify the env-var
    # name is referenced in the loop file so a future refactor doesn't
    # accidentally drop it.
    from pathlib import Path
    loop_text = (Path(__file__).resolve().parents[1] / "arc" / "chat" / "loop.py").read_text()
    assert "ARC_CHAT_V2" in loop_text, "ARC_CHAT_V2 flag is no longer read"


def test_route_input_default_path_still_works():
    """Sanity: default (no ARC_CHAT_V2) path is undisturbed."""
    import asyncio
    from arc.chat.router import route_input

    async def runme():
        return await route_input(
            "/help", registry=build_registry(), provider=None,
            has_active_goal=False,
        )
    route = asyncio.run(runme())
    assert route.kind == "command"


# ── Router cost budget (Finding #10) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_router_call_budget_enforced():
    """When state.router_calls >= router_call_budget, raise."""
    from dataclasses import dataclass, field
    from arc.chat.loop import _route_via_v2, RouterBudgetExceeded

    @dataclass
    class _S:
        router_calls: int = 5
        router_call_budget: int = 5
        target: dict = field(default_factory=dict)
        extras: dict = field(default_factory=dict)

    state = _S()
    provider = FakeProvider(replies=[])
    with pytest.raises(RouterBudgetExceeded):
        await _route_via_v2(
            "anything", registry=build_registry(), provider=provider,
            has_active_goal=False, state=state,
        )
    # No LLM call happened
    assert provider.calls == []


@pytest.mark.asyncio
async def test_router_call_budget_ticks_per_call():
    """Each v2 routing call increments state.router_calls."""
    from dataclasses import dataclass, field
    from arc.chat.loop import _route_via_v2

    @dataclass
    class _S:
        router_calls: int = 0
        router_call_budget: int = 100
        target: dict = field(default_factory=dict)
        extras: dict = field(default_factory=dict)

    state = _S()
    provider = FakeProvider(replies=[
        '{"tool": "answer_question", "args": {"text": "hi"}}',
        '{"tool": "answer_question", "args": {"text": "again"}}',
    ])
    await _route_via_v2("hi", registry=build_registry(), provider=provider,
                       has_active_goal=False, state=state)
    await _route_via_v2("again", registry=build_registry(), provider=provider,
                       has_active_goal=False, state=state)
    assert state.router_calls == 2


@pytest.mark.asyncio
async def test_router_call_budget_does_not_tick_slash_commands():
    """Slash commands bypass v2; they must not count against the budget."""
    from dataclasses import dataclass, field
    from arc.chat.loop import _route_via_v2

    @dataclass
    class _S:
        router_calls: int = 0
        router_call_budget: int = 100
        target: dict = field(default_factory=dict)
        extras: dict = field(default_factory=dict)

    state = _S()
    await _route_via_v2("/help", registry=build_registry(), provider=None,
                       has_active_goal=False, state=state)
    assert state.router_calls == 0


@pytest.mark.asyncio
async def test_router_call_budget_does_not_tick_in_stub_mode():
    """R3-4: when provider is None, no LLM call is made — so the
    budget MUST NOT tick. Otherwise stub-mode sessions would exhaust
    the budget for free."""
    from dataclasses import dataclass, field
    from arc.chat.loop import _route_via_v2

    @dataclass
    class _S:
        router_calls: int = 0
        router_call_budget: int = 100
        target: dict = field(default_factory=dict)
        extras: dict = field(default_factory=dict)

    state = _S()
    # provider=None → no LLM call → no tick
    await _route_via_v2(
        "ambiguous text", registry=build_registry(),
        provider=None, has_active_goal=False, state=state,
    )
    assert state.router_calls == 0
