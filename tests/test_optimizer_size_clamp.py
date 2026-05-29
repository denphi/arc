"""Regression: optimizers must clamp caller-supplied sizes.

A user can reach the optimizers via ``/optimize <generations> <pop>`` and
type ``0`` for either. Before the clamp, ``pop_size=0`` produced an empty
population/candidate list and crashed on ``min(range(0), …)`` with
"min() arg is an empty sequence". Every optimizer must instead clamp to a
usable minimum and run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.chat


def _artifact():
    return SimpleNamespace(
        artifact_id="clamp-test",
        name="bandgap",
        path="/tmp/does-not-exist",
        metadata={"sim2l_inputs": {
            "thickness": {"default": 5.0, "min": 1.0, "max": 10.0},
        }},
    )


def _adapter():
    async def _run(artifact, inputs):
        return SimpleNamespace(outputs={"bandgap_ev": inputs.get("thickness", 1.0) / 10.0})
    return SimpleNamespace(run=_run)


def _ctx():
    return SimpleNamespace(memory={"adapter": _adapter(), "schema_registry": {}})


def _run_opt(cls, **kw):
    agent = cls(context=_ctx())
    return asyncio.run(agent.run(
        _artifact(), target={"bandgap_ev": 1.0},
        max_generations=0, pop_size=0, **kw,   # the dangerous inputs
    ))


def test_genetic_optimizer_clamps_zero_pop():
    from arc.packages.arc_sim2l_agents.optimizer import GeneticOptimizerAgent
    result = _run_opt(GeneticOptimizerAgent)
    assert "best_inputs" in result
    assert result["generations_run"] >= 1


def test_bayes_optimizer_clamps_zero_pop():
    from arc.packages.arc_sim2l_agents.optimizer_bayes import BayesOptOptimizerAgent
    result = _run_opt(BayesOptOptimizerAgent)
    assert "best_inputs" in result


def test_cmaes_optimizer_clamps_zero_pop():
    from arc.packages.arc_sim2l_agents.optimizer_cmaes import CMAESOptimizerAgent
    result = _run_opt(CMAESOptimizerAgent)
    assert "best_inputs" in result


def test_llm_optimizer_clamps_zero_pop():
    from arc.packages.arc_sim2l_agents.optimizer_llm import LLMGuidedOptimizerAgent
    # No provider in ctx → falls back to its non-LLM sampling, still must run.
    result = _run_opt(LLMGuidedOptimizerAgent)
    assert "best_inputs" in result
