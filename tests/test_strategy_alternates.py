"""Tests for the alternate research-loop strategies.

Covers four moves landed in this PR:

  * ``MARSPlannerAgent`` matches the ``PlannerAgent`` contract so
    ``/strategy planner mars_planner`` is a drop-in swap.
  * ``ReflectiveReviewerAgent`` matches the ``ReviewerAgent`` contract.
  * A new ``searcher`` role with ``KeywordSearcherAgent`` (default) and
    ``EmbeddingSearcherAgent`` (re-ranks by cosine similarity).
  * ``IdeatorAgent`` delegates catalog/results lookup to whichever
    Searcher is configured, so search swaps are transparent.

Tests use lightweight stubs for catalog/results HTTP calls — no live
sim2l services required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arc.schemas.research import ResearchGoal, ResearchProposal, SearchResult


pytestmark = pytest.mark.chat


# ── MARS planner contract ──────────────────────────────────────────────


def _proposal():
    return ResearchProposal(
        hypothesis="silicon nanowires reach 1.1 eV bandgap at thickness 5 nm",
        objective="design a silicon nanowire with bandgap = 1.1 eV",
        variables=["thickness", "temperature"],
        methodology="quantum confinement model",
        expected_outcomes="thickness vs bandgap curve",
        evaluation_metrics=["bandgap_ev"],
    )


def _context(memory=None):
    return SimpleNamespace(memory=dict(memory or {}))


def test_mars_planner_cold_start_delegates_to_default():
    """No history → returns the same plan the default planner would."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent
    from arc.core.strategies import resolve_role
    MARSPlannerAgent = resolve_role("planner", overrides={"planner": "mars_planner"})

    ctx = _context()
    default_plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    mars_plan = asyncio.run(MARSPlannerAgent(context=ctx).run(_proposal()))
    # Same fields filled.
    assert set(mars_plan.parameters.keys()) == set(default_plan.parameters.keys())
    assert mars_plan.proposal.objective == default_plan.proposal.objective


def test_mars_planner_warm_path_biases_sweep_to_unexplored():
    """When history has entries, the sweep changes from the default."""
    from arc.core.strategies import resolve_role
    MARSPlannerAgent = resolve_role("planner", overrides={"planner": "mars_planner"})

    history = [
        {"inputs": {"thickness": 5.0, "temperature": 300.0}, "outputs": {}},
        {"inputs": {"thickness": 5.0, "temperature": 300.0}, "outputs": {}},
    ]
    ctx = _context({"run_history": history})
    plan = asyncio.run(MARSPlannerAgent(context=ctx).run(_proposal()))

    # The warm path replaces the sweep with cell midpoints.
    assert plan.parameter_sweep
    for name, values in plan.parameter_sweep.items():
        # Sweep should produce non-empty list of floats per parameter.
        assert len(values) >= 2
        for v in values:
            assert isinstance(v, float)
    # Tag is appended to experimental_design.
    assert any("mars_planner" in step for step in plan.experimental_design)


def test_mars_planner_can_be_picked_via_strategy_resolver():
    from arc.core.strategies import resolve_role
    cls = resolve_role("planner", overrides={"planner": "mars_planner"})
    assert cls.__name__ == "MARSPlannerAgent"


# ── Reflective reviewer contract ───────────────────────────────────────


def _execution(outputs=None, status="completed", metrics=None):
    from arc.schemas.execution import ExecutionResult
    return ExecutionResult(
        run_id="run-1",
        status=status,
        outputs=outputs or {"bandgap_ev": 1.5},
        logs=[],
        metrics=metrics or {"thickness": 5.0, "temperature": 300.0},
    )


def test_reflective_reviewer_returns_review_result():
    """Smoke: run the reviewer with no history → ReviewResult shape."""
    from arc.core.strategies import resolve_role
    ReflectiveReviewerAgent = resolve_role(
        "reviewer", overrides={"reviewer": "reflective"},
    )
    from arc.schemas.review import ReviewResult

    ctx = _context({"target": {"bandgap_ev": 1.1}})
    review = asyncio.run(ReflectiveReviewerAgent(context=ctx).run(_execution()))
    assert isinstance(review, ReviewResult)
    assert review.summary


def test_reflective_reviewer_flags_repeat_inputs():
    """When inputs match a prior run, weaknesses mention it."""
    from arc.core.strategies import resolve_role
    ReflectiveReviewerAgent = resolve_role(
        "reviewer", overrides={"reviewer": "reflective"},
    )

    history = [
        {"inputs": {"thickness": 5.0, "temperature": 300.0}, "outputs": {"bandgap_ev": 1.5}},
    ]
    ctx = _context({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(ReflectiveReviewerAgent(context=ctx).run(_execution()))
    assert any("identical" in w.lower() or "repeat" in w.lower()
               or "vary" in r.lower()
               for w in review.weaknesses for r in review.recommendations)


def test_reflective_reviewer_switches_to_explore_on_stagnation():
    """Three consecutive non-improving runs → strategy='explore'."""
    from arc.core.strategies import resolve_role
    ReflectiveReviewerAgent = resolve_role(
        "reviewer", overrides={"reviewer": "reflective"},
    )

    # 3 prior runs at the same (worse) fitness, current run also worse.
    history = [
        {"inputs": {"thickness": 1.0}, "outputs": {"bandgap_ev": 1.5}},
        {"inputs": {"thickness": 2.0}, "outputs": {"bandgap_ev": 1.5}},
        {"inputs": {"thickness": 3.0}, "outputs": {"bandgap_ev": 1.5}},
    ]
    ctx = _context({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(ReflectiveReviewerAgent(context=ctx).run(
        _execution(outputs={"bandgap_ev": 1.5}),
    ))
    assert review.strategy == "explore"
    assert review.approved is False


def test_reflective_reviewer_approves_when_target_within_threshold():
    """Hit target → approved + strategy='stop'."""
    from arc.core.strategies import resolve_role
    ReflectiveReviewerAgent = resolve_role(
        "reviewer", overrides={"reviewer": "reflective"},
    )

    ctx = _context({"target": {"bandgap_ev": 1.1}})
    review = asyncio.run(ReflectiveReviewerAgent(context=ctx).run(
        _execution(outputs={"bandgap_ev": 1.1}),  # exact match
    ))
    assert review.approved is True
    assert review.strategy == "stop"


def test_reflective_reviewer_can_be_picked_via_strategy_resolver():
    from arc.core.strategies import resolve_role
    cls = resolve_role("reviewer", overrides={"reviewer": "reflective"})
    assert cls.__name__ == "ReflectiveReviewerAgent"


# ── Searcher role ──────────────────────────────────────────────────────


@pytest.fixture
def stub_catalog():
    """Patch the requests.get used by the searcher to return canned hits."""
    hits = [
        {"id": 1, "name": "silicon_quantum_well",
         "description": "Quantum confined silicon nanowire bandgap calculator",
         "input_schema": {"thickness": {"default": 5.0}, "temperature": {"default": 300.0}},
         "output_schema": {"bandgap_ev": {}},
         "tags": ["semiconductor", "quantum"]},
        {"id": 2, "name": "iron_oxide_phase",
         "description": "Phase diagram for iron oxide",
         "input_schema": {"pressure": {}}, "output_schema": {"phase": {}},
         "tags": ["chemistry"]},
        {"id": 3, "name": "polymer_chain",
         "description": "Polymer chain length to glass transition",
         "input_schema": {"chain_length": {}}, "output_schema": {"tg": {}},
         "tags": ["polymer"]},
    ]

    class _Resp:
        status_code = 200
        def __init__(self, data): self._data = data
        def json(self): return self._data

    def _get(url, params=None, timeout=None, **kwargs):
        return _Resp(hits)

    def _post(url, json=None, timeout=None, **kwargs):
        return _Resp({"results": []})

    with patch("requests.get", _get), patch("requests.post", _post):
        yield hits


def test_keyword_searcher_returns_search_result(stub_catalog):
    from arc.packages.arc_sim2l_agents.searcher import KeywordSearcherAgent

    ctx = _context()
    goal = ResearchGoal(goal="silicon nanowire bandgap 1.1 eV", domain="materials")
    result = asyncio.run(KeywordSearcherAgent(context=ctx).search(goal))
    assert isinstance(result, SearchResult)
    assert len(result.catalog_hits) == 3
    # Default keyword search preserves catalog order.
    assert result.catalog_hits[0]["name"] == "silicon_quantum_well"


def test_embedding_searcher_reranks_by_similarity(stub_catalog):
    """The polymer hit shouldn't beat the silicon hit when the goal is about silicon."""
    from arc.packages.arc_sim2l_agents.searcher import EmbeddingSearcherAgent

    ctx = _context()  # no provider → falls back to TF cosine
    goal = ResearchGoal(
        goal="silicon nanowire quantum confined bandgap design",
        domain="materials",
    )
    result = asyncio.run(EmbeddingSearcherAgent(context=ctx).search(goal))
    assert isinstance(result, SearchResult)
    assert result.catalog_hits, "expected re-ranked hits"
    assert result.catalog_hits[0]["name"] == "silicon_quantum_well"


def test_embedding_searcher_uses_provider_embed_when_available(stub_catalog):
    """If provider exposes embed(text), the searcher prefers it over TF."""
    from arc.packages.arc_sim2l_agents.searcher import EmbeddingSearcherAgent

    seen_texts: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        seen_texts.append(text)
        # Vector encoding: 1.0 if "silicon" present else 0.1
        return [1.0 if "silicon" in text.lower() else 0.1, 0.5]

    provider = SimpleNamespace(embed=fake_embed)
    ctx = _context({"provider": provider})

    goal = ResearchGoal(goal="silicon nanowire bandgap", domain="materials")
    result = asyncio.run(EmbeddingSearcherAgent(context=ctx).search(goal))
    # Provider was consulted at least once for the goal.
    assert any("silicon nanowire" in t for t in seen_texts)
    assert result.catalog_hits[0]["name"] == "silicon_quantum_well"


def test_keyword_searcher_handles_empty_catalog():
    """When the catalog returns [] the searcher still returns a valid shape."""
    from arc.packages.arc_sim2l_agents.searcher import KeywordSearcherAgent

    class _Resp:
        status_code = 200
        def json(self): return []

    with patch("requests.get", lambda *a, **kw: _Resp()), \
         patch("requests.post", lambda *a, **kw: _Resp()):
        result = asyncio.run(KeywordSearcherAgent(context=_context()).search(
            ResearchGoal(goal="bandgap")
        ))
    assert result.catalog_hits == []
    assert result.prior_results == []


def test_searcher_default_is_keyword_searcher():
    from arc.core.strategies import resolve_role
    cls = resolve_role("searcher")
    assert cls.__name__ == "KeywordSearcherAgent"


def test_searcher_override_picks_embedding_searcher():
    from arc.core.strategies import resolve_role
    cls = resolve_role("searcher", overrides={"searcher": "embeddings"})
    assert cls.__name__ == "EmbeddingSearcherAgent"


# ── Ideator → Searcher integration ─────────────────────────────────────


def test_ideator_delegates_catalog_lookup_to_searcher_strategy(stub_catalog):
    """Without a provider, the ideator falls through to the stub
    proposal but still records the searcher's hits into context.memory."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    ctx = _context()
    proposal = asyncio.run(IdeatorAgent(context=ctx).run(
        ResearchGoal(goal="silicon nanowire bandgap", domain="materials"),
    ))
    assert proposal.objective == "silicon nanowire bandgap"
    assert ctx.memory["catalog_hits"]
    assert ctx.memory["catalog_hits"][0]["name"] == "silicon_quantum_well"


def test_ideator_honours_runtime_searcher_override(stub_catalog):
    """``/strategy searcher embeddings`` makes the ideator re-rank hits."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    ctx = _context({"strategy_overrides": {"searcher": "embeddings"}})
    asyncio.run(IdeatorAgent(context=ctx).run(
        ResearchGoal(goal="polymer chain glass transition", domain="materials"),
    ))
    # With the polymer-centric goal, embeddings should put polymer_chain on top.
    assert ctx.memory["catalog_hits"][0]["name"] == "polymer_chain"


def test_ideator_swallows_searcher_failure_and_continues():
    """If the searcher raises, the ideator must still produce a proposal."""
    from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    with patch("requests.get", _boom), patch("requests.post", _boom):
        ctx = _context()
        proposal = asyncio.run(IdeatorAgent(context=ctx).run(
            ResearchGoal(goal="some research goal"),
        ))
    assert proposal.hypothesis  # falls through to the stub proposal
