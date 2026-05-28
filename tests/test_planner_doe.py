"""Design-of-Experiments planners — LHS, fractional factorial, Sobol.

All three match the ``PlannerAgent`` contract so the resolver can swap
them in for any optimisation-free coverage-driven workflow. Tests cover:

  * The three generators (LHS, factorial, Sobol) as pure functions —
    shape, coverage, determinism.
  * ``_apply_scheme`` rewriting ``plan.parameters`` + ``plan.parameter_sweep``.
  * Each agent class through ``resolve_role`` end-to-end.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.schemas.research import ExperimentPlan, ResearchProposal


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


def _baseline_plan():
    from arc.packages.arc_sim2l_agents.planner import _fallback_plan
    return _fallback_plan(_proposal())


# ── Latin hypercube ───────────────────────────────────────────────────


def test_lhs_shape():
    from arc.packages.arc_sim2l_agents.planner_doe import latin_hypercube
    points = latin_hypercube(8, 3)
    assert len(points) == 8
    assert all(len(p) == 3 for p in points)
    for p in points:
        assert all(0.0 < v < 1.0 for v in p)


def test_lhs_stratification_one_sample_per_bin():
    """Each dimension's samples land in distinct bins — the defining
    property of Latin hypercube sampling."""
    from arc.packages.arc_sim2l_agents.planner_doe import latin_hypercube
    points = latin_hypercube(10, 3)
    width = 1.0 / 10
    for d in range(3):
        bins = {int(p[d] / width) for p in points}
        assert len(bins) == 10  # every bin used exactly once


def test_lhs_deterministic_with_seed():
    from arc.packages.arc_sim2l_agents.planner_doe import latin_hypercube
    a = latin_hypercube(8, 3, seed=99)
    b = latin_hypercube(8, 3, seed=99)
    assert a == b


def test_lhs_handles_zero_dimensions():
    from arc.packages.arc_sim2l_agents.planner_doe import latin_hypercube
    assert latin_hypercube(8, 0) == []
    assert latin_hypercube(0, 3) == []


# ── Fractional factorial ──────────────────────────────────────────────


def test_factorial_returns_full_cube_below_seven_dims():
    """2-level full factorial: 2^n corners."""
    from arc.packages.arc_sim2l_agents.planner_doe import fractional_factorial
    for n_dims in (1, 2, 3, 4, 5, 6):
        corners = fractional_factorial(n_dims, include_centre=False)
        assert len(corners) == 2 ** n_dims
        # Every coordinate is 0 or 1.
        for c in corners:
            assert all(v in (0.0, 1.0) for v in c)


def test_factorial_includes_centre_point():
    from arc.packages.arc_sim2l_agents.planner_doe import fractional_factorial
    corners = fractional_factorial(3, include_centre=True)
    assert [0.5, 0.5, 0.5] in corners


def test_factorial_caps_high_dimensions():
    """Beyond 6 dims we use a fractional design; the result is bounded."""
    from arc.packages.arc_sim2l_agents.planner_doe import fractional_factorial
    corners = fractional_factorial(8, include_centre=False)
    # 2^8 = 256 corners; the fractional fallback caps it well below.
    assert len(corners) <= 64


# ── Sobol ──────────────────────────────────────────────────────────────


def test_sobol_shape():
    from arc.packages.arc_sim2l_agents.planner_doe import sobol_sequence
    points = sobol_sequence(16, 4)
    assert len(points) == 16
    assert all(len(p) == 4 for p in points)
    for p in points:
        assert all(0.0 <= v < 1.0 for v in p)


def test_sobol_deterministic():
    from arc.packages.arc_sim2l_agents.planner_doe import sobol_sequence
    a = sobol_sequence(16, 4)
    b = sobol_sequence(16, 4)
    assert a == b


def test_sobol_handles_more_dims_than_directions():
    """Beyond the 8 hand-coded direction sets we cycle — must not crash."""
    from arc.packages.arc_sim2l_agents.planner_doe import sobol_sequence
    points = sobol_sequence(4, 12)
    assert all(len(p) == 12 for p in points)


def test_sobol_better_uniformity_than_random_on_2d():
    """A rough discrepancy check: split [0,1]^2 into a 4×4 grid and
    confirm the Sobol sample touches more cells than a fixed-seed
    uniform sample for the same point count."""
    import random
    from arc.packages.arc_sim2l_agents.planner_doe import sobol_sequence

    n = 16
    sobol = sobol_sequence(n, 2)
    rng = random.Random(0)
    uniform = [[rng.random(), rng.random()] for _ in range(n)]

    def cells(points):
        return {(int(p[0] * 4), int(p[1] * 4)) for p in points}

    assert len(cells(sobol)) >= len(cells(uniform))


# ── _apply_scheme ─────────────────────────────────────────────────────


def test_apply_scheme_lhs_rewrites_sweep():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    rewritten = _apply_scheme(plan, scheme="lhs", sweep_size=6)
    assert rewritten
    # All numeric params got a 6-row sweep.
    for name in rewritten:
        assert name in plan.parameter_sweep
        assert len(plan.parameter_sweep[name]) == 6


def test_apply_scheme_factorial_uses_corner_count():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    rewritten = _apply_scheme(plan, scheme="factorial")
    assert rewritten
    n_dims = len(rewritten)
    expected = (2 ** n_dims) + 1  # corners + centre
    if n_dims > 6:
        expected = None  # fractional fallback; just assert > 1
    sweep_len = len(plan.parameter_sweep[rewritten[0]])
    if expected is not None:
        assert sweep_len == expected
    else:
        assert sweep_len > 1


def test_apply_scheme_sobol_size_matches_sweep_size():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    rewritten = _apply_scheme(plan, scheme="sobol", sweep_size=10)
    for name in rewritten:
        assert len(plan.parameter_sweep[name]) == 10


def test_apply_scheme_first_column_becomes_nominal():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    rewritten = _apply_scheme(plan, scheme="lhs", sweep_size=8)
    for name in rewritten:
        assert plan.parameters[name] == plan.parameter_sweep[name][0]


def test_apply_scheme_rejects_unknown_scheme():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    with pytest.raises(ValueError, match="Unknown DoE scheme"):
        _apply_scheme(plan, scheme="not_real")


def test_apply_scheme_returns_empty_when_no_numeric_params():
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    plan.parameters = {"label": "non-numeric"}
    plan.parameter_constraints = {}
    assert _apply_scheme(plan, scheme="lhs") == []


def test_apply_scheme_respects_parameter_bounds():
    """Every generated sweep value must fall within the constraint band."""
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    rewritten = _apply_scheme(plan, scheme="sobol", sweep_size=16)
    for name in rewritten:
        lo = plan.parameter_constraints[name]["min"]
        hi = plan.parameter_constraints[name]["max"]
        for v in plan.parameter_sweep[name]:
            assert lo <= v <= hi


def test_apply_scheme_preserves_non_numeric_parameters():
    """An LLM-declared string parameter must survive a DoE rewrite."""
    from arc.packages.arc_sim2l_agents.planner_doe import _apply_scheme
    plan = _baseline_plan()
    plan.parameters["material_name"] = "silicon"
    _apply_scheme(plan, scheme="lhs")
    assert plan.parameters["material_name"] == "silicon"
    assert "material_name" not in plan.parameter_sweep


# ── Strategy resolver wiring ──────────────────────────────────────────


def test_resolver_returns_lhs_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("planner", overrides={"planner": "doe_lhs"})
    assert cls.__name__ == "LatinHypercubePlannerAgent"


def test_resolver_returns_factorial_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("planner", overrides={"planner": "doe_factorial"})
    assert cls.__name__ == "FractionalFactorialPlannerAgent"


def test_resolver_returns_sobol_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("planner", overrides={"planner": "doe_sobol"})
    assert cls.__name__ == "SobolPlannerAgent"


# ── Agent contract end-to-end ─────────────────────────────────────────


def _resolve(name):
    from arc.core.strategies import resolve_role
    return resolve_role("planner", overrides={"planner": name})


def test_lhs_agent_produces_valid_experiment_plan():
    ctx = _context()
    plan = asyncio.run(_resolve("doe_lhs")(context=ctx).run(_proposal()))
    assert isinstance(plan, ExperimentPlan)
    # Numeric parameters got an LHS sweep.
    numeric = [
        n for n, v in plan.parameters.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    assert numeric
    for n in numeric:
        assert n in plan.parameter_sweep


def test_factorial_agent_attribution_appears_in_design():
    ctx = _context()
    plan = asyncio.run(_resolve("doe_factorial")(context=ctx).run(_proposal()))
    tags = " ".join(plan.experimental_design)
    assert "doe_factorial" in tags
    assert "Fractional factorial" in tags


def test_sobol_agent_attribution_appears_in_design():
    ctx = _context()
    plan = asyncio.run(_resolve("doe_sobol")(context=ctx).run(_proposal()))
    tags = " ".join(plan.experimental_design)
    assert "doe_sobol" in tags
    assert "Sobol" in tags


def test_all_doe_agents_share_numeric_param_set():
    """All three DoE planners pick the same parameters to sweep — they
    differ only in *how* they fill the cube, not in what cube they
    fill."""
    names_per_agent = {}
    for impl in ("doe_lhs", "doe_factorial", "doe_sobol"):
        plan = asyncio.run(_resolve(impl)(context=_context()).run(_proposal()))
        names_per_agent[impl] = set(plan.parameter_sweep.keys())
    assert names_per_agent["doe_lhs"] == names_per_agent["doe_factorial"]
    assert names_per_agent["doe_lhs"] == names_per_agent["doe_sobol"]


def test_doe_does_not_need_history():
    """Unlike active-learning, DoE works fine on the first iteration."""
    ctx = _context()  # no run_history
    plan = asyncio.run(_resolve("doe_lhs")(context=ctx).run(_proposal()))
    assert plan.parameter_sweep
