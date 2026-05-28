"""Phase 4 security invariants.

Covers ``allowed_tools`` enforcement, cost-budget caps, plan-mode
integration with the tool dispatcher, and the router fallback.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest


pytestmark = pytest.mark.chat


CHAT_ROOT = Path(__file__).resolve().parents[1] / "arc" / "chat"


@dataclass
class _State:
    target: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)
    router_calls: int = 0
    cost_budget_usd: float = 1.0


# ── allowed_tools enforced at dispatch ────────────────────────────────────

@pytest.mark.asyncio
async def test_allowed_tools_rejects_unlisted_tool():
    from arc.chat.tools import build_tool_registry, ToolValidationError
    reg = build_tool_registry()
    state = _State()
    # The reviewer agent's allowed_tools is []; with allowed=[] it must
    # refuse *every* tool call.
    with pytest.raises(ToolValidationError, match="not in allowed_tools"):
        await reg.dispatch(
            state, "answer_question", {"text": "x"},
            allowed_tools=[],  # empty list → no tools permitted
        )


@pytest.mark.asyncio
async def test_allowed_tools_accepts_listed_tool():
    from arc.chat.tools import build_tool_registry
    reg = build_tool_registry()
    state = _State()
    # Should not raise
    await reg.dispatch(
        state, "answer_question", {"text": "x"},
        allowed_tools=["answer_question"],
    )


@pytest.mark.asyncio
async def test_allowed_tools_none_means_no_restriction():
    from arc.chat.tools import build_tool_registry
    reg = build_tool_registry()
    state = _State()
    # allowed_tools=None means "no filter", every tool runs
    await reg.dispatch(state, "answer_question", {"text": "x"}, allowed_tools=None)


# ── Cost budget enforcement ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zero_budget_raises_immediately():
    from arc.chat.tools import build_tool_registry, ToolBudgetExceeded
    reg = build_tool_registry()
    state = _State(cost_budget_usd=0)
    with pytest.raises(ToolBudgetExceeded):
        await reg.dispatch(state, "answer_question", {"text": "x"})


@pytest.mark.asyncio
async def test_negative_budget_raises():
    from arc.chat.tools import build_tool_registry, ToolBudgetExceeded
    reg = build_tool_registry()
    state = _State(cost_budget_usd=-0.5)
    with pytest.raises(ToolBudgetExceeded):
        await reg.dispatch(state, "answer_question", {"text": "x"})


# ── Side-effect tools refused in plan mode ────────────────────────────────

@pytest.mark.asyncio
async def test_side_effect_tool_blocked_in_plan_mode():
    from arc.chat.tools import Tool, ToolRegistry
    from arc.chat.tools.routing import StartGoalArgs
    from arc.chat.plan_mode import plan_mode, PlanModeBlocked

    async def writer(state, args):
        state.extras["wrote"] = True

    reg = ToolRegistry()
    reg.register(Tool(
        name="write_file",
        description="writes a file",
        schema=StartGoalArgs,
        run=writer,
        side_effects=True,
    ))
    state = _State()
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            await reg.dispatch(state, "write_file", {"goal": "x"})
    assert "wrote" not in state.extras


# ── AgentDefinition strict mode rejects typos ─────────────────────────────

def test_agent_definition_strict_extra_forbidden():
    from pydantic import ValidationError
    from arc.chat.agents.definition import AgentDefinition
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", typoed_field="oops")


# ── No subprocess usage in arc.chat.agents / arc.chat.tools ───────────────

def test_agents_and_tools_have_no_subprocess():
    """The agent / tool layer must never spawn subprocesses directly.
    Sub-process work belongs in ``arc.services`` (the daemon manager)."""
    for sub in ("agents", "tools"):
        root = CHAT_ROOT / sub
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            text = p.read_text()
            for forbidden in ("import subprocess", "from subprocess"):
                if forbidden in text:
                    pytest.fail(f"{p} imports subprocess — not allowed in chat.{sub}")


# ── Router v2 fallback chain never raises into the caller ────────────────

@pytest.mark.asyncio
async def test_router_v2_does_not_raise_on_garbage_provider_output():
    """The fallback chain must absorb every misbehaviour."""
    from arc.chat.router_v2 import route_via_tools
    from arc.chat.tools import build_tool_registry

    class Provider:
        async def complete(self, *args, **kwargs):
            return "<garbage> not json"
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=Provider(),
                                      registry=reg, has_active_goal=False)
    assert decision.tool == "answer_question"


@pytest.mark.asyncio
async def test_router_v2_falls_back_on_oversize_reply():
    """Even an enormous reply shouldn't crash — it falls through to
    fallback because parse fails on the prose."""
    from arc.chat.router_v2 import route_via_tools
    from arc.chat.tools import build_tool_registry

    class Provider:
        async def complete(self, *args, **kwargs):
            return "x" * 1_000_000  # 1 MB of garbage
    reg = build_tool_registry()
    decision = await route_via_tools("hi", provider=Provider(),
                                      registry=reg, has_active_goal=False)
    assert decision.tool == "answer_question"


# ── YAML safe_load only ──────────────────────────────────────────────────

def test_agent_loader_uses_yaml_safe_load_only():
    text = (CHAT_ROOT / "agents" / "definition.py").read_text()
    for bad in ("yaml.load(", "yaml.full_load(", "yaml.unsafe_load("):
        assert bad not in text, f"forbidden {bad!r} in agents/definition.py"
    assert "yaml.safe_load" in text
