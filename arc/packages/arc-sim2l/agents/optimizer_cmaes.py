"""CMA-ES optimizer agent.

Third drop-in alternative for the ``optimizer`` role (alongside the
genetic algorithm and Bayesian optimization). Same constructor + same
``run(artifact, target, ...)`` contract, so the ``/optimize`` slash
command and the ``optimizer`` strategy slot can pick it up with no
caller change.

CMA-ES is the right choice when:

  * parameters are **continuous and correlated** (changing thickness
    affects which temperature is best) — its self-adapting covariance
    matrix learns the correlation that GA/random search ignores.
  * each evaluation is **moderately expensive but cheaper than BO** —
    CMA-ES asks for ~10-30 evaluations per generation, so it's faster
    than BO per *real-time* unit while still much more sample-efficient
    than GA.

Backends:

  1. ``cma`` (pycma) when installed. CMA-ES proper.
  2. **Diagonal coordinate descent** as a deterministic fallback. We
     intentionally pick a different baseline than the BayesOpt agent's
     random-sample fallback so the two alternatives don't collapse to
     the same behaviour without their primary library.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.packages.arc_sim2l_agents.optimizer import _fitness
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)


# ── Bounds + initial mean / sigma ───────────────────────────────────────


def _bounds_and_seed(schema: dict[str, Any]) -> tuple[
    list[tuple[float, float]],
    list[float],
    list[float],
    list[str],
]:
    """Build the per-parameter ``(bounds, mean, sigma, names)`` tuple.

    Mirrors :func:`optimizer_bayes._bounds_from_schema`'s heuristics so
    the three optimizers explore the same search space when given the
    same artifact.
    """
    bounds: list[tuple[float, float]] = []
    mean: list[float] = []
    sigma: list[float] = []
    names: list[str] = []
    for name, field in schema.items():
        if not isinstance(field, dict):
            field = {}
        default = field.get("default", 1.0)
        if "min" in field and "max" in field:
            lo, hi = float(field["min"]), float(field["max"])
        elif default == 0:
            lo, hi = -1.0, 1.0
        else:
            lo = default * 0.1
            hi = default * 3.0
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            hi = lo + 1e-6
        names.append(name)
        bounds.append((lo, hi))
        mean.append(float(default))
        # Initial step size: 30% of the bound span — CMA-ES rule of thumb.
        sigma.append(max(1e-6, 0.3 * (hi - lo)))
    return bounds, mean, sigma, names


# ── Agent ───────────────────────────────────────────────────────────────


class CMAESOptimizerAgent(AgentContract):
    """CMA-ES over the artifact's continuous input space."""

    name = "optimizer"
    description = (
        "CMA-ES (Covariance Matrix Adaptation Evolution Strategy) over the "
        "artifact's input schema. Best for continuous, correlated parameter "
        "spaces. Uses ``cma`` (pycma) when installed; otherwise falls back "
        "to a deterministic diagonal coordinate-descent baseline."
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
            raise RuntimeError("CMAESOptimizerAgent requires 'adapter' in context.memory")

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
                "Artifact has no sim2l_inputs schema — cannot initialise CMA-ES"
            )

        registry: dict = self.context.memory.get("schema_registry", {})
        schema_defaults = {
            k: (v.get("default", 1.0) if isinstance(v, dict) else 1.0)
            for k, v in schema.items()
        }
        bounds, mean, sigma, names = _bounds_and_seed(schema)

        # Try the in-process simulate() shortcut the GA + BO use.
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

        # Try pycma first.
        cma_run = await _maybe_run_pycma(
            names, bounds, mean, sigma, _evaluate, target, registry,
            max_generations=max_generations, pop_size=pop_size,
            convergence_threshold=convergence_threshold, on_gen=on_generation,
        )
        if cma_run is not None:
            return cma_run

        # Fallback: diagonal coordinate descent.
        return await _coordinate_descent(
            names, bounds, mean, _evaluate, target, registry,
            max_generations=max_generations, pop_size=pop_size,
            convergence_threshold=convergence_threshold, on_gen=on_generation,
        )


# ── pycma backend ───────────────────────────────────────────────────────


async def _maybe_run_pycma(
    names, bounds, mean, sigma, evaluate_async, target, registry,
    *, max_generations, pop_size, convergence_threshold, on_gen,
):
    """Return a result dict when ``cma`` is installed, else ``None``."""
    try:
        import cma  # type: ignore[import-not-found]
    except ImportError:
        return None

    lo = [b[0] for b in bounds]
    hi = [b[1] for b in bounds]

    # CMA-ES wants one initial sigma — use the smallest per-dim sigma so
    # the largest dim doesn't blow past its bounds in the first ask().
    init_sigma = min(sigma) if sigma else 0.3

    opts = {
        "bounds": [lo, hi],
        "popsize": pop_size,
        "maxiter": max_generations,
        "tolfun": convergence_threshold,
        "verbose": -9,
        "verb_disp": 0,
    }
    es = cma.CMAEvolutionStrategy(mean, init_sigma, opts)

    best_inputs: dict = {}
    best_outputs: dict = {}
    best_fitness = float("inf")
    history: list[dict] = []
    converged = False
    gen = 0

    while not es.stop() and gen < max_generations:
        xs = es.ask()
        fits: list[float] = []
        outputs_list: list[dict] = []
        for x in xs:
            point = {name: float(val) for name, val in zip(names, x)}
            out = await evaluate_async(point)
            fit = _fitness(out, target, registry)
            # CMA-ES does not accept inf — clamp.
            fits.append(fit if fit != float("inf") else 1e18)
            outputs_list.append(out)
        es.tell(xs, fits)

        # Best of this generation.
        idx = min(range(len(fits)), key=lambda i: fits[i])
        gen_best_fit = fits[idx]
        gen_best_pt = {name: float(val) for name, val in zip(names, xs[idx])}
        gen_best_out = outputs_list[idx]

        if gen_best_fit < best_fitness:
            best_fitness = gen_best_fit
            best_inputs = dict(gen_best_pt)
            best_outputs = dict(gen_best_out)

        history.append({
            "generation": gen,
            "best_fitness": gen_best_fit,
            "best_inputs": gen_best_pt,
            "best_outputs": gen_best_out,
        })

        if on_gen is not None:
            await on_gen(gen, gen_best_pt, gen_best_out, gen_best_fit)

        if best_fitness <= convergence_threshold:
            converged = True
            break

        gen += 1

    return {
        "best_inputs": best_inputs,
        "best_outputs": best_outputs,
        "best_fitness": best_fitness,
        "generations_run": len(history),
        "converged": converged,
        "history": history,
    }


# ── Coordinate-descent fallback ─────────────────────────────────────────


async def _coordinate_descent(
    names, bounds, mean, evaluate_async, target, registry,
    *, max_generations, pop_size, convergence_threshold, on_gen,
):
    """Deterministic fallback when ``cma`` isn't installed.

    Cycles through each parameter once per generation, probing
    ``pop_size`` evenly spaced values along that parameter's bound while
    holding the others at the current best. Accepts the best probe as
    the new mean. Simple, monotone-ish, and good enough as a floor.
    """
    current = dict(zip(names, mean))
    best_inputs: dict = dict(current)
    best_outputs: dict = await evaluate_async(current)
    best_fitness = _fitness(best_outputs, target, registry)
    history: list[dict] = []
    converged = False

    for gen in range(max_generations):
        improved = False
        for ndx, name in enumerate(names):
            lo, hi = bounds[ndx]
            if hi <= lo:
                continue
            # ``pop_size`` evenly spaced probes along this axis.
            n_probes = max(2, pop_size)
            step = (hi - lo) / (n_probes - 1)
            for k in range(n_probes):
                candidate = dict(current)
                candidate[name] = lo + k * step
                out = await evaluate_async(candidate)
                fit = _fitness(out, target, registry)
                if fit < best_fitness:
                    best_fitness = fit
                    best_inputs = dict(candidate)
                    best_outputs = dict(out)
                    improved = True
            # Update the running point along this axis to the best so far.
            current[name] = best_inputs[name]

        history.append({
            "generation": gen,
            "best_fitness": best_fitness,
            "best_inputs": dict(best_inputs),
            "best_outputs": dict(best_outputs),
        })

        if on_gen is not None:
            await on_gen(gen, best_inputs, best_outputs, best_fitness)

        if best_fitness <= convergence_threshold:
            converged = True
            break
        if not improved:
            # Nothing moved — coordinate descent has stalled, no point
            # continuing. CMA-ES proper would still explore via its
            # covariance updates; the fallback can't.
            break

    return {
        "best_inputs": best_inputs,
        "best_outputs": best_outputs,
        "best_fitness": best_fitness,
        "generations_run": len(history),
        "converged": converged,
        "history": history,
    }
