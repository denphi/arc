"""Bayesian-optimization optimizer agent.

Pins the run-contract compatibility with the GA so ``/optimize`` and the
``optimizer`` strategy slot can swap between them without caller changes.
Most assertions exercise the random-sample fallback path because we don't
require ``scikit-optimize`` in the test environment — the BO-with-skopt
path is exercised by a separate ``importorskip`` test below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.packages.arc_sim2l_agents.optimizer_bayes import (
    BayesOptOptimizerAgent,
    _bounds_from_schema,
)


pytestmark = pytest.mark.chat


# ── helpers ─────────────────────────────────────────────────────────────


def _artifact(schema=None):
    return SimpleNamespace(
        artifact_id="bo-test-1",
        name="bandgap",
        path="/tmp/does-not-exist",
        metadata={"sim2l_inputs": schema or {
            "thickness": {"default": 5.0, "min": 1.0, "max": 10.0},
            "temperature": {"default": 300.0, "min": 100.0, "max": 600.0},
        }},
    )


def _adapter(outputs_fn):
    """Stub adapter whose ``.run(artifact, inputs)`` returns whatever
    ``outputs_fn(inputs)`` produces wrapped as a result-like object."""
    async def _run(artifact, inputs):
        return SimpleNamespace(outputs=outputs_fn(inputs))
    return SimpleNamespace(run=_run)


def _agent(adapter):
    ctx = SimpleNamespace(memory={"adapter": adapter, "schema_registry": {}})
    return BayesOptOptimizerAgent(context=ctx)


# ── _bounds_from_schema ─────────────────────────────────────────────────


def test_bounds_use_min_max_when_provided():
    bounds = _bounds_from_schema({"x": {"default": 5.0, "min": 1.0, "max": 10.0}})
    assert bounds["x"] == (1.0, 10.0)


def test_bounds_fall_back_to_default_scaled_when_min_max_missing():
    bounds = _bounds_from_schema({"x": {"default": 4.0}})
    assert bounds["x"] == pytest.approx((0.4, 12.0))


def test_bounds_handle_zero_default():
    bounds = _bounds_from_schema({"x": {"default": 0.0}})
    assert bounds["x"] == (-1.0, 1.0)


def test_bounds_avoid_degenerate_dimension():
    bounds = _bounds_from_schema({"x": {"default": 5.0, "min": 5.0, "max": 5.0}})
    lo, hi = bounds["x"]
    assert hi > lo


# ── Contract: same shape as GA ──────────────────────────────────────────


def test_run_requires_adapter_in_context():
    art = _artifact()
    bad_ctx = SimpleNamespace(memory={})  # no adapter
    agent = BayesOptOptimizerAgent(context=bad_ctx)
    with pytest.raises(RuntimeError, match="adapter"):
        asyncio.run(agent.run(art, {"bandgap_ev": 1.1}))


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
    """Fallback path produces the same keys as the GA agent."""
    art = _artifact()

    def _outputs(inputs):
        # Closer to thickness=5, temperature=300 → lower error
        return {"bandgap_ev": 1.0 + 0.01 * abs(inputs["thickness"] - 5)}

    agent = _agent(_adapter(_outputs))
    result = asyncio.run(agent.run(
        art,
        target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=4,  # budget=8
    ))
    assert set(result.keys()) >= {
        "best_inputs", "best_outputs", "best_fitness",
        "generations_run", "converged", "history",
    }
    assert result["generations_run"] >= 1
    assert isinstance(result["history"], list)


def test_run_records_history_with_per_step_inputs_and_outputs():
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=1, pop_size=3,
    ))
    for entry in result["history"]:
        assert "best_inputs" in entry
        assert "best_outputs" in entry
        assert "best_fitness" in entry


def test_run_calls_on_generation_callback():
    """``on_generation`` fires once per evaluation, matching the GA agent.

    Use a non-zero residual so BO never converges early — that way we get
    the full budget worth of callback invocations.
    """
    art = _artifact()
    calls = []

    async def _cb(step, point, out, fit):
        calls.append((step, dict(point), dict(out)))

    # Output is far from target (target=1.0, output=2.0) so fitness > threshold.
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 2.0}))
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=2,  # budget=4
        convergence_threshold=0.0,      # disable early stop
        on_generation=_cb,
    ))
    assert len(calls) == 4
    for step, *_ in calls:
        assert isinstance(step, int)


def test_run_converges_when_target_within_threshold():
    """When the evaluator returns the target verbatim, BO stops early."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=5, pop_size=4,
        convergence_threshold=0.01,
    ))
    assert result["converged"] is True
    assert result["best_fitness"] <= 0.01


def test_run_accepts_and_ignores_elite_frac():
    """GA-specific kwargs must not break the BO agent."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    # Should not raise — elite_frac is accepted then ignored.
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=1, pop_size=1, elite_frac=0.99,
    ))


# ── Strategy integration ────────────────────────────────────────────────


def test_strategy_resolver_returns_bayesopt_class():
    """``resolve_role`` with the bayesopt override loads this agent."""
    from arc.core.strategies import resolve_role
    cls = resolve_role("optimizer", overrides={"optimizer": "bayesopt"})
    assert cls.__name__ == "BayesOptOptimizerAgent"


def test_strategy_default_is_still_genetic():
    from arc.core.strategies import resolve_role
    cls = resolve_role("optimizer")
    assert cls.__name__ == "GeneticOptimizerAgent"


# ── skopt-backed path (optional dependency) ─────────────────────────────


def test_run_uses_skopt_when_available():
    """If scikit-optimize is installed, the skopt branch must execute."""
    pytest.importorskip("skopt")

    art = _artifact()
    seen_points = []

    def _outputs(inputs):
        seen_points.append(dict(inputs))
        # Linear response; skopt's GP should converge toward thickness=10
        return {"bandgap_ev": inputs["thickness"] / 10.0}

    agent = _agent(_adapter(_outputs))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=3,  # budget=6
    ))
    assert result["generations_run"] >= 1
    assert len(seen_points) >= 1


@pytest.mark.asyncio
async def test_skopt_path_runs_inside_existing_event_loop():
    """Regression for the ``run_until_complete`` crash: the skopt backend
    must run when ``run()`` is awaited from inside an already-running event
    loop (i.e. the normal research-loop path). Previously the BO loop
    called ``asyncio.get_event_loop().run_until_complete`` here, which
    raised ``RuntimeError: This event loop is already running``."""
    pytest.importorskip("skopt")

    art = _artifact()

    def _outputs(inputs):
        return {"bandgap_ev": inputs["thickness"] / 10.0}

    agent = _agent(_adapter(_outputs))
    # No asyncio.run() — we're already in a running loop (pytest-asyncio).
    result = await agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=3,
    )
    assert result["generations_run"] >= 1
    assert "best_inputs" in result
