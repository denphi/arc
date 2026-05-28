"""LLM-guided optimizer.

Fourth drop-in alternative for the ``optimizer`` role (alongside the
genetic algorithm, Bayesian optimization, and CMA-ES). Same constructor
+ same ``run(artifact, target, ...)`` contract, so the ``/optimize``
slash command and the ``optimizer`` strategy slot can swap it in with
no caller change.

When to use it
==============

When each simulation call is **very** expensive (minutes to hours) and
you'd rather spend a few LLM tokens than burn budget on numerical
samples. Examples:

  * DFT calculations on small supercells (~minutes per point)
  * Molecular-dynamics equilibration runs
  * Anything where 100 BayesOpt steps would take a day but 10
    LLM-proposed points get you 90% of the way

How it works
============

Each "generation" hands the LLM:

  * The target the loop is steering toward.
  * The parameter bounds (so values stay physically plausible).
  * The N most recent (inputs, outputs, fitness) triples.

…and asks for ``pop_size`` candidate points in strict JSON form. We
evaluate each, fold them into the history, and ask again. Same fitness
function as the GA + BayesOpt + CMA-ES, so benchmark comparisons stay
honest.

Fallback
========

Without a provider, falls back to **Latin hypercube sampling** over the
parameter bounds — quasi-uniform coverage, deliberately different from
BayesOpt's random fallback and CMA-ES's coordinate-descent so the four
optimizers don't degrade to the same baseline.
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Any

from arc.contracts.agent import AgentContract
from arc.packages.arc_sim2l_agents.optimizer import _fitness  # reuse the GA's metric
from arc.packages.arc_sim2l_agents.optimizer_bayes import _bounds_from_schema
from arc.packages.arc_sim2l_agents.planner_doe import latin_hypercube
from arc.schemas.artifact import ArtifactRecord

logger = logging.getLogger(__name__)


# Single fixed RNG seed used both by ``latin_hypercube`` (the no-provider
# fallback) and to fill in any candidates the LLM couldn't produce — so
# two runs over the same artifact yield reproducible histories.
_RANDOM_SEED = 42


# ── Prompt construction ────────────────────────────────────────────────


_PROMPT = """\
You are an experimental-design adviser. Suggest parameter combinations
to evaluate next in a simulation campaign.

Target (we want output(s) close to these values): {target}

Parameter bounds (each value must fall in [min, max]):
{bounds_block}

History (most recent {n_history} runs, oldest first):
{history_block}

Suggest {n_points} new candidate point(s). Bias toward:
  * Reducing distance to the target if the history shows a trend.
  * Exploring regions of the bound box that prior runs missed when
    the trend is unclear.

Return STRICT JSON: a list of {n_points} objects, each mapping every
parameter name to a numeric value within its bound. No prose, no
extra fields, no comments. Example shape:

[{{"thickness": 5.2, "temperature": 298.0}}, ...]
"""


def _bounds_block(bounds: dict[str, tuple[float, float]]) -> str:
    return "\n".join(
        f"  - {name}: [{lo:.6g}, {hi:.6g}]"
        for name, (lo, hi) in bounds.items()
    )


def _history_block(history: list[dict]) -> str:
    if not history:
        return "  (no prior runs)"
    lines: list[str] = []
    for i, entry in enumerate(history):
        inputs = entry.get("inputs", {})
        outputs = entry.get("outputs", {})
        fit = entry.get("fitness")
        fit_str = f"fit={fit:.4g}" if isinstance(fit, (int, float)) else ""
        lines.append(
            f"  [{i}] inputs={inputs}  outputs={outputs}  {fit_str}".rstrip()
        )
    return "\n".join(lines)


# ── Response parsing ───────────────────────────────────────────────────


_JSON_BLOCK = re.compile(r"\[.*\]", re.DOTALL)


def _parse_candidates(
    raw: str,
    bounds: dict[str, tuple[float, float]],
    *,
    n_points: int,
) -> list[dict[str, float]]:
    """Parse the LLM's JSON list into validated candidate points.

    Returns up to ``n_points`` candidates. Each value is clamped to the
    parameter's bound; rows missing required keys are dropped. A
    response that doesn't contain a parseable JSON list yields ``[]``,
    which triggers the caller's LHS fill.
    """
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    # The model may wrap output in ```json fences; strip them.
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Some models add prose before/after the JSON; find the first [...] span.
    match = _JSON_BLOCK.search(text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    out: list[dict[str, float]] = []
    for entry in data[:n_points]:
        if not isinstance(entry, dict):
            continue
        point: dict[str, float] = {}
        valid = True
        for name, (lo, hi) in bounds.items():
            value = entry.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                valid = False
                break
            # Clamp into the bound — better to use a slightly-pushed-in
            # value than to drop the candidate over a minor overshoot.
            point[name] = max(lo, min(hi, float(value)))
        if valid:
            out.append(point)
    return out


def _lhs_candidates(
    bounds: dict[str, tuple[float, float]],
    *,
    n_points: int,
    seed: int = _RANDOM_SEED,
) -> list[dict[str, float]]:
    """LHS fill — used as the fallback and to top up partial LLM responses."""
    if n_points <= 0 or not bounds:
        return []
    names = list(bounds.keys())
    points_unit = latin_hypercube(n_points, len(names), seed=seed)
    out: list[dict[str, float]] = []
    for unit_point in points_unit:
        candidate: dict[str, float] = {}
        for i, name in enumerate(names):
            lo, hi = bounds[name]
            candidate[name] = lo + unit_point[i] * (hi - lo)
        out.append(candidate)
    return out


# ── Agent ──────────────────────────────────────────────────────────────


class LLMGuidedOptimizerAgent(AgentContract):
    """Optimizer that proposes candidate points by consulting an LLM.

    Drop-in for :class:`GeneticOptimizerAgent`. Same constructor + same
    return shape. The agent never executes simulations directly — it
    delegates to the adapter on ``context.memory['adapter']`` exactly
    like the other optimizers.
    """

    name = "optimizer"
    description = (
        "LLM-guided optimizer. Hands the most recent run history + "
        "target + parameter bounds to the configured LLM provider and "
        "asks for candidate points. Best when each simulation is "
        "expensive enough that LLM tokens are cheap by comparison. "
        "Falls back to Latin hypercube sampling without a provider."
    )

    async def run(
        self,
        artifact: ArtifactRecord,
        target: dict[str, Any],
        max_generations: int = 10,
        pop_size: int = 4,
        elite_frac: float = 0.25,        # noqa: ARG002 — accepted for sig compat
        convergence_threshold: float = 0.0025,
        on_generation=None,
    ) -> dict[str, Any]:
        adapter = self.context.memory.get("adapter")
        if adapter is None:
            raise RuntimeError(
                "LLMGuidedOptimizerAgent requires 'adapter' in context.memory"
            )

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
                "Artifact has no sim2l_inputs schema — cannot initialise "
                "LLM-guided search"
            )

        registry: dict = self.context.memory.get("schema_registry", {})
        schema_defaults = {
            k: (v.get("default", 1.0) if isinstance(v, dict) else 1.0)
            for k, v in schema.items()
        }
        bounds = _bounds_from_schema(schema)
        param_names = list(bounds.keys())

        # Reuse the in-process simulate() shortcut when available — same
        # trick the GA / BO / CMA-ES use to skip per-eval subprocess cost.
        _simulate_func = None
        try:
            from pathlib import Path
            from arc.runtime.sim2l_adapter import _import_workflow_func
            workflow_path = Path(artifact.path) / "workflow.py"
            if workflow_path.exists():
                _simulate_func = _import_workflow_func(
                    artifact.path, artifact.artifact_id,
                )
        except Exception:
            _simulate_func = None

        async def _evaluate(point: dict) -> dict:
            inputs = {**schema_defaults,
                      **{k: v for k, v in point.items() if k in schema}}
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

        provider = self.context.memory.get("provider")
        history_for_llm: list[dict] = []
        best_inputs: dict = {}
        best_outputs: dict = {}
        best_fitness = float("inf")
        history: list[dict] = []
        converged = False
        n_calls = 0
        rng = random.Random(_RANDOM_SEED)

        for gen in range(max_generations):
            # Pull candidates from the LLM (when available) and top up
            # from LHS with a per-generation seed so different gens
            # produce different fallback points.
            llm_candidates: list[dict[str, float]] = []
            if provider is not None and hasattr(provider, "complete"):
                prompt = _PROMPT.format(
                    target=target or "(no target — explore broadly)",
                    bounds_block=_bounds_block(bounds),
                    n_history=len(history_for_llm),
                    history_block=_history_block(history_for_llm),
                    n_points=pop_size,
                )
                try:
                    response = await provider.complete(prompt)
                    llm_candidates = _parse_candidates(
                        response, bounds, n_points=pop_size,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("LLM-guided proposal failed: %s", exc)
                    llm_candidates = []

            need = pop_size - len(llm_candidates)
            if need > 0:
                fallback = _lhs_candidates(
                    bounds, n_points=need,
                    seed=_RANDOM_SEED + gen * 1000 + n_calls,
                )
                candidates = llm_candidates + fallback
            else:
                candidates = llm_candidates[:pop_size]

            # Shuffle so the LLM's first candidate isn't always evaluated
            # first — keeps benchmarking honest when the LLM picks
            # corner points.
            rng.shuffle(candidates)

            fitnesses: list[float] = []
            outputs_list: list[dict] = []
            for cand in candidates:
                n_calls += 1
                out = await _evaluate(cand)
                fit = _fitness(out, target, registry)
                fitnesses.append(fit)
                outputs_list.append(out)
                history_for_llm.append({
                    "inputs": dict(cand),
                    "outputs": dict(out),
                    "fitness": fit if fit != float("inf") else None,
                })
                # Keep the LLM context bounded.
                if len(history_for_llm) > 20:
                    history_for_llm = history_for_llm[-20:]

            # Best of this generation.
            idx = min(range(len(fitnesses)), key=lambda i: fitnesses[i])
            gen_best_fit = fitnesses[idx]
            gen_best_pt = dict(candidates[idx])
            gen_best_out = dict(outputs_list[idx])

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

            if on_generation is not None:
                await on_generation(gen, gen_best_pt, gen_best_out, gen_best_fit)

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
