"""Routing tools — what the v2 router agent emits.

These tools express the four kinds of intent the router cares about.
Each ``run`` does the minimum to record the decision on ``state``;
heavy side effects (actually launching the research workflow) happen
back in ``chat_loop`` after the dispatcher returns. This keeps the
tools fast, pure, and trivially testable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.chat.tools.registry import Tool


# ── Argument schemas ─────────────────────────────────────────────────────


class StartGoalArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    goal: str = Field(..., min_length=1, max_length=2048)
    target: dict[str, float] | None = None


class RefineGoalArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    refinement: str = Field(..., min_length=1, max_length=2048)


class SetTargetArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    key:   str = Field(..., min_length=1, max_length=64,
                        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    value: float


class AnswerQuestionArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str = Field(..., min_length=1, max_length=4096)


# ── Handlers ─────────────────────────────────────────────────────────────


async def start_goal(state: Any, args: StartGoalArgs) -> dict:
    """Record a fresh research goal on state.

    The chat loop will see ``state.extras["pending_action"] == "start"``
    and trigger the confirmation prompt + pipeline launch.
    """
    state.extras["pending_action"] = "start"
    state.extras["pending_goal"] = args.goal
    if args.target:
        state.extras["pending_target"] = args.target
    return {"intent": "goal", "goal": args.goal, "target": args.target}


async def refine_goal(state: Any, args: RefineGoalArgs) -> dict:
    state.extras["pending_action"] = "refine"
    state.extras["pending_refinement"] = args.refinement
    return {"intent": "refinement", "refinement": args.refinement}


async def set_target(state: Any, args: SetTargetArgs) -> dict:
    """Merge one target key/value into state.target."""
    state.target = {**state.target, args.key: args.value}
    return {"intent": "set_target", "target": dict(state.target)}


async def answer_question(state: Any, args: AnswerQuestionArgs) -> dict:
    state.extras["pending_action"] = "answer"
    state.extras["pending_question"] = args.text
    return {"intent": "question", "text": args.text}


# ── Tool catalogue (registered by build_tool_registry) ───────────────────


TOOLS: list[Tool] = [
    Tool(
        name="start_research_goal",
        description=(
            "Call this when the user wants to start a research workflow — "
            "e.g. 'simulate silicon at 300K', 'compute the bandgap of GaAs'. "
            "If the user mentions a numeric target (e.g. 'near 1.1 eV'), "
            "include it in the target field."
        ),
        schema=StartGoalArgs,
        run=start_goal,
        side_effects=False,  # only records intent; actual launch is gated elsewhere
    ),
    Tool(
        name="refine_goal",
        description=(
            "Call this when the user is adding a constraint or correction to "
            "the active research goal — e.g. 'make the thickness smaller', "
            "'compliance should be > 0'. Only valid when a goal is already "
            "active."
        ),
        schema=RefineGoalArgs,
        run=refine_goal,
        side_effects=False,
    ),
    Tool(
        name="set_target",
        description=(
            "Call this when the user explicitly sets one numeric target "
            "key, e.g. 'target bandgap_ev=1.1' or 'change the target to 1.05'. "
            "Pass the canonical key (with units suffix) and the numeric value."
        ),
        schema=SetTargetArgs,
        run=set_target,
        side_effects=False,
    ),
    Tool(
        name="answer_question",
        description=(
            "Call this when the user asks a conversational question — "
            "'what is bandgap?', 'how does DFT work', 'hello'. Used when no "
            "research action is required."
        ),
        schema=AnswerQuestionArgs,
        run=answer_question,
        side_effects=False,
    ),
]
