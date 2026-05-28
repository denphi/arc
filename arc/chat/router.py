"""Input router — classify a raw user input into a Route.

The router is the **only** place where intent classification lives:

  1. Slash commands → looked up in the CommandRegistry.
  2. Conversational / questions → answered directly by the LLM.
  3. Research goals → fresh workflow.
  4. Refinements → constrain the active goal.

Decision order:
  * If input starts with ``/`` → command route (or unknown-command error).
  * Heuristic ``_is_question``:
      - True  → ``question`` route
      - False → ``goal`` or ``refinement`` (based on active state)
      - None  → ambiguous; ask the LLM via ``_llm_classify_intent``.

The router never executes side effects — it only classifies. The chat
loop is responsible for invoking the right handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from arc.chat.registry import CommandRegistry, SlashCommand


RouteKind = Literal[
    "command",        # slash command (cmd + argv)
    "command_error",  # malformed slash (e.g. /unknown, unmatched quote)
    "question",       # conversational — go to _answer_question
    "goal",           # research goal — confirm + launch
    "refinement",     # refinement of an active goal
    "set_target",     # explicit target update (key + numeric value)
    "noop",           # empty input
]


@dataclass(frozen=True)
class Route:
    """A routing decision.

    Read-only value object — see the frozen-dataclass note in
    ``arc.chat.__init__`` for the mutation contract.
    """
    kind: RouteKind
    text: str = ""
    command: Optional[SlashCommand] = None
    argv: list[str] = field(default_factory=list)
    error: Optional[str] = None
    # Structured arguments emitted by v2 tool routing (set_target etc.).
    # Free-text routes keep this empty.
    args: dict = field(default_factory=dict)


async def route_input(
    raw: str,
    *,
    registry: CommandRegistry,
    provider: Any | None,
    has_active_goal: bool,
    # Allow callers (tests) to inject the classifier functions to keep
    # the router itself dependency-light. Defaults wire to arc.chat.loop.
    is_question=None,
    llm_classify_intent=None,
    # Called once just before an LLM round-trip happens. The callback
    # may raise (e.g. ``RouterBudgetExceeded``) to refuse the call. Used
    # by chat_loop to enforce ``ChatState.router_call_budget`` on the
    # v1 heuristic+LLM path the same way v2 does.
    on_llm_call=None,
) -> Route:
    """Classify a single raw input string.

    Parameters
    ----------
    raw
        The user's input as typed.
    registry
        The CommandRegistry to look slash commands up in.
    provider
        Optional LLM provider for the ambiguous-input path. ``None`` →
        the LLM fallback short-circuits and we treat unknown free text
        as a goal.
    has_active_goal
        True when the session already has a primary goal + artifact —
        the router uses this to prefer ``refinement`` over ``goal`` for
        ambiguous LLM-classified inputs.
    is_question, llm_classify_intent
        Optional injection points for tests.
    """
    if is_question is None:
        from arc.chat.loop import _is_question as is_question_default
        is_question = is_question_default
    if llm_classify_intent is None:
        from arc.chat.loop import _llm_classify_intent as llm_default
        llm_classify_intent = llm_default

    stripped = (raw or "").strip()
    if not stripped:
        return Route(kind="noop")

    # Slash command path. registry.lookup handles normalization.
    if stripped.startswith("/") or stripped.startswith("\\"):
        # Honour the legacy backslash convention by translating to slash.
        normalized = "/" + stripped[1:] if stripped.startswith("\\") else stripped
        result = registry.lookup(normalized)
        if result.command is not None:
            return Route(kind="command", command=result.command, argv=result.argv,
                         text=normalized)
        # registry already classified the failure (unknown / bare slash /
        # malformed). Pass the error through.
        return Route(kind="command_error", text=normalized,
                     error=result.error or "unknown command")

    # Free text — classify intent.
    heuristic = is_question(stripped)
    if heuristic is True:
        return Route(kind="question", text=stripped)
    if heuristic is False:
        # Definitely a research verb. If a goal is active, treat as a
        # competing new-goal (caller will confirm); otherwise it's the
        # first goal of the session.
        return Route(kind="goal", text=stripped)

    # heuristic is None → uncertain. Ask the LLM. Fire the budget hook
    # first so callers can refuse before paying the round-trip.
    if on_llm_call is not None:
        on_llm_call()
    intent = await llm_classify_intent(provider, stripped, has_active_goal=has_active_goal)
    if intent == "question":
        return Route(kind="question", text=stripped)
    if intent == "refinement" and has_active_goal:
        return Route(kind="refinement", text=stripped)
    # Default → goal (LLM said goal, or said refinement with no active goal)
    return Route(kind="goal", text=stripped)
