"""Bayesian-optimization optimizer agent.

Drop-in alternative to :class:`GeneticOptimizerAgent`. Same constructor
signature, same ``run(artifact, target, ...)`` contract, same return shape
— so the ``/optimize`` slash command and the ``optimizer`` strategy slot
in :mod:`arc.core.strategies` can swap one for the other with no caller
changes.

BO is the right choice when each simulation call is expensive: the GP
surrogate builds a model of the response surface and proposes the next
point that maximises an acquisition criterion, instead of paying for a
whole GA population's worth of evaluations per "generation".

When ``scikit-optimize`` is installed we delegate to it (``gp_minimize``
with ``LCB``). Without it we fall back to a lightweight random-sample +
keep-the-best strategy so the agent still works in CI and on minimal
installs. The fallback is intentionally weaker than the GA — it's a
correctness floor, not a competitor.

Mapping from the unified ``/optimize`` signature:
    max_generations × pop_size  →  total evaluation budget
    pop_size                    →  initial random-sample seed (kept ≤ budget)
    elite_frac                  →  ignored (BO doesn't carry elites)
    convergence_threshold       →  early-stop when best fitness ≤ threshold
    on_generation               →  called after each post-seed evaluation
                                   with (step, best_inputs, best_outputs, fitness)
"""

from __future__ import annotations

import logging
import random
from typing import Any

from arc.contracts.agent import AgentContract
from arc.packages.arc_sim2l_agents.optimizer import _fitness  # reuse the GA's metric
from arc.runtime.key_matching import keys_match as _keys_match
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)


# ── Bounds extraction ────────────────────────────────────────────────────


def _bounds_from_schema(schema: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Build [(lo, hi)] per parameter using schema constraints when present.

    Mirrors :func:`optimizer._initial_population`'s heuristics so the BO
    search space is the same as what the GA would have sampled — keeps
    benchmark comparisons honest.
    """
    bounds: dict[str, tuple[float, float]] = {}
    for name, field in schema.items():
        default = field.get("default", 1.0) if isinstance(field, dict) else 1.0
        if isinstance(field, dict) and "min" in field and "max" in field:
            lo, hi = float(field["min"]), float(field["max"])
        elif default == 0:
            lo, hi = -1.0, 1.0
        else:
            lo = default * 0.1
            hi = default * 3.0
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            hi = lo + 1e-6  # GP needs a non-degenerate dimension
        bounds[name] = (lo, hi)
    return bounds


# ── Agent ───────────────────────────────────────────────────────────────


class BayesOptOptimizerAgent(AgentContract):
    """Bayesian-optimization optimizer over the artifact's input space."""

    name = "optimizer"
    description = (
        "Bayesian optimization (Gaussian-process + LCB) over the artifact's "
        "input schema. Drop-in alternative to the genetic optimizer; use it "
        "when each simulation call is expensive."
    )

    async def run(
        self,
        artifact: ArtifactRecord,
        target: dict[str, Any],
        max_generations: int = 10,
        pop_size: int = 8,
        elite_frac: float = 0.25,             # noqa: ARG002 — accepted for sig compat
        convergence_threshold: float = 0.0025,
        on_generation=None,
    ) -> dict[str, Any]:
        adapter = self.context.memory.get("adapter")
        if adapter is None:
            raise RuntimeError("BayesOptOptimizerAgent requires 'adapter' in context.memory")

        # Resolve schema the same way GeneticOptimizerAgent does so the two
        # behave identically when picking parameters.
        schema: dict = artifact.metadata.get("sim2l_inputs", {})
        if not schema:
            try:
                from arc.sim2l_schema import load_sim2l_schema
                schema, _ = load_sim2l_schema(artifact.path)
                if schema:
                    artifact.metadata["sim2l_inputs"] = schema
            except Exception:
                schema = {}
        if not schema:
            raise ValueError(
                "Artifact has no sim2l_inputs schema — cannot initialise BO search"
            )

        registry: dict = self.context.memory.get("schema_registry", {})
        schema_defaults = {
            k: (v.get("default", 1.0) if isinstance(v, dict) else 1.0)
            for k, v in schema.items()
        }
        bounds = _bounds_from_schema(schema)
        param_names = list(bounds.keys())

        # Try to reuse the in-process simulate() function (matches GA).
        _simulate_func = None
        try:
            from pathlib import Path
            from arc.runtime.sim2l_adapter import _import_workflow_func
            workflow_path = Path(artifact.path) / "workflow.py"
            if workflow_path.exists():
                _simulate_func = _import_workflow_func(artifact.path, artifact.artifact_id)
        except Exception:
            _simulate_func = None

        async def _evaluate(point: dict) -> dict:
            inputs = {**schema_defaults, **{k: v for k, v in point.items() if k in schema}}
            if _simulate_func is not None:
                try:
                    return _simulate_func(**inputs) or {}
                except Exception:
                    return {}
            try:
                result = await adapter.run(artifact, inputs)
                return result.outputs or {}
            except Exception:
                return {}

        # Clamp sizes: a user can pass 0 via /optimize, which would make
        # n_seed 0 (skopt's n_initial_points must be ≥1) and starve the loop.
        pop_size = max(1, int(pop_size))
        max_generations = max(1, int(max_generations))
        budget = max(1, max_generations * pop_size)
        n_seed = max(1, min(pop_size, budget))

        # ── Try scikit-optimize backend ─────────────────────────────────
        skopt_run = await _maybe_run_skopt(
            param_names, bounds, _evaluate, target, registry,
            budget=budget, n_seed=n_seed,
            convergence_threshold=convergence_threshold,
            on_step=on_generation,
        )
        if skopt_run is not None:
            return skopt_run

        # ── Fallback: random-sample + keep-best ─────────────────────────
        return await _random_sample_loop(
            param_names, bounds, _evaluate, target, registry,
            budget=budget, n_seed=n_seed,
            convergence_threshold=convergence_threshold,
            on_step=on_generation,
        )


# ── scikit-optimize backend ─────────────────────────────────────────────


async def _maybe_run_skopt(
    param_names, bounds, evaluate_async, target, registry,
    *, budget, n_seed, convergence_threshold, on_step,
):
    """Return a result dict when skopt is available, else ``None``.

    Runs as a coroutine inside the caller's event loop and ``await``\\s the
    async evaluator directly. ``skopt.Optimizer.ask()/tell()`` are plain
    synchronous calls, so they interleave cleanly with the awaits — we do
    *not* spin up a nested loop (the previous ``run_until_complete`` here
    raised ``RuntimeError: This event loop is already running`` whenever
    the optimizer was invoked from the async research loop, i.e. always).
    """
    try:
        from skopt import Optimizer  # type: ignore[import-not-found]
        from skopt.space import Real  # type: ignore[import-not-found]
    except ImportError:
        return None

    dimensions = [Real(bounds[n][0], bounds[n][1], name=n) for n in param_names]
    opt = Optimizer(
        dimensions,
        base_estimator="GP",
        acq_func="LCB",
        random_state=0,
        n_initial_points=n_seed,
    )

    best_inputs: dict = {}
    best_outputs: dict = {}
    best_fitness = float("inf")
    history: list[dict] = []
    step_index = 0
    converged = False

    for step_index in range(budget):
        x = opt.ask()
        point = {name: float(val) for name, val in zip(param_names, x)}
        out = await evaluate_async(point)
        fit = _fitness(out, target, registry)
        opt.tell(x, fit if fit != float("inf") else 1e18)  # skopt rejects inf

        if fit < best_fitness:
            best_fitness = fit
            best_inputs = dict(point)
            best_outputs = dict(out)

        history.append({
            "generation": step_index,
            "best_fitness": fit,
            "best_inputs": dict(point),
            "best_outputs": dict(out),
        })
        if on_step is not None:
            await on_step(step_index, point, out, fit)

        if best_fitness <= convergence_threshold:
            converged = True
            break

    return {
        "best_inputs": best_inputs,
        "best_outputs": best_outputs,
        "best_fitness": best_fitness,
        "generations_run": len(history),
        "converged": converged,
        "history": history,
    }


# ── Fallback: random sampling with keep-best ─────────────────────────────


async def _random_sample_loop(
    param_names, bounds, evaluate_async, target, registry,
    *, budget, n_seed, convergence_threshold, on_step,  # noqa: ARG001
):
    """Deterministic fallback for environments without scikit-optimize.

    Quasi-Sobol-ish: uses ``random.uniform`` with a fixed seed so two runs
    over the same artifact yield reproducible histories. Not competitive
    with real BO — it's the correctness floor.
    """
    rng = random.Random(0)

    best_inputs: dict = {}
    best_outputs: dict = {}
    best_fitness = float("inf")
    history: list[dict] = []
    converged = False

    for step_index in range(budget):
        point = {
            name: rng.uniform(bounds[name][0], bounds[name][1])
            for name in param_names
        }
        out = await evaluate_async(point)
        fit = _fitness(out, target, registry)

        if fit < best_fitness:
            best_fitness = fit
            best_inputs = dict(point)
            best_outputs = dict(out)

        history.append({
            "generation": step_index,
            "best_fitness": fit,
            "best_inputs": dict(point),
            "best_outputs": dict(out),
        })

        if on_step is not None:
            await on_step(step_index, point, out, fit)

        if best_fitness <= convergence_threshold:
            converged = True
            break

    return {
        "best_inputs": best_inputs,
        "best_outputs": best_outputs,
        "best_fitness": best_fitness,
        "generations_run": len(history),
        "converged": converged,
        "history": history,
    }
