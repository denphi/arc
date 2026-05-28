"""Design-of-Experiments planners.

Three classic DoE schemes, all matching the ``PlannerAgent`` contract.
Pick one when the user wants *coverage* of the parameter space rather
than optimisation against a target:

  * ``doe_lhs``       — Latin hypercube sampling.
                        Stratifies each dimension into N equal bins,
                        picks one sample per bin per dimension. The
                        default DoE if you don't have a strong reason
                        to pick another.
  * ``doe_factorial`` — 2-level fractional factorial.
                        Samples the corners of the parameter cube
                        (low / high per dimension) plus the centre.
                        Good for screening — which parameters matter?
  * ``doe_sobol``     — Sobol low-discrepancy sequence.
                        More uniform than LHS in high dimensions;
                        deterministic with a fixed start index.

Cold start delegates to the default planner so the first iteration
still gets an LLM-rich plan with real parameter names. Warm path
rewrites ``parameters`` + ``parameter_sweep`` over the numeric subset
of those parameters, leaving non-numeric ones alone.

All three are seeded for determinism — two runs over the same artifact
produce the same sweep, so users can reproduce results and tests stay
stable.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal

logger = logging.getLogger(__name__)


_DEFAULT_SWEEP_SIZE = 8     # rows in the generated sweep
_RANDOM_SEED = 42           # deterministic stratified-LHS shuffling


# ── Bound extraction (shared with the AL planner) ──────────────────────


def _numeric_bounds(plan: ExperimentPlan) -> dict[str, tuple[float, float]]:
    """Pull ``(min, max)`` per numeric parameter with a valid constraint."""
    bounds: dict[str, tuple[float, float]] = {}
    for name, default in plan.parameters.items():
        if not isinstance(default, (int, float)) or isinstance(default, bool):
            continue
        constraint = plan.parameter_constraints.get(name) or {}
        lo, hi = constraint.get("min"), constraint.get("max")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        if hi <= lo:
            continue
        bounds[name] = (float(lo), float(hi))
    return bounds


def _format(value: float) -> float:
    """Six significant figures, matching the default planner's output style."""
    if value == 0:
        return 0.0
    return float(f"{value:.6g}")


def _denormalise(value: float, lo: float, hi: float) -> float:
    return lo + value * (hi - lo)


# ── Latin hypercube sampling ───────────────────────────────────────────


def latin_hypercube(n_samples: int, n_dims: int, *, seed: int = _RANDOM_SEED) -> list[list[float]]:
    """Generate ``n_samples`` points in ``[0, 1]^n_dims`` via stratified LHS.

    Each dimension is divided into ``n_samples`` equal-width bins; a
    permutation of those bins (deterministic, seeded) decides the row
    order per dimension. The sample within each bin lands at the bin
    midpoint — using the midpoint instead of a random offset keeps the
    layout reproducible and prevents corner-clustering artifacts.
    """
    if n_samples <= 0 or n_dims <= 0:
        return []
    rng = random.Random(seed)
    per_dim_perm: list[list[int]] = []
    for _ in range(n_dims):
        order = list(range(n_samples))
        rng.shuffle(order)
        per_dim_perm.append(order)
    points: list[list[float]] = []
    width = 1.0 / n_samples
    for i in range(n_samples):
        point = []
        for d in range(n_dims):
            bin_idx = per_dim_perm[d][i]
            point.append((bin_idx + 0.5) * width)
        points.append(point)
    return points


# ── Fractional factorial ───────────────────────────────────────────────


def fractional_factorial(n_dims: int, *, include_centre: bool = True) -> list[list[float]]:
    """Return the 2-level corners of the unit cube in ``n_dims`` dims.

    With ``n_dims=3`` that's 8 corners plus optionally a centre point
    (1 row). The full 2^n grid grows fast — we cap at ``2^6 = 64``
    corners and fall back to a *fractional* design (every other corner
    by Gray-code) when ``n_dims > 6`` so the sweep stays usable.
    """
    if n_dims <= 0:
        return [[0.5]] if include_centre else []
    if n_dims <= 6:
        # Full factorial.
        corners: list[list[float]] = []
        for combo in range(1 << n_dims):
            corner = [
                1.0 if (combo >> d) & 1 else 0.0
                for d in range(n_dims)
            ]
            corners.append(corner)
    else:
        # Fractional: take every other corner (Gray-code stride). Keeps
        # main effects estimable while bounding the sweep size.
        full = 1 << n_dims
        step = max(1, full // 32)
        corners = [
            [1.0 if (combo >> d) & 1 else 0.0 for d in range(n_dims)]
            for combo in range(0, full, step)
        ]
    if include_centre:
        corners.append([0.5] * n_dims)
    return corners


# ── Sobol sequence ─────────────────────────────────────────────────────

# Joe-Kuo (2008) direction numbers for the first 8 Sobol dimensions.
# Each tuple is (degree, polynomial, initial m values).
_SOBOL_DIRECTIONS = (
    (1, 0, (1,)),
    (2, 1, (1, 3)),
    (3, 1, (1, 3, 1)),
    (3, 2, (1, 1, 1)),
    (4, 1, (1, 1, 3, 3)),
    (4, 4, (1, 3, 5, 13)),
    (5, 2, (1, 1, 5, 5, 17)),
    (5, 4, (1, 1, 5, 5, 5)),
)
_SOBOL_BITS = 30


def _sobol_direction_table(n_dims: int) -> list[list[int]]:
    """Pre-compute the V matrix of direction numbers per dimension."""
    table: list[list[int]] = []
    for d in range(min(n_dims, len(_SOBOL_DIRECTIONS))):
        degree, poly, m = _SOBOL_DIRECTIONS[d]
        if d == 0:
            v = [1 << (_SOBOL_BITS - 1 - i) for i in range(_SOBOL_BITS)]
        else:
            v = [0] * _SOBOL_BITS
            for i in range(degree):
                v[i] = m[i] << (_SOBOL_BITS - 1 - i)
            for i in range(degree, _SOBOL_BITS):
                value = v[i - degree] ^ (v[i - degree] >> degree)
                for k in range(1, degree):
                    if (poly >> (degree - 1 - k)) & 1:
                        value ^= v[i - k]
                v[i] = value
        table.append(v)
    # Pad extra dimensions by cycling — beyond the 8th the discrepancy
    # bound degrades but the sequence is still better than uniform random.
    while len(table) < n_dims:
        table.append(list(table[len(table) % len(_SOBOL_DIRECTIONS)]))
    return table


def sobol_sequence(n_samples: int, n_dims: int, *, skip: int = 1) -> list[list[float]]:
    """Generate ``n_samples`` points of the Sobol sequence in ``[0, 1]^n_dims``.

    ``skip=1`` drops the all-zero first term so we don't seed sweeps on
    the lower bound.
    """
    if n_samples <= 0 or n_dims <= 0:
        return []
    v = _sobol_direction_table(n_dims)
    x = [0] * n_dims
    points: list[list[float]] = []
    scale = 1.0 / (1 << _SOBOL_BITS)
    for index in range(skip, skip + n_samples):
        # Find the lowest unset bit of ``index`` — controls which
        # direction number xors in next.
        c = 0
        tmp = index
        while tmp & 1:
            c += 1
            tmp >>= 1
        for d in range(n_dims):
            x[d] ^= v[d][c]
        points.append([x[d] * scale for d in range(n_dims)])
    return points


# ── Plan rewrite ──────────────────────────────────────────────────────


def _apply_scheme(
    plan: ExperimentPlan,
    *,
    scheme: str,
    sweep_size: int = _DEFAULT_SWEEP_SIZE,
) -> list[str]:
    """Rewrite ``plan.parameter_sweep`` + ``plan.parameters`` using ``scheme``.

    Returns the list of parameter names that were rewritten — empty
    when no numeric parameters exist to lay out.
    """
    bounds = _numeric_bounds(plan)
    if not bounds:
        return []
    names = list(bounds.keys())
    n_dims = len(names)

    if scheme == "lhs":
        points = latin_hypercube(sweep_size, n_dims)
    elif scheme == "factorial":
        points = fractional_factorial(n_dims, include_centre=True)
    elif scheme == "sobol":
        points = sobol_sequence(sweep_size, n_dims)
    else:
        raise ValueError(f"Unknown DoE scheme: {scheme}")

    if not points:
        return []

    # First point becomes nominal; full set becomes the sweep.
    sweep: dict[str, list[float]] = {name: [] for name in names}
    for pt in points:
        for d, name in enumerate(names):
            lo, hi = bounds[name]
            sweep[name].append(_format(_denormalise(pt[d], lo, hi)))
    plan.parameter_sweep = sweep
    for name in names:
        plan.parameters[name] = sweep[name][0]
    return names


# ── Agents ────────────────────────────────────────────────────────────


class _DoEPlannerBase(AgentContract):
    """Shared plumbing for the three DoE schemes.

    Subclasses set ``_scheme`` and ``name`` / ``description``.
    """

    _scheme: str = ""
    name: str = "doe_planner"
    description: str = "Design-of-Experiments planner."

    async def run(self, input_data: ResearchProposal) -> ExperimentPlan:
        proposal = (
            input_data
            if isinstance(input_data, ResearchProposal)
            else ResearchProposal(**input_data)
        )

        # Cold start: delegate to the default planner for parameter
        # names + constraints, even on the first iteration. DoE doesn't
        # need history; we always rewrite the sweep over the resolved
        # numeric subset.
        from arc.core.strategies import resolve_role as _core_resolve
        DefaultPlanner = _core_resolve(
            "planner", overrides={"planner": "default"},
        )
        plan = await DefaultPlanner(context=self.context).run(proposal)

        rewritten = _apply_scheme(plan, scheme=self._scheme)
        if rewritten:
            scheme_label = {
                "lhs": "Latin hypercube",
                "factorial": "Fractional factorial",
                "sobol": "Sobol quasi-random",
            }.get(self._scheme, self._scheme)
            plan.experimental_design = list(plan.experimental_design) + [
                f"[{self.name}] {scheme_label} sweep across "
                f"{', '.join(rewritten)}.",
            ]
        return plan


class LatinHypercubePlannerAgent(_DoEPlannerBase):
    name = "doe_lhs"
    description = (
        "Latin hypercube DoE planner. Stratifies each numeric parameter "
        "into N bins and picks one sample per bin per dimension. The "
        "default DoE scheme — good general-purpose coverage."
    )
    _scheme = "lhs"


class FractionalFactorialPlannerAgent(_DoEPlannerBase):
    name = "doe_factorial"
    description = (
        "2-level fractional factorial DoE planner. Samples the corners "
        "of the parameter cube (low / high per dim) plus the centre. "
        "Best for sensitivity screening: which parameters matter most?"
    )
    _scheme = "factorial"


class SobolPlannerAgent(_DoEPlannerBase):
    name = "doe_sobol"
    description = (
        "Sobol low-discrepancy DoE planner. Quasi-random points that "
        "fill the parameter cube more uniformly than LHS in high "
        "dimensions. Deterministic with a fixed start index."
    )
    _scheme = "sobol"
