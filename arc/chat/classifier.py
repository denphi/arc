"""Intent classification for the v1 router.

Two layers:

  * ``is_question(text)`` — heuristic only, returns True/False/None.
  * ``llm_classify_intent(provider, text, has_active_goal)`` — LLM call
    used when the heuristic is uncertain.

The legacy underscored names (``_is_question``, ``_llm_classify_intent``)
remain importable from ``arc.chat.loop`` via re-export.
"""

from __future__ import annotations

from typing import Any


# ── Static dictionaries ──────────────────────────────────────────────────


QUESTION_STARTERS: tuple[str, ...] = (
    "what ", "what's ", "whats ", "who ", "where ", "when ", "why ", "how ",
    "is ", "are ", "can ", "could ", "would ", "should ", "does ", "do ",
    "explain ", "describe ", "tell me", "show me", "list ", "define ",
    "help ", "help?", "?",
)


RESEARCH_STARTERS: tuple[str, ...] = (
    "simulate ", "model ", "compute ", "calculate ", "find ", "optimize ",
    "build ", "create ", "generate ", "run ", "design ", "study ", "analyse ",
    "analyze ", "explore ", "investigate ", "predict ", "estimate ",
    "develop ", "implement ", "write a simulation", "write a model",
    # "I want to <verb>" / "I need to <verb>" / "I'd like to <verb>"
    "i want to ", "i need to ", "i'd like to ", "i would like to ",
    "i need a ", "i want a ",
)


# Single-token conversational inputs that are never research goals
CONVERSATIONAL: frozenset[str] = frozenset({
    "hello", "hi", "hey", "hiya", "howdy", "greetings",
    "thanks", "thank", "ty", "thx", "cheers",
    "bye", "goodbye", "cya", "later",
    "ok", "okay", "sure", "yes", "no", "nope", "yep",
    "lol", "haha", "cool", "great", "nice", "wow", "awesome",
    "help",
})


_GREETING_PARTNERS: frozenset[str] = CONVERSATIONAL | {
    "there", "all", "everyone", "folks",
}


# ── Heuristic classifier ────────────────────────────────────────────────


def is_question(text: str) -> bool | None:
    """Fast heuristic classifier.

    Returns:
        ``True``  — definitely conversational (route to ``answer_question``)
        ``False`` — definitely a research goal (route to the workflow)
        ``None``  — uncertain (caller should fall back to the LLM)
    """
    stripped = text.strip()
    if stripped.endswith("?"):
        return True

    lower = stripped.lower()
    words = lower.split()
    n = len(words)
    if n == 0:
        # Whitespace-only input: nothing to classify. The REPL guards empty
        # input upstream, but is_question is a public helper, so don't crash
        # on words[0] for callers that don't.
        return None

    # Single conversational token, or short greeting phrase ("hi there", "hey!")
    if words[0].rstrip("!.,") in CONVERSATIONAL and (
        n == 1
        or (n <= 3 and words[-1].rstrip("!.,") in _GREETING_PARTNERS)
    ):
        return True

    # Strong research intent — always a goal
    for starter in RESEARCH_STARTERS:
        if lower.startswith(starter):
            return False

    # Short question pattern
    if n <= 10:
        for starter in QUESTION_STARTERS:
            if lower.startswith(starter):
                return True

    # Uncertain — let the LLM decide
    return None


# ── LLM fallback ─────────────────────────────────────────────────────────


INTENT_SYSTEM_PROMPT = """\
Classify the user message into exactly one of these intents:
  question   — a conversational question, greeting, or general inquiry
  goal       — a research or simulation task to run (build/compute/optimize/model/simulate)
  refinement — a constraint or correction on an already-stated research goal

Reply with a single lowercase word: question, goal, or refinement. No other text.\
"""


async def llm_classify_intent(
    provider: Any | None,
    text: str,
    *,
    has_active_goal: bool,
) -> str:
    """Ask the LLM to classify intent.

    Returns ``"question"``, ``"goal"``, or ``"refinement"``. Falls back
    to ``"goal"`` on any error so uncertain input doesn't silently drop.

    Cost: a single-token reply at ``temperature=0``. Cap ``max_tokens``
    tightly to avoid runaway billing.
    """
    if provider is None:
        # Stub mode — no LLM, default to goal so behaviour is unchanged
        return "goal"

    context = (
        "There is an active research goal in progress."
        if has_active_goal
        else "No research goal is active yet."
    )
    prompt = f"{context}\n\nUser message: {text!r}"

    try:
        reply = await provider.complete(
            prompt,
            system=INTENT_SYSTEM_PROMPT,
            max_tokens=10,
            temperature=0,
        )
        intent = reply.strip().lower().split()[0]
        if intent in ("question", "goal", "refinement"):
            return intent
    except Exception:
        pass
    return "goal"
