"""Tests for catalog reuse fit-scoring (B) and curator capability output (A)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arc.contracts.agent import AgentContext  # noqa: E402
from arc.packages.arc_sim2l_agents import reuse  # noqa: E402
from arc.schemas.research import ResearchGoal  # noqa: E402


def _hit(name, *, description="", outputs=None, inputs=None, capability=None, sim_id=1):
    return {
        "id": sim_id,
        "name": name,
        "description": description,
        "input_schema": {k: {"type": "Number"} for k in (inputs or [])},
        "output_schema": {k: {"type": "Number"} for k in (outputs or [])},
        "metadata": {"capability": capability} if capability else {},
    }


@pytest.mark.asyncio
async def test_schema_overlap_dominates_ranking():
    goal = ResearchGoal(goal="predict band gap", domain="materials",
                        target={"band_gap_ev": 1.1})
    hits = [
        _hit("unrelated", description="computes lattice constant", outputs=["lattice"]),
        _hit("bandgap_sim", description="density functional band gap",
             outputs=["band_gap_ev"]),
    ]
    scored = await reuse.score_fit(goal, hits, provider=None)
    # The artifact that actually emits the target key must win.
    assert scored[0]["name"] == "bandgap_sim"
    assert scored[0]["fit"] > scored[1]["fit"]
    assert scored[0]["fit_parts"]["schema"] == 1.0


@pytest.mark.asyncio
async def test_loose_key_matching_ignores_underscores_and_case():
    goal = ResearchGoal(goal="x", target={"bandgap_eV": 1.0})
    hits = [_hit("s", outputs=["band_gap_ev"])]
    scored = await reuse.score_fit(goal, hits, provider=None)
    assert scored[0]["fit_parts"]["schema"] == 1.0


@pytest.mark.asyncio
async def test_semantic_only_when_no_target():
    goal = ResearchGoal(goal="thermal conductivity of silicon")
    hits = [
        _hit("a", capability={"summary": "computes thermal conductivity of crystals",
                              "domain_tags": ["thermal", "conductivity"]}),
        _hit("b", capability={"summary": "stock price forecasting model",
                              "domain_tags": ["finance"]}),
    ]
    scored = await reuse.score_fit(goal, hits, provider=None)
    assert scored[0]["name"] == "a"
    # No target → schema component is None, fit equals the semantic score.
    assert scored[0]["fit_parts"]["schema"] is None
    assert scored[0]["fit"] == scored[0]["fit_parts"]["semantic"]


@pytest.mark.asyncio
async def test_candidate_text_prefers_capability_summary():
    hit = _hit("s", description="terse", capability={"summary": "rich capability text"})
    assert "rich capability text" in reuse.candidate_text(hit)


@pytest.mark.asyncio
async def test_scoring_never_raises_on_bad_hit():
    goal = ResearchGoal(goal="x", target={"y": 1})
    scored = await reuse.score_fit(goal, [{"name": "broken"}], provider=None)
    assert len(scored) == 1
    assert "fit" in scored[0]


def test_reuse_threshold_reads_config():
    ctx = AgentContext(session_id="t", config={"ARC_REUSE_THRESHOLD": "0.8"})
    assert reuse.reuse_threshold(ctx) == 0.8
    assert reuse.reuse_threshold(AgentContext(session_id="t")) == reuse.DEFAULT_REUSE_THRESHOLD
