"""Chat-level tools + ToolRegistry tests (Phase 4)."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from arc.chat.tools import (
    Tool,
    ToolBudgetExceeded,
    ToolRegistry,
    ToolValidationError,
    build_tool_registry,
)
from arc.chat.tools.routing import (
    AnswerQuestionArgs,
    RefineGoalArgs,
    SetTargetArgs,
    StartGoalArgs,
    answer_question,
    refine_goal,
    set_target,
    start_goal,
)
from arc.chat.plan_mode import plan_mode, PlanModeBlocked


pytestmark = pytest.mark.chat


# Minimal state object for tool tests — mirrors ChatState surface
@dataclass
class FakeRouterState:
    target: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)
    router_calls: int = 0
    cost_budget_usd: float = 1.0


# ── Pydantic schemas reject malformed args ───────────────────────────────

def test_start_goal_rejects_empty_goal():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        StartGoalArgs(goal="")


def test_start_goal_rejects_oversize_goal():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        StartGoalArgs(goal="x" * 5000)


def test_set_target_rejects_path_like_key():
    """The pattern guards against attempts to set "../../etc/passwd" as a key."""
    from pydantic import ValidationError
    for bad in ("../etc", "a/b", "with spaces", "1starts_with_digit"):
        with pytest.raises(ValidationError):
            SetTargetArgs(key=bad, value=1.0)


def test_set_target_accepts_canonical_key():
    SetTargetArgs(key="bandgap_ev", value=1.1)  # no raise


def test_set_target_accepts_int_for_value():
    """Pydantic v2 strict mode allows int → float coercion; document it."""
    args = SetTargetArgs(key="bandgap_ev", value=1)
    assert args.value == 1.0
    assert isinstance(args.value, float)


def test_set_target_rejects_string_value():
    """If the LLM emits a stringified number, the schema rejects it and
    router_v2's fallback kicks in. Pin this so it doesn't accidentally
    start coercing strings."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SetTargetArgs(key="bandgap_ev", value="1.1")


def test_set_target_rejects_missing_value():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SetTargetArgs(key="bandgap_ev")


def test_set_target_rejects_none_value():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SetTargetArgs(key="bandgap_ev", value=None)


# ── ToolRegistry behaviour ───────────────────────────────────────────────

def test_register_rejects_duplicate():
    async def _noop(state, args):
        pass
    reg = ToolRegistry()
    t = Tool("x", "x", AnswerQuestionArgs, _noop)
    reg.register(t)
    with pytest.raises(ValueError, match="Duplicate"):
        reg.register(t)


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_raises():
    reg = build_tool_registry()
    with pytest.raises(ToolValidationError, match="unknown tool"):
        await reg.dispatch(FakeRouterState(), "no_such_tool", {})


@pytest.mark.asyncio
async def test_dispatch_invalid_args_raises():
    reg = build_tool_registry()
    with pytest.raises(ToolValidationError, match="invalid args"):
        await reg.dispatch(FakeRouterState(), "start_research_goal", {})  # missing goal


@pytest.mark.asyncio
async def test_dispatch_respects_allowed_tools():
    reg = build_tool_registry()
    state = FakeRouterState()
    with pytest.raises(ToolValidationError, match="not in allowed_tools"):
        await reg.dispatch(
            state, "start_research_goal",
            {"goal": "x"},
            allowed_tools=["answer_question"],
        )


@pytest.mark.asyncio
async def test_dispatch_increments_router_calls_counter():
    reg = build_tool_registry()
    state = FakeRouterState()
    await reg.dispatch(state, "answer_question", {"text": "hi"})
    assert state.router_calls == 1


@pytest.mark.asyncio
async def test_dispatch_raises_when_budget_exhausted():
    reg = build_tool_registry()
    state = FakeRouterState(cost_budget_usd=0)
    with pytest.raises(ToolBudgetExceeded):
        await reg.dispatch(state, "answer_question", {"text": "hi"})


@pytest.mark.asyncio
async def test_dispatch_does_not_tick_counter_when_budget_exhausted():
    """P4-4 regression: budget refusal must not bump router_calls."""
    reg = build_tool_registry()
    state = FakeRouterState(cost_budget_usd=0)
    with pytest.raises(ToolBudgetExceeded):
        await reg.dispatch(state, "answer_question", {"text": "hi"})
    assert state.router_calls == 0


@pytest.mark.asyncio
async def test_dispatch_does_not_tick_counter_in_plan_mode_for_side_effect_tool():
    """P4-4 regression: plan-mode refusal must not bump router_calls."""
    from arc.chat.tools import Tool, ToolRegistry
    from arc.chat.tools.routing import StartGoalArgs
    from arc.chat.plan_mode import plan_mode, PlanModeBlocked

    async def writer(state, args):
        pass

    reg = ToolRegistry()
    reg.register(Tool(
        name="write_file",
        description="writes a file",
        schema=StartGoalArgs,
        run=writer,
        side_effects=True,
    ))
    state = FakeRouterState()
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            await reg.dispatch(state, "write_file", {"goal": "x"})
    assert state.router_calls == 0


# ── Routing tools record intent ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_goal_records_intent_and_target():
    state = FakeRouterState()
    result = await start_goal(state, StartGoalArgs(goal="simulate Si",
                                                    target={"bandgap_ev": 1.1}))
    assert state.extras["pending_action"] == "start"
    assert state.extras["pending_goal"] == "simulate Si"
    assert state.extras["pending_target"] == {"bandgap_ev": 1.1}
    assert result["intent"] == "goal"


@pytest.mark.asyncio
async def test_refine_goal_records_refinement():
    state = FakeRouterState()
    await refine_goal(state, RefineGoalArgs(refinement="make it bigger"))
    assert state.extras["pending_action"] == "refine"
    assert state.extras["pending_refinement"] == "make it bigger"


@pytest.mark.asyncio
async def test_set_target_merges():
    state = FakeRouterState(target={"strain": 0.02})
    await set_target(state, SetTargetArgs(key="bandgap_ev", value=1.1))
    assert state.target == {"strain": 0.02, "bandgap_ev": 1.1}


@pytest.mark.asyncio
async def test_answer_question_records_text():
    state = FakeRouterState()
    await answer_question(state, AnswerQuestionArgs(text="what is bandgap?"))
    assert state.extras["pending_action"] == "answer"
    assert state.extras["pending_question"] == "what is bandgap?"


# ── Plan-mode integration ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_mode_blocks_side_effect_tools():
    """A tool with side_effects=True is refused in plan mode."""
    reg = ToolRegistry()

    async def _do_thing(state, args):
        state.extras["did_it"] = True

    reg.register(Tool(
        name="side_tool",
        description="does a thing",
        schema=AnswerQuestionArgs,
        run=_do_thing,
        side_effects=True,
    ))
    state = FakeRouterState()
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            await reg.dispatch(state, "side_tool", {"text": "go"})
    assert "did_it" not in state.extras


@pytest.mark.asyncio
async def test_plan_mode_allows_pure_routing_tools():
    """Plan mode must NOT block the routing tools (they have no side effects).

    Otherwise --plan would break the router completely.
    """
    reg = build_tool_registry()
    state = FakeRouterState()
    with plan_mode(True):
        await reg.dispatch(state, "answer_question", {"text": "hi"})
    assert state.extras["pending_action"] == "answer"


# ── build_tool_registry default contents ─────────────────────────────────

def test_default_registry_has_all_routing_tools():
    reg = build_tool_registry()
    expected = {
        "start_research_goal", "refine_goal", "set_target", "answer_question",
    }
    assert expected.issubset(set(reg.names()))
