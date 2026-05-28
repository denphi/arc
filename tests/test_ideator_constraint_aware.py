"""ConstraintAwareIdeatorAgent — wires the arc-materials vocabulary
into the LLM prompt.

Drop-in replacement for ``IdeatorAgent``. When ``goal.domain ==
'materials'`` the agent reads ``arc-materials/vocabularies/*.yaml``
and prepends a canonical-names + physical-ranges + suitable-methods
block to every prompt the wrapped provider sees.

Without a domain match, behaves identically to the default ideator.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.packages.arc_sim2l_agents.ideator_constraint_aware import (
    _domain_to_package_dir,
    _load_vocab_yaml,
    _render_vocabulary_block,
)
from arc.schemas.research import ResearchGoal, ResearchProposal


pytestmark = pytest.mark.chat


# ── _domain_to_package_dir ─────────────────────────────────────────────


def test_domain_mapping_materials():
    assert _domain_to_package_dir("materials") == "arc-materials"


def test_domain_mapping_case_insensitive():
    assert _domain_to_package_dir("Materials") == "arc-materials"
    assert _domain_to_package_dir("MATERIALS") == "arc-materials"


def test_domain_mapping_unknown_returns_none():
    assert _domain_to_package_dir("ornithology") is None
    assert _domain_to_package_dir("") is None
    assert _domain_to_package_dir(None) is None


# ── _load_vocab_yaml ───────────────────────────────────────────────────


def test_load_vocab_yaml_returns_dict_for_existing_file():
    data = _load_vocab_yaml("arc-materials", "materials_properties.yaml")
    assert isinstance(data, dict)
    assert "properties" in data


def test_load_vocab_yaml_returns_none_for_missing_file():
    assert _load_vocab_yaml("arc-materials", "does-not-exist.yaml") is None


def test_load_vocab_yaml_returns_none_for_missing_package():
    assert _load_vocab_yaml("arc-does-not-exist", "anything.yaml") is None


# ── _render_vocabulary_block ───────────────────────────────────────────


def test_vocabulary_block_empty_for_unknown_domain():
    assert _render_vocabulary_block("ornithology", []) == ""


def test_vocabulary_block_includes_property_names_and_units():
    block = _render_vocabulary_block("materials", [])
    assert "band_gap" in block
    assert "eV" in block
    assert "formation_energy" in block


def test_vocabulary_block_includes_simulation_methods():
    block = _render_vocabulary_block("materials", [])
    # DFT is the headline method in the shipped vocabulary.
    assert "dft" in block.lower() or "DFT" in block
    assert "Density Functional Theory" in block


def test_vocabulary_block_promotes_target_properties():
    """A goal targeting ``band_gap`` should see band_gap before
    ``magnetic_moment`` in the prompt block — relevance ordering."""
    block = _render_vocabulary_block("materials", ["band_gap"])
    bg = block.find("band_gap")
    mm = block.find("magnetic_moment")
    if mm == -1:
        pytest.skip("vocabulary lacks magnetic_moment; nothing to order")
    assert bg < mm


def test_vocabulary_block_highlights_relevant_methods_for_target():
    """When the target is band_gap, DFT (suitable_for=band_gap) should
    appear before molecular_dynamics (not suitable)."""
    block = _render_vocabulary_block("materials", ["band_gap"])
    # Find positions in the method list.
    dft_idx = block.find("dft (Density Functional Theory)")
    md_idx = block.find("md (Molecular Dynamics)")
    if md_idx == -1:
        pytest.skip("vocabulary lacks md; nothing to order")
    assert 0 <= dft_idx < md_idx


def test_vocabulary_block_caps_property_count():
    """The prompt block should bound itself — never dump 30+ properties.

    We slice the block at the "Simulation methods" heading so we only
    count the property bullets, not the method bullets that follow.
    """
    block = _render_vocabulary_block("materials", [])
    properties_section = block.split("Simulation methods", 1)[0]
    prop_lines = [
        line for line in properties_section.splitlines()
        if line.startswith("  - ")
    ]
    assert len(prop_lines) <= 12


def test_vocabulary_block_includes_constraint_directive():
    """The block ends with an explicit "stay inside ranges" instruction."""
    block = _render_vocabulary_block("materials", [])
    assert "ranges" in block.lower()
    assert "units" in block.lower()


# ── Agent contract ─────────────────────────────────────────────────────


def _resolve_agent():
    from arc.core.strategies import resolve_role
    return resolve_role("ideator", overrides={"ideator": "constraint_aware"})


def test_resolver_returns_constraint_aware_class():
    assert _resolve_agent().__name__ == "ConstraintAwareIdeatorAgent"


def test_default_ideator_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("ideator").__name__ == "IdeatorAgent"


class _CapturingProvider:
    """Stub that captures every prompt + returns a canned proposal."""

    def __init__(self):
        self.prompts: list[str] = []

    async def complete_structured(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return schema(
            hypothesis="canned hypothesis",
            objective="canned objective",
            variables=["band_gap"],
            methodology="DFT",
            expected_outcomes="x",
            evaluation_metrics=["band_gap"],
        )

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "canned"


def _context(provider=None, memory=None):
    base = dict(memory or {})
    if provider is not None:
        base["provider"] = provider
    return SimpleNamespace(memory=base)


def test_run_injects_vocabulary_block_for_materials_goal():
    """A materials goal → the captured prompt contains canonical names."""
    provider = _CapturingProvider()
    ctx = _context(provider=provider)
    agent = _resolve_agent()(context=ctx)
    asyncio.run(agent.run(ResearchGoal(
        goal="design a silicon-based bandgap engineer",
        domain="materials",
        target={"band_gap": 1.1},
    )))
    assert provider.prompts, "expected the LLM to be called"
    p = provider.prompts[0]
    assert "Domain vocabulary for ``materials``" in p
    assert "band_gap" in p
    assert "eV" in p


def test_run_skips_vocabulary_for_non_materials_goal():
    """Non-materials domain → vocab block absent (zero regression)."""
    provider = _CapturingProvider()
    ctx = _context(provider=provider)
    agent = _resolve_agent()(context=ctx)
    asyncio.run(agent.run(ResearchGoal(
        goal="study bird migration",
        domain="ornithology",
    )))
    assert provider.prompts
    p = provider.prompts[0]
    assert "Domain vocabulary" not in p


def test_run_skips_vocabulary_for_no_domain():
    provider = _CapturingProvider()
    ctx = _context(provider=provider)
    agent = _resolve_agent()(context=ctx)
    asyncio.run(agent.run(ResearchGoal(goal="generic goal", domain=None)))
    assert provider.prompts
    assert "Domain vocabulary" not in provider.prompts[0]


def test_run_returns_research_proposal_shape():
    """Contract: same return type as the default ideator."""
    ctx = _context(provider=_CapturingProvider())
    agent = _resolve_agent()(context=ctx)
    result = asyncio.run(agent.run(ResearchGoal(
        goal="design silicon", domain="materials",
        target={"band_gap": 1.1},
    )))
    assert isinstance(result, ResearchProposal)


def test_run_works_without_provider():
    """No provider → falls back to the default ideator's stub proposal.
    Vocab injection requires a provider; the agent must still produce
    a proposal."""
    agent = _resolve_agent()(context=_context())
    result = asyncio.run(agent.run(ResearchGoal(
        goal="design silicon", domain="materials",
    )))
    assert isinstance(result, ResearchProposal)


def test_run_promotes_target_property_in_prompt():
    """An ``band_gap`` target → band_gap appears in the rendered vocab
    *before* unrelated properties like ``magnetic_moment``."""
    provider = _CapturingProvider()
    ctx = _context(provider=provider)
    agent = _resolve_agent()(context=ctx)
    asyncio.run(agent.run(ResearchGoal(
        goal="design something",
        domain="materials",
        target={"band_gap": 1.5},
    )))
    p = provider.prompts[0]
    bg_idx = p.find("band_gap")
    mm_idx = p.find("magnetic_moment")
    if mm_idx == -1:
        pytest.skip("vocabulary lacks magnetic_moment; nothing to order")
    assert bg_idx < mm_idx


def test_vocab_wrapper_exposes_underlying_provider_attributes():
    """The provider wrapper must forward arbitrary attribute lookups so
    code that does ``provider.model`` etc. still works."""
    from arc.packages.arc_sim2l_agents.ideator_constraint_aware import (
        _VocabWrappedProvider,
    )

    class _Inner:
        model = "test-model-7"
        base_url = "http://example.invalid"

        async def complete(self, prompt, **kw): return prompt
        async def complete_structured(self, prompt, schema, **kw):
            return schema

    wrapped = _VocabWrappedProvider(_Inner(), "VOCAB")
    assert wrapped.model == "test-model-7"
    assert wrapped.base_url == "http://example.invalid"


def test_vocab_wrapper_prepends_to_complete_calls():
    """Both ``complete`` and ``complete_structured`` get the prepend."""
    from arc.packages.arc_sim2l_agents.ideator_constraint_aware import (
        _VocabWrappedProvider,
    )

    captured: list[str] = []

    class _Inner:
        async def complete(self, prompt, **kw):
            captured.append(prompt)
            return ""
        async def complete_structured(self, prompt, schema, **kw):
            captured.append(prompt)
            return schema

    wrapped = _VocabWrappedProvider(_Inner(), "VOCAB_BLOCK")
    asyncio.run(wrapped.complete("user prompt 1"))
    assert captured[0].startswith("VOCAB_BLOCK")
    assert "user prompt 1" in captured[0]
