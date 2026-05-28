"""ActiveLearningPlannerAgent — maximin-distance acquisition planner.

Drop-in replacement for ``PlannerAgent`` that emits a sweep biased
toward unexplored regions of the parameter space. Contract must match
the default planner so the resolver can swap one for the other.

Three layers of coverage:

  * Pure helpers — Halton sampling determinism, normalisation,
    minimum-distance scoring.
  * Plan synthesis — ``_apply_active_learning_sweep`` rewrites the
    sweep over the LLM-derived parameter set without overwriting
    non-numeric ones.
  * Agent — cold start delegates, warm path produces an unexplored
    sweep, contract compatibility with the default ``PlannerAgent``.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from arc.schemas.research import ResearchProposal


pytestmark = pytest.mark.chat


def _proposal():
    return ResearchProposal(
        hypothesis="x",
        objective="design a silicon nanostructure",
        variables=["thickness_nm", "temperature"],
        methodology="DFT",
        expected_outcomes="x",
        evaluation_metrics=["bandgap_ev"],
    )


def _context(memory=None):
    return SimpleNamespace(memory=dict(memory or {}))


def _al_module():
    """File-path load of the active-learning planner module.

    arc-sim2l is canonically reachable via the symlink, but we mirror
    the resolver's import shape for parity with the other planner tests.
    """
    import importlib.util
    import sys
    from pathlib import Path

    mod_name = "_test_al_planner_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = (
        Path(__file__).resolve().parent.parent
        / "arc" / "packages" / "arc-sim2l"
        / "agents" / "planner_active_learning.py"
    )
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ── Pure helpers ──────────────────────────────────────────────────────


def test_halton_sequence_is_deterministic():
    mod = _al_module()
    assert mod._halton(1, 2) == 0.5
    assert mod._halton(2, 2) == 0.25
    assert mod._halton(3, 2) == 0.75


def test_halton_points_have_correct_shape():
    mod = _al_module()
    points = mod._halton_points(8, 3)
    assert len(points) == 8
    assert all(len(p) == 3 for p in points)
    for p in points:
        assert all(0.0 <= v < 1.0 for v in p)


def test_normalise_clamps_to_unit_interval():
    mod = _al_module()
    assert mod._normalise(5.0, 0.0, 10.0) == 0.5
    assert mod._normalise(-1.0, 0.0, 10.0) == 0.0    # clamped low
    assert mod._normalise(15.0, 0.0, 10.0) == 1.0    # clamped high
    assert mod._normalise(5.0, 10.0, 10.0) == 0.0    # degenerate range


def test_min_distance_returns_infinity_when_empty():
    mod = _al_module()
    assert mod._min_distance([0.5, 0.5], []) == math.inf


def test_min_distance_finds_closest_neighbour():
    mod = _al_module()
    point = [0.5, 0.5]
    others = [
        [0.0, 0.0],   # distance = sqrt(0.5)
        [0.6, 0.5],   # distance = 0.1 ← closest
        [1.0, 1.0],   # distance = sqrt(0.5)
    ]
    assert mod._min_distance(point, others) == pytest.approx(0.1)


# ── Sweep rewrite ─────────────────────────────────────────────────────


def _baseline_plan():
    """Construct a fresh fallback plan we can use as the starting point
    for sweep-rewrite tests, just like the agent does internally."""
    from arc.packages.arc_sim2l_agents.planner import _fallback_plan
    return _fallback_plan(_proposal())


def test_apply_sweep_rewrites_sweep_with_history():
    mod = _al_module()
    plan = _baseline_plan()
    history = [
        {"inputs": {name: plan.parameter_constraints[name]["min"]
                    for name in plan.parameters}},
    ]
    rewritten = mod._apply_active_learning_sweep(plan, history)
    assert rewritten, "expected at least one parameter rewritten"
    # Sweep has the configured size and one column per parameter.
    sweep_lengths = {len(v) for v in plan.parameter_sweep.values()}
    assert sweep_lengths == {5}


def test_apply_sweep_picks_points_far_from_history():
    """The top sweep entry must be farther from history than the
    bottom one — that's the whole point of the acquisition function."""
    mod = _al_module()
    plan = _baseline_plan()
    # Pin one corner of the unit hypercube as the only history point.
    history_inputs = {
        name: plan.parameter_constraints[name]["min"]
        for name in plan.parameters
    }
    history = [{"inputs": history_inputs}]
    mod._apply_active_learning_sweep(plan, history)

    # Project each sweep column back into the unit cube; compare the
    # first (best) column's distance to the origin against the last.
    bounds = mod._extract_bounds(plan)
    names = list(bounds.keys())
    first = [
        (plan.parameter_sweep[n][0] - bounds[n][0]) /
        (bounds[n][1] - bounds[n][0])
        for n in names
    ]
    last = [
        (plan.parameter_sweep[n][-1] - bounds[n][0]) /
        (bounds[n][1] - bounds[n][0])
        for n in names
    ]
    origin = [0.0] * len(names)
    assert mod._min_distance(first, [origin]) >= mod._min_distance(last, [origin])


def test_apply_sweep_returns_empty_when_no_numeric_parameters():
    mod = _al_module()
    plan = _baseline_plan()
    # Wipe all numeric parameters.
    plan.parameters = {"label": "string-not-a-number"}
    plan.parameter_constraints = {}
    assert mod._apply_active_learning_sweep(plan, []) == []


def test_apply_sweep_is_deterministic_for_same_history():
    """Halton + maximin → same history yields same sweep, every time."""
    mod = _al_module()
    plan_a = _baseline_plan()
    plan_b = _baseline_plan()
    history = [{"inputs": {name: plan_a.parameter_constraints[name]["min"]
                            for name in plan_a.parameters}}]
    mod._apply_active_learning_sweep(plan_a, history)
    mod._apply_active_learning_sweep(plan_b, history)
    assert plan_a.parameter_sweep == plan_b.parameter_sweep


def test_apply_sweep_first_column_becomes_nominal():
    """``plan.parameters[name]`` after the sweep == sweep[name][0]."""
    mod = _al_module()
    plan = _baseline_plan()
    history = [{"inputs": {name: plan.parameter_constraints[name]["min"]
                            for name in plan.parameters}}]
    mod._apply_active_learning_sweep(plan, history)
    for name in plan.parameter_sweep:
        assert plan.parameters[name] == plan.parameter_sweep[name][0]


# ── Strategy resolver wiring ──────────────────────────────────────────


def test_strategy_resolver_returns_active_learning_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("planner", overrides={"planner": "active_learning"})
    assert cls.__name__ == "ActiveLearningPlannerAgent"


def test_planner_default_is_still_llm_planner():
    from arc.core.strategies import resolve_role
    assert resolve_role("planner").__name__ == "PlannerAgent"


# ── Agent contract ────────────────────────────────────────────────────


def _resolve_agent():
    from arc.core.strategies import resolve_role
    return resolve_role("planner", overrides={"planner": "active_learning"})


def test_cold_start_delegates_to_default_planner():
    """No history → return a plan that matches what the default planner
    would have produced (same parameter names)."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _context()
    default_plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    al_plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))
    assert set(al_plan.parameters.keys()) == set(default_plan.parameters.keys())


def test_warm_path_rewrites_sweep_around_history():
    history = [
        {"inputs": {"thickness_nm": 5.0, "temperature": 300.0,
                    "effective_mass": 0.25, "doping_concentration": 1e16,
                    "strain": 0.0}},
        {"inputs": {"thickness_nm": 5.5, "temperature": 305.0,
                    "effective_mass": 0.26, "doping_concentration": 1e16,
                    "strain": 0.0}},
    ]
    ctx = _context({"run_history": history})
    plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))

    # Every numeric parameter has a sweep with the configured length.
    numeric_params = [
        n for n, v in plan.parameters.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    for name in numeric_params:
        assert name in plan.parameter_sweep
        assert len(plan.parameter_sweep[name]) >= 2


def test_warm_path_attribution_in_experimental_design():
    """The plan records that the AL planner rewrote the sweep so users
    see *why* the parameters look the way they do."""
    history = [{"inputs": {"thickness_nm": 5.0, "temperature": 300.0,
                            "effective_mass": 0.25, "doping_concentration": 1e16,
                            "strain": 0.0}}]
    ctx = _context({"run_history": history})
    plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))

    tags = " ".join(plan.experimental_design)
    assert "active_learning_planner" in tags
    assert "maximin-distance" in tags
    assert "1 prior run" in tags


def test_warm_path_handles_malformed_history_entries():
    """Garbage entries in history shouldn't crash the planner — they
    just don't contribute to the maximin score."""
    ctx = _context({"run_history": [
        "not a dict",
        {"inputs": "also not a dict"},
        {"inputs": {"thickness_nm": "not a number"}},  # accepted, normalised to 0.5
        {"inputs": {"thickness_nm": 5.0, "temperature": 300.0}},
    ]})
    plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))
    # At minimum the plan still has numeric parameters with constraints.
    assert plan.parameters
    assert plan.parameter_constraints


def test_warm_path_preserves_non_numeric_parameters():
    """If the LLM declared a string parameter, the AL sweep must not
    drop or rewrite it."""
    from arc.packages.arc_sim2l_agents.planner_active_learning import (
        _apply_active_learning_sweep,
    )
    plan = _baseline_plan()
    plan.parameters["material_name"] = "silicon"  # non-numeric

    history = [{"inputs": {name: plan.parameter_constraints[name]["min"]
                            for name in plan.parameters
                            if name in plan.parameter_constraints}}]
    _apply_active_learning_sweep(plan, history)
    # The non-numeric parameter survives the rewrite.
    assert plan.parameters["material_name"] == "silicon"
    # And isn't accidentally added to the sweep.
    assert "material_name" not in plan.parameter_sweep


def test_run_returns_experiment_plan_shape():
    """Sanity: the returned object passes Pydantic validation as an
    ExperimentPlan (same as the default planner)."""
    from arc.schemas.research import ExperimentPlan

    ctx = _context({"run_history": [{"inputs": {"thickness_nm": 5.0}}]})
    plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))
    assert isinstance(plan, ExperimentPlan)


def test_no_op_when_no_history_after_default_planner_returns_plan_unchanged():
    """Cold start: AL planner returns *exactly* what the default
    produced — no rewrite, no attribution line."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _context()
    default_plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    al_plan = asyncio.run(_resolve_agent()(context=ctx).run(_proposal()))

    # The attribution line is only added when a rewrite happens.
    al_tags = " ".join(al_plan.experimental_design)
    default_tags = " ".join(default_plan.experimental_design)
    assert "active_learning_planner" not in al_tags
    assert al_tags == default_tags
