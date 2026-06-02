"""Strengthened default hypothesis generation (design/todo.md item 2).

Covers the hypothesis-space helpers + the ideator wiring:
  * deterministic multi-candidate stub mode,
  * scoring/ranking + near-duplicate rejection,
  * one primary ResearchProposal is always returned,
  * the candidate pool + selection rationale are recorded on memory,
  * a provider that returns several candidates is ranked, not blindly used.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.packages.arc_sim2l_agents.hypothesis import (
    HypothesisCandidates,
    rank_candidates,
    score_candidate,
    select,
    stub_candidates,
)
from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent
from arc.schemas.research import ResearchGoal, ResearchProposal

pytestmark = pytest.mark.chat


def _goal():
    return ResearchGoal(goal="maximize the band gap of silicon", target={"band_gap": 1.1})


def _ctx(provider=None, memory=None):
    base = dict(memory or {})
    if provider is not None:
        base["provider"] = provider
    return SimpleNamespace(memory=base, config={})


# ── helpers ─────────────────────────────────────────────────────────────


def test_stub_candidates_are_multiple_and_deterministic():
    a = stub_candidates(_goal())
    b = stub_candidates(_goal())
    assert len(a) >= 3
    # Fully reproducible — same hypotheses in the same order.
    assert [p.hypothesis for p in a] == [p.hypothesis for p in b]


def test_scoring_penalises_generic_stub_phrasing():
    goal = _goal()
    generic = ResearchProposal(
        hypothesis="A simulation workflow can test: foo",
        objective="foo", variables=["x"], methodology="m",
        expected_outcomes="o", evaluation_metrics=["band_gap"],
    )
    specific = ResearchProposal(
        hypothesis="Increasing layer thickness increases the band_gap toward target",
        objective="foo",
        variables=["thickness", "band_gap"],
        methodology="DFT sweep over thickness",
        expected_outcomes="o", evaluation_metrics=["band_gap"], risk_level="low",
    )
    g = score_candidate(generic, goal, [])
    s = score_candidate(specific, goal, [])
    assert s["total"] > g["total"]
    assert g["specificity"] < s["specificity"]


def test_rank_marks_near_duplicates_rejected():
    goal = _goal()
    p1 = ResearchProposal(
        hypothesis="Increasing thickness raises the band gap of silicon",
        objective="o", variables=["thickness"], methodology="m",
        expected_outcomes="e", evaluation_metrics=["band_gap"],
    )
    dup = ResearchProposal(
        hypothesis="Increasing thickness raises the band gap of silicon",
        objective="o2", variables=["thickness"], methodology="m2",
        expected_outcomes="e2", evaluation_metrics=["band_gap"],
    )
    ranked = rank_candidates([p1, dup], goal)
    rejected = [e for e in ranked if e["rejected"]]
    assert len(rejected) == 1
    assert "duplicate" in rejected[0]["reason"]


def test_select_returns_primary_and_diverse_alternate():
    result = select(stub_candidates(_goal()), _goal())
    assert isinstance(result["primary"], ResearchProposal)
    # Stub candidates are deliberately diverse → an alternate exists.
    assert isinstance(result["alternate"], ResearchProposal)
    assert result["primary"].hypothesis != result["alternate"].hypothesis
    assert "Selected best of" in result["rationale"]


def test_select_handles_single_candidate():
    one = [stub_candidates(_goal())[0]]
    result = select(one, _goal())
    assert isinstance(result["primary"], ResearchProposal)
    assert result["alternate"] is None


# ── ideator wiring ──────────────────────────────────────────────────────


def test_ideator_stub_mode_records_candidates_and_returns_proposal():
    agent = IdeatorAgent(context=_ctx())
    proposal = asyncio.run(agent.run(_goal()))
    assert isinstance(proposal, ResearchProposal)
    candidates = agent.context.memory.get("ideator_candidates")
    assert candidates and len(candidates) >= 3
    assert agent.context.memory.get("ideator_selection_rationale")
    # The returned proposal is the genuinely highest-scored non-rejected
    # candidate — not a pinned baseline. Prove it by recomputing the max
    # score over the recorded pool independently of presentation order.
    best = max(
        (c for c in candidates if not c["rejected"]),
        key=lambda c: c["scores"]["total"],
    )
    assert proposal.hypothesis == best["hypothesis"]
    # And it must NOT be the generic stub baseline, which the scorer penalises.
    assert not proposal.hypothesis.startswith("A simulation workflow can test:")


class _MultiProvider:
    """Provider that fills the HypothesisCandidates wrapper schema."""

    def __init__(self):
        self.prompts = []

    async def complete_structured(self, prompt, schema, **kw):
        self.prompts.append((prompt, schema))
        if schema is HypothesisCandidates:
            return HypothesisCandidates(candidates=[
                ResearchProposal(
                    hypothesis="Weak generic: A simulation workflow can test: x",
                    objective="x", variables=[], methodology="",
                    expected_outcomes="", evaluation_metrics=[], risk_level="high",
                ),
                ResearchProposal(
                    hypothesis="Increasing thickness raises band_gap toward 1.1 eV target",
                    objective="optimise thickness", variables=["thickness", "band_gap"],
                    methodology="DFT sweep over thickness with structured grid",
                    expected_outcomes="response surface", evaluation_metrics=["band_gap"],
                    risk_level="low",
                ),
            ])
        # Single-proposal fallback path.
        return schema(
            hypothesis="single", objective="o", variables=["band_gap"],
            methodology="m", expected_outcomes="e", evaluation_metrics=["band_gap"],
        )


def test_ideator_provider_multi_candidate_is_ranked():
    provider = _MultiProvider()
    agent = IdeatorAgent(context=_ctx(provider=provider))
    proposal = asyncio.run(agent.run(_goal()))
    # The strong, low-risk, target-covering candidate must win over the weak one.
    assert "thickness" in proposal.hypothesis
    # The wrapper schema was requested (multi-candidate path taken).
    assert any(s is HypothesisCandidates for _, s in provider.prompts)
    ranked = agent.context.memory["ideator_candidates"]
    assert ranked[0]["hypothesis"] == proposal.hypothesis


def test_ideator_provider_prompt_includes_retry_context():
    provider = _MultiProvider()
    agent = IdeatorAgent(context=_ctx(provider=provider, memory={
        "retry_context": [
            {
                "reason": "schema_mismatch",
                "required_outputs": ["bandgap_ev"],
                "actual_outputs": {"result": 2.0},
                "artifact_name": "old_artifact",
                "review_summary": "missing requested output",
            },
        ],
    }))

    asyncio.run(agent.run(_goal()))

    prompt = provider.prompts[0][0]
    assert "Recent ARC retry context" in prompt
    assert "required_outputs=['bandgap_ev']" in prompt
    assert "actual_outputs=['result']" in prompt
    assert "missing requested output" in prompt
