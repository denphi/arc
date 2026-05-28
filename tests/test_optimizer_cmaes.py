"""CMA-ES optimizer agent.

Pins the run-contract compatibility with the GA/BayesOpt so ``/optimize``
and recipes can swap between the three with no caller change. Most
assertions exercise the deterministic coordinate-descent fallback path
because we don't require ``pycma`` in CI; the ``cma`` branch is exercised
by a separate ``importorskip`` test below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.packages.arc_sim2l_agents.optimizer_cmaes import (
    CMAESOptimizerAgent,
    _bounds_and_seed,
)


pytestmark = pytest.mark.chat


# ── helpers ─────────────────────────────────────────────────────────────


def _artifact(schema=None):
    return SimpleNamespace(
        artifact_id="cmaes-test-1",
        name="bandgap",
        path="/tmp/does-not-exist",
        metadata={"sim2l_inputs": schema or {
            "thickness":   {"default": 5.0, "min": 1.0,   "max": 10.0},
            "temperature": {"default": 300.0, "min": 100.0, "max": 600.0},
        }},
    )


def _adapter(outputs_fn):
    async def _run(artifact, inputs):
        return SimpleNamespace(outputs=outputs_fn(inputs))
    return SimpleNamespace(run=_run)


def _agent(adapter):
    ctx = SimpleNamespace(memory={"adapter": adapter, "schema_registry": {}})
    return CMAESOptimizerAgent(context=ctx)


# ── _bounds_and_seed ────────────────────────────────────────────────────


def test_bounds_use_min_max_when_provided():
    bounds, mean, sigma, names = _bounds_and_seed(
        {"x": {"default": 5.0, "min": 1.0, "max": 10.0}}
    )
    assert names == ["x"]
    assert bounds == [(1.0, 10.0)]
    assert mean == [5.0]
    # Initial sigma = 30% of span.
    assert sigma[0] == pytest.approx(2.7)


def test_bounds_fall_back_to_default_scaled():
    bounds, mean, _sigma, names = _bounds_and_seed({"x": {"default": 4.0}})
    assert names == ["x"]
    assert bounds[0] == pytest.approx((0.4, 12.0))
    assert mean == [4.0]


def test_bounds_handle_zero_default():
    bounds, mean, _, _ = _bounds_and_seed({"x": {"default": 0.0}})
    assert bounds == [(-1.0, 1.0)]
    assert mean == [0.0]


def test_bounds_avoid_degenerate_dimension():
    bounds, _, _, _ = _bounds_and_seed({"x": {"default": 5.0, "min": 5.0, "max": 5.0}})
    lo, hi = bounds[0]
    assert hi > lo


# ── Contract: same surface as GA / BayesOpt ─────────────────────────────


def test_run_requires_adapter_in_context():
    art = _artifact()
    bad_ctx = SimpleNamespace(memory={})
    agent = CMAESOptimizerAgent(context=bad_ctx)
    with pytest.raises(RuntimeError, match="adapter"):
        asyncio.run(agent.run(art, {"bandgap_ev": 1.0}))


def test_run_raises_when_no_schema():
    art = SimpleNamespace(
        artifact_id="x",
        name="x",
        path="/tmp/nope",
        metadata={"sim2l_inputs": {}},
    )
    agent = _agent(_adapter(lambda inputs: {}))
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(agent.run(art, {}))


def test_run_returns_expected_result_shape_via_fallback():
    """Coordinate-descent fallback returns the same dict shape as GA/BO."""
    art = _artifact()

    def _outputs(inputs):
        # Closer to thickness=5 → lower error
        return {"bandgap_ev": 1.0 + 0.01 * abs(inputs["thickness"] - 5)}

    agent = _agent(_adapter(_outputs))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=4,
    ))
    assert set(result.keys()) >= {
        "best_inputs", "best_outputs", "best_fitness",
        "generations_run", "converged", "history",
    }
    assert result["generations_run"] >= 1
    assert isinstance(result["history"], list)


def test_run_accepts_and_ignores_elite_frac():
    """GA-specific kwargs must not break the CMA-ES agent."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.0}))
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=1, pop_size=2, elite_frac=0.99,
    ))


def test_run_calls_on_generation_callback():
    """The callback fires once per generation, matching GA/BO."""
    art = _artifact()
    calls: list[int] = []

    async def _cb(gen, point, out, fit):
        calls.append(gen)

    # Output is far from target so fallback does multiple generations.
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 2.0}))
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=3, pop_size=3,
        convergence_threshold=0.0,
        on_generation=_cb,
    ))
    assert calls  # at least one generation ran
    # Generations are 0-indexed and monotone.
    assert calls == sorted(calls)


def test_run_converges_when_evaluator_returns_target():
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=5, pop_size=4,
        convergence_threshold=0.01,
    ))
    assert result["converged"] is True
    assert result["best_fitness"] <= 0.01


def test_fallback_finds_correct_minimum_for_separable_quadratic():
    """Coordinate descent must converge to the optimum on a separable bowl.

    f(x, y) = (x - 5)^2 + (y - 300)^2 is the simplest case where
    coordinate descent works: optimal x doesn't depend on y. The
    fallback should locate (5, 300) within the probe resolution.
    """
    art = _artifact()

    def _outputs(inputs):
        x = inputs["thickness"]
        y = inputs["temperature"]
        score = (x - 5.0) ** 2 + (y - 300.0) ** 2
        return {"bandgap_ev": 1.0 + 0.001 * score}

    agent = _agent(_adapter(_outputs))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=3, pop_size=10,  # 10 probes per axis
        convergence_threshold=0.0,
    ))
    best = result["best_inputs"]
    assert abs(best["thickness"] - 5.0) <= 1.0
    assert abs(best["temperature"] - 300.0) <= 60.0


# ── Strategy + recipe integration ───────────────────────────────────────


def test_strategy_resolver_returns_cmaes_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("optimizer", overrides={"optimizer": "cmaes"})
    assert cls.__name__ == "CMAESOptimizerAgent"


def test_strategy_default_remains_genetic():
    from arc.core.strategies import resolve_role
    cls = resolve_role("optimizer")
    assert cls.__name__ == "GeneticOptimizerAgent"


def test_cmaes_continuous_recipe_is_valid():
    """The bundled cmaes recipe references only known roles + impls."""
    from arc.core.recipes import get_recipe, validate_recipe
    recipe = get_recipe("cmaes-continuous")
    assert recipe is not None
    assert recipe.strategies["optimizer"] == "cmaes"
    assert validate_recipe(recipe) == []


def test_cmaes_continuous_recipe_applies_through_resolver(monkeypatch):
    """End-to-end: /recipe apply cmaes-continuous → resolve_role yields CMA-ES."""
    import asyncio

    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.packages import resolve_role as pkg_resolve_role
    from tests.fakes import make_workflow

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = ChatState(workflow=make_workflow())
    asyncio.run(run(state, ["apply", "cmaes-continuous"]))

    cls = pkg_resolve_role("optimizer", state.workflow)
    assert cls.__name__ == "CMAESOptimizerAgent"


# ── pycma-backed path (optional dependency) ─────────────────────────────


def test_run_uses_pycma_when_available():
    """If pycma is installed, the cma branch must execute end-to-end."""
    pytest.importorskip("cma")

    art = _artifact()
    seen_points = []

    def _outputs(inputs):
        seen_points.append(dict(inputs))
        x = inputs["thickness"]
        y = inputs["temperature"]
        score = (x - 5.0) ** 2 + (y - 300.0) ** 2
        return {"bandgap_ev": 1.0 + 0.001 * score}

    agent = _agent(_adapter(_outputs))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=3, pop_size=6,
    ))
    assert result["generations_run"] >= 1
    assert len(seen_points) >= 1
