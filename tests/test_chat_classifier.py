"""Phase 0 baseline: pin the behaviour of the input classifier + LLM intent.

These tests must stay green through every later refactor.
"""

import pytest

from arc import chat
from tests.fakes import FakeProvider


pytestmark = pytest.mark.chat


# ── _is_question (heuristic) ───────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    # explicit question mark → always True
    ("what is bandgap?",                    True),
    ("?",                                   True),
    ("really?",                             True),

    # question words → True when short
    ("what is bandgap",                     True),
    ("how does DFT work",                   True),
    ("explain effective mass",              True),
    ("is silicon a semiconductor",          True),
    ("can you explain me bandgap",          True),
    ("define dielectric constant",          True),
    ("describe the workflow",               True),
    ("tell me about strain",                True),
    ("show me the artifacts",               True),
    ("list the available packages",         True),

    # conversational single tokens
    ("hello",                               True),
    ("hi",                                  True),
    ("hey",                                 True),
    ("thanks",                              True),
    ("bye",                                 True),
    ("ok",                                  True),
    ("cool",                                True),

    # multi-word greetings
    ("hi there",                            True),
    ("hey everyone",                        True),
    ("hello all",                           True),

    # research verbs → False
    ("simulate silicon at 300K",            False),
    ("compute bandgap of GaAs",             False),
    ("optimize bandgap to 1.1 eV",          False),
    ("model GaN",                           False),
    ("build a workflow for bandgap",        False),
    ("create a simulation",                 False),
    ("design an experiment",                False),
    ("predict bandgap",                     False),
    ("estimate effective mass",             False),
    ("write a simulation for strain",       False),

    # "I want to <verb>" / "I need a" / "I'd like to"
    ("I want to model GaN",                 False),
    ("I need to compute bandgap",           False),
    ("I'd like to optimize this",           False),
    ("I need a bandgap workflow",           False),

    # ambiguous / data-shape → None
    ("bandgap of GaN",                      None),
    ("silicon at 300K",                     None),
    ("GaN bandgap near 3.4 eV",             None),
    ("just some random text",               None),

    # empty / whitespace-only → None (must not raise IndexError on words[0])
    ("",                                    None),
    ("   ",                                 None),
    ("\t\n",                                None),
])
def test_is_question(text, expected):
    assert chat._is_question(text) is expected


# ── _llm_classify_intent (LLM fallback) ────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_classify_returns_question_when_llm_says_question():
    provider = FakeProvider(replies=["question"])
    intent = await chat._llm_classify_intent(provider, "what about X", has_active_goal=False)
    assert intent == "question"


@pytest.mark.asyncio
async def test_llm_classify_returns_goal_when_llm_says_goal():
    provider = FakeProvider(replies=["goal"])
    intent = await chat._llm_classify_intent(provider, "silicon at 300K", has_active_goal=False)
    assert intent == "goal"


@pytest.mark.asyncio
async def test_llm_classify_returns_refinement_when_llm_says_refinement():
    provider = FakeProvider(replies=["refinement"])
    intent = await chat._llm_classify_intent(provider, "make it 1.1eV", has_active_goal=True)
    assert intent == "refinement"


@pytest.mark.asyncio
async def test_llm_classify_handles_leading_whitespace_and_case():
    # Pin current behaviour: lowercase + first token after .split()
    provider = FakeProvider(replies=["  Question\n"])
    intent = await chat._llm_classify_intent(provider, "x", has_active_goal=False)
    assert intent == "question"


@pytest.mark.asyncio
async def test_llm_classify_falls_back_on_trailing_punctuation():
    # Known quirk: "question." is not equal to "question" so we fall back.
    # Documented here so Phase 1 can fix it intentionally rather than
    # accidentally regressing it.
    provider = FakeProvider(replies=["question."])
    intent = await chat._llm_classify_intent(provider, "x", has_active_goal=False)
    assert intent == "goal"  # fallback, not "question"


@pytest.mark.asyncio
async def test_llm_classify_falls_back_to_goal_on_garbage_reply():
    provider = FakeProvider(replies=["banana"])
    intent = await chat._llm_classify_intent(provider, "x", has_active_goal=False)
    assert intent == "goal"


@pytest.mark.asyncio
async def test_llm_classify_falls_back_to_goal_on_exception():
    class BoomProvider:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("API down")
    intent = await chat._llm_classify_intent(BoomProvider(), "x", has_active_goal=False)
    assert intent == "goal"


@pytest.mark.asyncio
async def test_llm_classify_returns_goal_in_stub_mode():
    intent = await chat._llm_classify_intent(None, "anything", has_active_goal=False)
    assert intent == "goal"


@pytest.mark.asyncio
async def test_llm_classify_call_is_cheap():
    """The intent call must use small max_tokens to keep cost bounded."""
    provider = FakeProvider(replies=["goal"])
    await chat._llm_classify_intent(provider, "x", has_active_goal=False)
    assert provider.calls, "expected at least one provider call"
    kwargs = provider.calls[0]["kwargs"]
    # Specific values are a contract: don't accidentally make this expensive.
    assert kwargs.get("max_tokens", 4096) <= 20
    assert kwargs.get("temperature", 1.0) == 0


@pytest.mark.asyncio
async def test_llm_classify_passes_active_goal_flag_in_prompt():
    provider = FakeProvider(replies=["refinement"])
    await chat._llm_classify_intent(provider, "make it bigger", has_active_goal=True)
    prompt = provider.calls[0]["prompt"]
    assert "active research goal" in prompt.lower()


@pytest.mark.asyncio
async def test_llm_classify_passes_no_active_goal_flag_in_prompt():
    provider = FakeProvider(replies=["goal"])
    await chat._llm_classify_intent(provider, "anything", has_active_goal=False)
    prompt = provider.calls[0]["prompt"]
    assert "no research goal" in prompt.lower()
