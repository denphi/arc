"""Prompt-template registry and ideator integration.

Pins three things:

  * ``find_prompt`` honours the documented precedence (runtime override
    > domain-prefixed file > role-only file > None).
  * The arc-materials hypothesis prompt is discoverable + renderable
    against the ideator's field set without raising on missing keys.
  * ``IdeatorAgent`` uses the domain template when ``goal.domain``
    matches and falls back to its built-in template otherwise.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.schemas.research import ResearchGoal, ResearchProposal


pytestmark = pytest.mark.chat


# ── Discovery + parsing ────────────────────────────────────────────────


def test_list_prompts_includes_arc_materials_hypothesis():
    """arc-materials' hypothesis prompt is discovered automatically."""
    from arc.core.prompts import list_prompts
    assert "materials_hypothesis_generation" in list_prompts()


def test_get_prompt_returns_body_without_h1_line():
    """Body starts after the ``# Heading``, not at the heading itself."""
    from arc.core.prompts import get_prompt
    body = get_prompt("materials_hypothesis_generation")
    assert body is not None
    assert not body.lstrip().startswith("# materials_hypothesis_generation")
    assert "materials science" in body.lower()


def test_get_prompt_returns_none_for_unknown_name():
    from arc.core.prompts import get_prompt
    assert get_prompt("not_a_real_prompt_name") is None


# ── find_prompt precedence ─────────────────────────────────────────────


def test_find_prompt_picks_domain_specific_when_available():
    from arc.core.prompts import find_prompt
    body = find_prompt("hypothesis_generation", domain="materials")
    assert body is not None
    assert "materials science" in body.lower()


def test_find_prompt_falls_back_to_role_only_when_no_domain_match():
    """An unrecognised domain should not block discovery of a role-only file."""
    from arc.core.prompts import find_prompt
    body = find_prompt("hypothesis_generation", domain="not_a_real_domain")
    # No role-only file ships today, so this is None — confirming the
    # fallback path. If we later ship one, this test will catch it.
    assert body is None


def test_find_prompt_returns_none_for_unknown_role():
    from arc.core.prompts import find_prompt
    assert find_prompt("no_such_role", domain="materials") is None


def test_runtime_override_beats_package_file():
    """A user-supplied override wins over the discovered file."""
    from arc.core.prompts import find_prompt
    overrides = {"hypothesis_generation": "raw override template {goal}"}
    body = find_prompt(
        "hypothesis_generation", domain="materials", overrides=overrides,
    )
    assert body == "raw override template {goal}"


def test_runtime_override_keyed_by_role_not_domain():
    """The override map is role-keyed (matches ``memory["prompt_overrides"]``
    convention from /strategy)."""
    from arc.core.prompts import find_prompt
    overrides = {"hypothesis_generation": "X"}
    # Override fires for any domain — it's the highest tier.
    assert find_prompt("hypothesis_generation", domain="chemistry", overrides=overrides) == "X"
    assert find_prompt("hypothesis_generation", domain=None, overrides=overrides) == "X"


def test_empty_override_falls_through():
    """An empty/None override does not silence the package lookup."""
    from arc.core.prompts import find_prompt
    body = find_prompt(
        "hypothesis_generation", domain="materials",
        overrides={"hypothesis_generation": ""},
    )
    assert body is not None
    assert "materials science" in body.lower()


# ── safe_format ────────────────────────────────────────────────────────


def test_safe_format_renders_known_fields():
    from arc.core.prompts import safe_format
    out = safe_format("hello {goal}", goal="world")
    assert out == "hello world"


def test_safe_format_leaves_unknown_fields_intact():
    """Unknown placeholders must render as themselves, not raise."""
    from arc.core.prompts import safe_format
    out = safe_format("a={a} b={b}", a=1)
    assert "a=1" in out
    assert "{b}" in out


def test_materials_template_renders_with_ideator_fields():
    """The shipped materials prompt formats cleanly with the ideator's
    field set (no KeyError, no leftover unfilled core fields)."""
    from arc.core.prompts import find_prompt, safe_format

    template = find_prompt("hypothesis_generation", domain="materials")
    rendered = safe_format(
        template,
        goal="design a silicon nanowire with bandgap = 1.1 eV",
        domain="materials",
        constraints={},
        context="no prior runs",
        target_property="bandgap_ev",
        material_system="silicon nanowire",
        simulation_method="DFT",
    )
    # Each placeholder the ideator supplies should land in the output.
    assert "silicon nanowire" in rendered
    assert "bandgap_ev" in rendered
    assert "DFT" in rendered
    # And no leftover `{goal}` markers from unfilled placeholders.
    assert "{goal}" not in rendered
    assert "{context}" not in rendered


# ── Ideator integration ───────────────────────────────────────────────


class _RecordingProvider:
    """Stand-in provider that captures the prompt + returns a canned proposal."""

    def __init__(self):
        self.prompts: list[str] = []

    async def complete_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return schema(
            hypothesis="canned hypothesis",
            objective="canned objective",
            variables=["thickness_nm", "bandgap_ev"],
            methodology="DFT",
            expected_outcomes="thicker → smaller gap",
            evaluation_metrics=["bandgap_ev"],
        )


def _ctx(memory_overrides=None):
    memory = dict(memory_overrides or {})
    return SimpleNamespace(memory=memory)


def test_ideator_uses_materials_prompt_when_domain_is_materials():
    """``goal.domain='materials'`` → ideator prompt contains the
    materials template, not the generic fallback."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    provider = _RecordingProvider()
    ctx = _ctx({"provider": provider})
    goal = ResearchGoal(goal="design a silicon bandgap material", domain="materials")
    result = asyncio.run(IdeatorAgent(context=ctx).run(goal))

    assert isinstance(result, ResearchProposal)
    assert provider.prompts, "ideator should have called the provider once"
    rendered = provider.prompts[0]
    assert "materials science researcher" in rendered.lower()
    assert "ResearchProposal schema" in rendered


def test_ideator_falls_back_to_default_when_domain_unknown():
    """Unknown domain → built-in template (which says "scientific research
    assistant"), not the materials one."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    provider = _RecordingProvider()
    ctx = _ctx({"provider": provider})
    goal = ResearchGoal(goal="some bird-watching study", domain="ornithology")
    asyncio.run(IdeatorAgent(context=ctx).run(goal))

    rendered = provider.prompts[0]
    assert "scientific research assistant" in rendered.lower()
    assert "materials science researcher" not in rendered.lower()


def test_ideator_honours_runtime_prompt_override():
    """An override on ``memory["prompt_overrides"]`` wins over the
    materials template — even when domain=materials."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    provider = _RecordingProvider()
    ctx = _ctx({
        "provider": provider,
        "prompt_overrides": {
            "hypothesis_generation": "CUSTOM PROMPT goal={goal} domain={domain}",
        },
    })
    goal = ResearchGoal(goal="custom goal", domain="materials")
    asyncio.run(IdeatorAgent(context=ctx).run(goal))

    rendered = provider.prompts[0]
    assert rendered.startswith("CUSTOM PROMPT goal=custom goal domain=materials")
    assert "materials science researcher" not in rendered.lower()


def test_ideator_template_render_does_not_crash_on_minimal_goal():
    """A goal with no constraints/target shouldn't leave unfilled
    placeholders that confuse the LLM."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    provider = _RecordingProvider()
    ctx = _ctx({"provider": provider})
    goal = ResearchGoal(goal="study x", domain="materials")
    asyncio.run(IdeatorAgent(context=ctx).run(goal))

    rendered = provider.prompts[0]
    # Empty placeholders should be filled with sensible defaults
    # ("unspecified" / "any computational method"), not raw `{material_system}`.
    assert "{material_system}" not in rendered
    assert "{simulation_method}" not in rendered
    assert "{target_property}" not in rendered
