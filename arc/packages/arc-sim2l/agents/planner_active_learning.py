"""Active-learning planner.

Drop-in alternative to ``PlannerAgent``. Same constructor + same
``run(proposal: ResearchProposal) → ExperimentPlan`` contract.

The difference vs. BayesOpt is what each one *optimises*:

  * BayesOpt minimises a known objective (the target). It needs a
    target value and a way to score outputs against it.
  * Active learning **explores** — it picks the next point that
    maximises information gain about the response surface, with no
    target required. The right planner when the user wants to
    *characterise* a system, not steer it toward a value.

Acquisition strategy
====================

We use **maximin-distance** in the unit hypercube:

  1. Normalise every numeric parameter to ``[0, 1]`` using its
     ``parameter_constraints`` ``[min, max]`` band.
  2. Generate ``K`` quasi-uniform candidate points via a fixed-seed
     Halton-like sequence (no external deps, deterministic).
  3. For each candidate, score = min Euclidean distance to any prior
     ``run_history`` point in the same normalised space.
  4. Pick the top ``N`` candidates by score → that's the new sweep.
  5. The top candidate becomes the nominal ``parameters``.

Cold start (no history) → delegate to the default planner. Warm path
rewrites the sweep over the LLM-supplied or fallback parameter names.
Non-numeric parameters pass through unchanged.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal

logger = logging.getLogger(__name__)


# ── Halton-like quasi-uniform sampling (deterministic, no deps) ────────


def _halton(index: int, base: int) -> float:
    """Return the ``index``-th term of the Halton sequence in base ``base``.

    Halton sequences fill ``[0, 1]`` more evenly than uniform random
    sampling and are deterministic — two runs over the same artifact
    produce the same candidate set.
    """
    f = 1.0
    result = 0.0
    i = index
    while i > 0:
        f /= base
        result += f * (i % base)
        i //= base
    return result


# First 6 primes — enough dimensions for almost every materials plan.
# When the plan has more numeric parameters than primes we fall back
# to cycling, which is fine for an active-learning seed.
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29)


def _halton_points(n_points: int, n_dims: int, *, skip: int = 1) -> list[list[float]]:
    """Generate ``n_points`` quasi-uniform points in ``n_dims`` dimensions.

    ``skip=1`` drops the all-zero first term so a Halton sequence
    doesn't seed candidates on the lower bound.
    """
    primes = [_PRIMES[d % len(_PRIMES)] for d in range(n_dims)]
    return [
        [_halton(skip + i, primes[d]) for d in range(n_dims)]
        for i in range(n_points)
    ]


# ── History → normalised point ─────────────────────────────────────────


def _normalise(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _history_points(
    history: list[dict],
    param_names: list[str],
    bounds: dict[str, tuple[float, float]],
) -> list[list[float]]:
    """Project every history entry into the unit hypercube spanned by
    ``param_names``. Entries missing a parameter use 0.5 (midpoint).
    Non-numeric values fall back to 0.5 too — better than dropping
    the entry entirely since downstream code wants a complete vector.
    """
    points: list[list[float]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        inputs = entry.get("inputs") or entry.get("parameters") or {}
        if not isinstance(inputs, dict):
            continue
        point: list[float] = []
        for name in param_names:
            lo, hi = bounds[name]
            raw = inputs.get(name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                point.append(_normalise(float(raw), lo, hi))
            else:
                point.append(0.5)
        points.append(point)
    return points


def _min_distance(point: list[float], others: list[list[float]]) -> float:
    """Minimum Euclidean distance from ``point`` to any in ``others``.

    ``others`` empty → return infinity so any candidate wins; the
    cold-start branch is handled separately but this keeps the helper
    safe to call.
    """
    if not others:
        return math.inf
    best = math.inf
    for other in others:
        if len(other) != len(point):
            continue
        squared = sum((a - b) ** 2 for a, b in zip(point, other))
        d = math.sqrt(squared)
        if d < best:
            best = d
    return best


# ── Plan synthesis ────────────────────────────────────────────────────


def _extract_bounds(plan: ExperimentPlan) -> dict[str, tuple[float, float]]:
    """Pull ``(min, max)`` per numeric parameter from the plan's
    ``parameter_constraints``. Drops parameters whose default isn't a
    number or whose constraint is missing/malformed.
    """
    bounds: dict[str, tuple[float, float]] = {}
    for name, default in plan.parameters.items():
        if not isinstance(default, (int, float)) or isinstance(default, bool):
            continue
        constraint = plan.parameter_constraints.get(name) or {}
        lo = constraint.get("min")
        hi = constraint.get("max")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        if hi <= lo:
            continue
        bounds[name] = (float(lo), float(hi))
    return bounds


def _denormalise(value: float, lo: float, hi: float) -> float:
    return lo + value * (hi - lo)


def _format(value: float) -> float:
    """Round to 6 significant figures — same as the default planner."""
    if value == 0:
        return 0.0
    return float(f"{value:.6g}")


def _apply_active_learning_sweep(
    plan: ExperimentPlan,
    history: list[dict],
    *,
    sweep_size: int = 5,
    n_candidates: int = 64,
) -> list[str]:
    """Rewrite ``plan.parameter_sweep`` + ``plan.parameters`` to a
    maximin-distance acquisition sweep. Returns the list of parameter
    names that got rewritten; an empty list means the planner had
    nothing numeric to work with and the LLM/fallback plan stands.
    """
    bounds = _extract_bounds(plan)
    if not bounds:
        return []
    names = list(bounds.keys())

    # Project history into the unit hypercube.
    prior = _history_points(history, names, bounds)

    # Generate candidates and score by maximin distance.
    candidates = _halton_points(n_candidates, len(names))
    scored: list[tuple[float, list[float]]] = []
    for cand in candidates:
        score = _min_distance(cand, prior)
        scored.append((score, cand))
    # Higher score = farther from history = better.
    scored.sort(key=lambda pair: pair[0], reverse=True)

    # Top N candidates form the sweep (one column per parameter).
    top = [pt for _score, pt in scored[:sweep_size]]
    if not top:
        return []

    # Rewrite parameters + sweep.
    sweep: dict[str, list[float]] = {}
    for col, name in enumerate(names):
        lo, hi = bounds[name]
        sweep[name] = [_format(_denormalise(pt[col], lo, hi)) for pt in top]
    plan.parameter_sweep = sweep
    # First column is the nominal point.
    for name in names:
        plan.parameters[name] = sweep[name][0]
    return names


# ── Agent ──────────────────────────────────────────────────────────────


class ActiveLearningPlannerAgent(AgentContract):
    """Planner that picks the next sweep by maximin-distance acquisition."""

    name = "active_learning_planner"
    description = (
        "Active-learning planner. On the cold-start iteration delegates "
        "to the default planner; on subsequent iterations rewrites the "
        "sweep using maximin-distance acquisition in the unit hypercube "
        "spanned by the parameter constraints. Picks points that are "
        "farthest from anything in ``run_history`` — the right planner "
        "when the user wants to *characterise* a system rather than "
        "optimise toward a target."
    )

    async def run(self, input_data: ResearchProposal) -> ExperimentPlan:
        proposal = (
            input_data
            if isinstance(input_data, ResearchProposal)
            else ResearchProposal(**input_data)
        )

        # Cold start: delegate to the default planner so the first
        # iteration still gets a rich LLM-derived plan instead of a
        # parameter-free skeleton. Same trick the MARS planner uses.
        history: list[dict] = self.context.memory.get("run_history", []) or []

        from arc.core.strategies import resolve_role as _core_resolve
        DefaultPlanner = _core_resolve(
            "planner", overrides={"planner": "default"},
        )
        baseline = await DefaultPlanner(context=self.context).run(proposal)

        if not history:
            return baseline

        rewritten = _apply_active_learning_sweep(baseline, history)
        if rewritten:
            baseline.experimental_design = list(baseline.experimental_design) + [
                f"[active_learning_planner] maximin-distance sweep around "
                f"{len(history)} prior run(s); rewrote: "
                f"{', '.join(rewritten)}.",
            ]
        return baseline
