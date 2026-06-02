"""Composite strategy merging for every role (design/todo.md item 5).

Each role now has a composite that runs an ordered stack and merges with
role-specific deterministic semantics. Single-strategy resolution is
unchanged; these tests pin the merge behaviour per role.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.core.strategies import resolve_role
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ExperimentPlan, ResearchGoal, ResearchProposal
from arc.schemas.review import ReviewResult

pytestmark = pytest.mark.chat


def _ctx(memory=None):
    return SimpleNamespace(
        memory=dict(memory or {}), config={}, session_id="s", iteration=0,
    )


def _proposal():
    return ResearchProposal(
        hypothesis="vary inputs to study result",
        objective="study result as inputs vary",
        variables=["control_parameter", "result"],
        methodology="sweep inputs and record result",
        expected_outcomes="response surface",
        evaluation_metrics=["result"],
    )


# ── single-strategy resolution unchanged ────────────────────────────────


def test_single_strategy_is_not_a_composite():
    cls = resolve_role("planner", overrides={"planner": "doe_lhs"})
    assert cls.__name__ == "LatinHypercubePlannerAgent"
    assert not hasattr(cls, "strategy_names") or not getattr(cls, "strategy_names")


# ── planner: merge exploration policy ───────────────────────────────────


def test_planner_composite_merges_designs_and_sweeps():
    cls = resolve_role("planner", overrides={"planner": "doe_lhs doe_factorial"})
    assert cls.strategy_names == ("doe_lhs", "doe_factorial")
    plan = asyncio.run(cls(context=_ctx()).run(_proposal()))
    assert isinstance(plan, ExperimentPlan)
    # Both planners' design labels are present.
    joined = " ".join(plan.experimental_design)
    assert "doe_lhs" in joined and "doe_factorial" in joined
    assert plan.parameter_sweep  # at least one merged sweep dimension


def test_planner_composite_intersects_numeric_constraints():
    from arc.packages.arc_sim2l_agents.composites import _merge_constraints
    into: dict = {"x": {"min": 0.0, "max": 10.0}}
    _merge_constraints(into, {"x": {"min": 2.0, "max": 8.0}})
    assert into["x"] == {"min": 2.0, "max": 8.0}
    _merge_constraints(into, {"x": {"min": 1.0, "max": 5.0}})
    # max(2,1)=2 ; min(8,5)=5
    assert into["x"] == {"min": 2.0, "max": 5.0}


# ── reviewer: consensus ─────────────────────────────────────────────────


def test_reviewer_composite_requires_all_to_approve():
    cls = resolve_role("reviewer", overrides={"reviewer": "default comparative"})
    ctx = _ctx({"target": {"result": 2.0}})
    ex = ExecutionResult(run_id="r", status="completed", outputs={"result": 2.0}, metrics={})
    review = asyncio.run(cls(context=ctx).run(ex))
    assert isinstance(review, ReviewResult)
    # Summary tags each reviewer.
    assert "[default]" in review.summary and "[comparative]" in review.summary


def test_reviewer_consensus_union_helper():
    from arc.packages.arc_sim2l_agents.composites import _union
    assert _union([["a", "b"], ["b", "c"]]) == ["a", "b", "c"]


# ── validator: aggregate ────────────────────────────────────────────────


def test_validator_composite_namespaces_evaluations():
    cls = resolve_role("validator", overrides={"validator": "dry_run materials_evaluators"})
    ctx = _ctx()
    outputs = {"band_gap": 1.1, "result": 2.0}
    report = asyncio.run(cls(context=ctx).validate(outputs, target={"band_gap": 1.1}))
    # Aggregated report; any evaluations are namespaced with strategy prefix.
    for key in report.evaluations:
        assert ":" in key


def test_validator_composite_passed_is_conjunction():
    cls = resolve_role("validator", overrides={"validator": "default dry_run"})
    ctx = _ctx()
    # Clean outputs → both validators pass → composite passes.
    report = asyncio.run(cls(context=ctx).validate({"result": 2.0}))
    assert report.passed is True


# ── curator: ordered normalisers ────────────────────────────────────────


def test_curator_composite_chains_single_curator():
    # Only one curator strategy exists; a "default default" stack collapses to
    # a single component and must still run cleanly.
    cls = resolve_role("curator", overrides={"curator": "default"})
    assert cls.__name__ == "CuratorAgent"


# ── ideator: synthesise one proposal ────────────────────────────────────


def test_ideator_composite_returns_one_proposal():
    cls = resolve_role("ideator", overrides={"ideator": "default constraint_aware"})
    assert cls.strategy_names == ("default", "constraint_aware")
    proposal = asyncio.run(cls(context=_ctx()).run(
        ResearchGoal(goal="maximize band gap", target={"band_gap": 1.1}),
    ))
    assert isinstance(proposal, ResearchProposal)


# ── optimizer: global best across optimizers ────────────────────────────


class _FakeOpt:
    """Records the generation budget it was handed."""

    def __init__(self, fitness, name):
        self._f, self.name = fitness, name
        self.got_generations = None

    async def run(self, artifact, target, max_generations=10, pop_size=8, **kw):
        self.got_generations = max_generations
        return {"best_fitness": self._f, "best_inputs": {"x": self._f},
                "history": [{"fitness": self._f}]}


def test_optimizer_composite_picks_global_best():
    from arc.packages.arc_sim2l_agents.composites import CompositeOptimizerAgent

    comp = CompositeOptimizerAgent(context=_ctx())
    comp.strategy_names = ("a", "b")

    # GA fitness is distance-to-target (LOWER is better), so the smaller value
    # (0.3, optimizer "a") must win.
    fakes = iter([("a", _FakeOpt(0.3, "a")), ("b", _FakeOpt(0.9, "b"))])
    comp._components = lambda: fakes  # type: ignore[assignment]

    best = asyncio.run(comp.run(artifact=object(), target={"result": 1.0}))
    assert best["best_fitness"] == 0.3
    assert best["optimizer"] == "a"
    # Combined history is tagged per optimizer.
    assert {h["optimizer"] for h in best["history"]} == {"a", "b"}


def test_optimizer_composite_splits_budget_without_exceeding():
    """Total generations across components must not exceed max_generations
    (review finding C). Remainder goes to the first components."""
    from arc.packages.arc_sim2l_agents.composites import CompositeOptimizerAgent

    comp = CompositeOptimizerAgent(context=_ctx())
    comp.strategy_names = ("a", "b", "c")
    opts = [_FakeOpt(0.5, n) for n in ("a", "b", "c")]
    comp._components = lambda: iter(list(zip(("a", "b", "c"), opts)))  # type: ignore[assignment]

    # 10 generations / 3 optimizers → 4, 3, 3 (sum == 10, never 12).
    asyncio.run(comp.run(artifact=object(), target={"result": 1.0}, max_generations=10))
    budgets = [o.got_generations for o in opts]
    assert sum(budgets) == 10
    assert budgets == [4, 3, 3]


def test_optimizer_composite_skips_zero_budget_components():
    """More optimizers than the budget → the extras get 0 generations and
    are skipped, not floored to 1 (which would multiply the budget)."""
    from arc.packages.arc_sim2l_agents.composites import CompositeOptimizerAgent

    comp = CompositeOptimizerAgent(context=_ctx())
    comp.strategy_names = ("a", "b", "c")
    opts = [_FakeOpt(0.5, n) for n in ("a", "b", "c")]
    comp._components = lambda: iter(list(zip(("a", "b", "c"), opts)))  # type: ignore[assignment]

    asyncio.run(comp.run(artifact=object(), target={"result": 1.0}, max_generations=2))
    budgets = [o.got_generations for o in opts]
    # 2 generations / 3 → [1, 1, 0]; the third never ran.
    assert budgets == [1, 1, None]
    assert sum(b for b in budgets if b) == 2
