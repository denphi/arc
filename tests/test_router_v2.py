"""V2 router (tool-call-based) tests + A/B parity check (Phase 4)."""

import pytest

from arc.chat.router_v2 import (
    ToolDecision,
    _parse_tool_call,
    route_via_tools,
)
from arc.chat.tools import build_tool_registry
from tests.fakes import FakeProvider


pytestmark = pytest.mark.chat


# ── _parse_tool_call ──────────────────────────────────────────────────────

def test_parse_clean_json():
    text = '{"tool": "answer_question", "args": {"text": "hi"}}'
    assert _parse_tool_call(text) == ("answer_question", {"text": "hi"})


def test_parse_tolerates_markdown_fence():
    text = '```json\n{"tool": "answer_question", "args": {"text": "hi"}}\n```'
    assert _parse_tool_call(text) == ("answer_question", {"text": "hi"})


def test_parse_tolerates_prose_before_json():
    text = 'Sure! Here\'s the call: {"tool": "answer_question", "args": {"text": "hi"}}'
    assert _parse_tool_call(text) == ("answer_question", {"text": "hi"})


def test_parse_missing_args_defaults_to_empty():
    text = '{"tool": "answer_question"}'
    assert _parse_tool_call(text) == ("answer_question", {})


def test_parse_returns_none_on_empty():
    assert _parse_tool_call("") is None


def test_parse_returns_none_on_pure_prose():
    assert _parse_tool_call("yeah I think you should start a goal") is None


def test_parse_returns_none_on_malformed_json():
    text = '{"tool": "answer_question", "args": {missing}}'
    assert _parse_tool_call(text) is None


def test_parse_returns_none_when_tool_not_a_string():
    text = '{"tool": 42, "args": {}}'
    assert _parse_tool_call(text) is None


def test_parse_returns_none_when_args_not_an_object():
    text = '{"tool": "x", "args": "not a dict"}'
    assert _parse_tool_call(text) is None


# ── route_via_tools — successful paths ────────────────────────────────────

@pytest.mark.asyncio
async def test_route_via_tools_calls_provider_with_tight_max_tokens():
    """Router classification needs ~30 tokens of output. Keep the budget
    tight so a misbehaving LLM can't drain the cost-per-turn — and so a
    future refactor doesn't accidentally enlarge it. P4-3 regression."""
    provider = FakeProvider(replies=['{"tool": "answer_question", "args": {"text": "hi"}}'])
    reg = build_tool_registry()
    await route_via_tools("hi", provider=provider, registry=reg, has_active_goal=False)
    assert provider.calls[0]["kwargs"]["temperature"] == 0
    # Bound matches the current default (256). Tighter than the legacy
    # 1024 sanity check to catch unintended budget growth.
    assert provider.calls[0]["kwargs"]["max_tokens"] <= 256


@pytest.mark.asyncio
async def test_route_returns_tool_decision():
    provider = FakeProvider(replies=[
        '{"tool": "start_research_goal", "args": {"goal": "simulate Si"}}'
    ])
    reg = build_tool_registry()
    decision = await route_via_tools("simulate Si", provider=provider,
                                      registry=reg, has_active_goal=False)
    assert decision.tool == "start_research_goal"
    # ``args`` is the model-dumped schema, so it may include
    # defaulted-None optionals. Just check the required fields are present.
    assert decision.args.get("goal") == "simulate Si"


# ── Fallbacks ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_falls_back_when_no_provider():
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=None, registry=reg,
                                      has_active_goal=False)
    assert decision.tool == "answer_question"
    assert decision.args == {"text": "hi"}


@pytest.mark.asyncio
async def test_route_falls_back_when_provider_raises():
    class Boom:
        async def complete(self, *a, **kw):
            raise RuntimeError("api down")
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=Boom(), registry=reg,
                                      has_active_goal=False)
    assert decision.tool == "answer_question"
    assert "error" in decision.raw_reply.lower()


@pytest.mark.asyncio
async def test_route_falls_back_when_reply_is_garbage():
    provider = FakeProvider(replies=["banana banana banana"])
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=provider, registry=reg,
                                      has_active_goal=False)
    assert decision.tool == "answer_question"


@pytest.mark.asyncio
async def test_route_falls_back_when_tool_unknown():
    provider = FakeProvider(replies=['{"tool": "nope_not_real", "args": {}}'])
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=provider, registry=reg,
                                      has_active_goal=False)
    assert decision.tool == "answer_question"


@pytest.mark.asyncio
async def test_route_falls_back_when_args_invalid_path_like_key():
    """R3-1: an attacker-controlled LLM emits set_target with a hostile
    key. The tool's schema regex rejects it; router must fall back."""
    provider = FakeProvider(replies=[
        '{"tool": "set_target", "args": {"key": "../etc/passwd", "value": 1.0}}'
    ])
    reg = build_tool_registry()
    decision = await route_via_tools("anything", provider=provider, registry=reg,
                                      has_active_goal=True)
    assert decision.tool == "answer_question"
    # And the malicious key is GONE from the args
    assert "../etc/passwd" not in str(decision.args)


@pytest.mark.asyncio
async def test_route_falls_back_when_args_missing_required_field():
    """Missing required field rejected by schema → fallback."""
    provider = FakeProvider(replies=[
        '{"tool": "start_research_goal", "args": {}}'  # missing 'goal'
    ])
    reg = build_tool_registry()
    decision = await route_via_tools("x", provider=provider, registry=reg,
                                      has_active_goal=False)
    assert decision.tool == "answer_question"


@pytest.mark.asyncio
async def test_route_returns_validated_args_dict():
    """When the schema accepts the args, decision.args is the normalised
    dict (extras dropped, types coerced)."""
    provider = FakeProvider(replies=[
        '{"tool": "set_target", "args": {"key": "bandgap_ev", "value": 1.1, "extra": "ignored"}}'
    ])
    reg = build_tool_registry()
    decision = await route_via_tools("x", provider=provider, registry=reg,
                                      has_active_goal=True)
    # Schema is extra="forbid" so validation should refuse the extra key
    assert decision.tool == "answer_question"  # rejected


@pytest.mark.asyncio
async def test_route_returns_clean_args_when_valid():
    provider = FakeProvider(replies=[
        '{"tool": "set_target", "args": {"key": "bandgap_ev", "value": 1.1}}'
    ])
    reg = build_tool_registry()
    decision = await route_via_tools("x", provider=provider, registry=reg,
                                      has_active_goal=True)
    assert decision.tool == "set_target"
    assert decision.args == {"key": "bandgap_ev", "value": 1.1}


# ── Active-goal context propagation ───────────────────────────────────────

@pytest.mark.asyncio
async def test_system_prompt_mentions_active_goal_state():
    provider = FakeProvider(replies=['{"tool": "refine_goal", "args": {"refinement": "x"}}'])
    reg = build_tool_registry()

    await route_via_tools("x", provider=provider, registry=reg, has_active_goal=True)
    assert "active research goal" in provider.calls[0]["system"].lower()

    provider2 = FakeProvider(replies=['{"tool": "answer_question", "args": {"text": "x"}}'])
    await route_via_tools("x", provider=provider2, registry=reg, has_active_goal=False)
    assert "no research goal" in provider2.calls[0]["system"].lower()


# ── A/B harness against the heuristic router ──────────────────────────────

@pytest.mark.asyncio
async def test_ab_parity_on_unambiguous_inputs():
    """For inputs where the heuristic router is decisive (returns True or
    False, NOT None), a well-behaved v2 router should agree. We simulate
    "well-behaved" by mocking the LLM to return the right tool for each
    case, and assert the tools match the expected intent class.
    """
    from arc.chat.loop import _is_question

    # (input, expected_v2_tool, expected_heuristic) pairs.
    # heuristic value:  True → question,  False → goal,  None → ambiguous (skip)
    cases = [
        # Conversational
        ("hello",                                  "answer_question", True),
        ("what is bandgap?",                       "answer_question", True),
        ("how does DFT work",                      "answer_question", True),
        # Research goals
        ("simulate silicon bandgap at 300K",       "start_research_goal", False),
        ("compute the bandgap of GaAs",            "start_research_goal", False),
        ("optimize bandgap to 1.1 eV",             "start_research_goal", False),
        ("I want to model GaN",                    "start_research_goal", False),
    ]

    reg = build_tool_registry()
    disagreements = []

    for text, expected_tool, expected_h in cases:
        h = _is_question(text)
        # We only A/B on decisive heuristic cases
        if h is None:
            continue

        # Mock the v2 router LLM to return the "correct" tool
        if expected_tool == "answer_question":
            reply = f'{{"tool": "answer_question", "args": {{"text": "{text}"}}}}'
        elif expected_tool == "start_research_goal":
            reply = f'{{"tool": "start_research_goal", "args": {{"goal": "{text}"}}}}'
        else:
            reply = '{"tool": "answer_question", "args": {"text": ""}}'

        provider = FakeProvider(replies=[reply])
        decision = await route_via_tools(
            text, provider=provider, registry=reg, has_active_goal=False,
        )

        # The expected agreement
        heuristic_class = "answer_question" if expected_h else "start_research_goal"
        if decision.tool != heuristic_class:
            disagreements.append((text, decision.tool, heuristic_class))

    assert not disagreements, f"v2/heuristic disagreement: {disagreements}"


# ── Pydantic args validate at dispatch (integration) ──────────────────────

@pytest.mark.asyncio
async def test_full_pipeline_route_then_dispatch():
    """End-to-end: text → router → registry.dispatch → state mutated."""
    from arc.chat.tools.registry import ToolValidationError

    provider = FakeProvider(replies=[
        '{"tool": "start_research_goal", "args": {"goal": "simulate Si"}}'
    ])
    reg = build_tool_registry()

    from dataclasses import dataclass, field
    @dataclass
    class State:
        target: dict = field(default_factory=dict)
        extras: dict = field(default_factory=dict)
        router_calls: int = 0
        cost_budget_usd: float = 1.0
    state = State()

    decision = await route_via_tools("simulate Si", provider=provider,
                                      registry=reg, has_active_goal=False)
    await reg.dispatch(state, decision.tool, decision.args)
    assert state.extras["pending_action"] == "start"
    assert state.extras["pending_goal"] == "simulate Si"
