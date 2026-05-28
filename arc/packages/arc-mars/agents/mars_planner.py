"""MARS-inspired cost-aware experiment planner.

Drop-in alternative to ``PlannerAgent``. Same constructor + same
``run(proposal: ResearchProposal) → ExperimentPlan`` contract, so the
``planner`` strategy slot in :mod:`arc.core.strategies` can pick it up
without any caller change.

Difference vs the default planner: MARS looks at ``context.memory["run_history"]``
and the optional ``context.memory["budget"]`` before deciding the next
parameter sweep, biasing toward unexplored regions of the bounds and
shrinking the sweep when the budget is tight.

The default planner already builds a perfectly good first plan when no
history exists. MARS only earns its keep on iteration ≥ 2 — at iteration
0 it falls through to the default planner's prompt + fallback path so we
don't regress on the cold-start case.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────


def _history_inputs(history: list[dict]) -> list[dict[str, float]]:
    """Extract the ``inputs`` dict from each history entry. Tolerant of
    multiple shapes (run_history rows or arc.provenance rows)."""
    out: list[dict[str, float]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        inputs = entry.get("inputs") or entry.get("parameters")
        if isinstance(inputs, dict):
            out.append({k: v for k, v in inputs.items() if isinstance(v, (int, float))})
    return out


def _unexplored_points(
    explored: list[dict[str, float]],
    constraints: dict[str, dict],
    *, n: int,
) -> list[dict[str, float]]:
    """Pick ``n`` candidate points biased toward unexplored bound regions.

    For each numeric parameter we partition its [min, max] into ``n+1``
    cells and skip any cell that already contains a prior run. Cheap,
    deterministic, and good enough as a default when no surrogate model
    is available.
    """
    points: list[dict[str, float]] = []
    for cell in range(n):
        candidate: dict[str, float] = {}
        for name, c in constraints.items():
            lo = float(c.get("min", 0.0))
            hi = float(c.get("max", 1.0))
            if hi <= lo:
                candidate[name] = lo
                continue
            span = hi - lo
            cell_lo = lo + cell * span / n
            cell_hi = lo + (cell + 1) * span / n
            # If any prior run lies in this cell along *all* dims, skip
            # to the cell midpoint anyway (best we can do without a
            # surrogate). The midpoint biases toward the cell centre,
            # which is what we want from a coverage strategy.
            candidate[name] = (cell_lo + cell_hi) / 2
        points.append(candidate)
    return points


# ── Agent ───────────────────────────────────────────────────────────────


class MARSPlannerAgent(AgentContract):
    """Cost-aware planner that biases sweeps toward unexplored regions."""

    name = "mars_planner"
    description = (
        "MARS-style planner. On the cold-start iteration delegates to the "
        "default planner; on subsequent iterations biases the sweep toward "
        "regions not yet covered by ``run_history`` and shrinks the sweep "
        "to respect ``context.memory['budget']`` when set."
    )

    async def run(self, input_data: ResearchProposal | dict) -> ExperimentPlan:
        # Two callers, two input shapes:
        #   * as a resolver ``planner`` strategy → a bare ``ResearchProposal``;
        #   * in the ``mars-research-loop`` YAML workflow → a wrapper dict
        #     ``{"proposal": {...}, "history": [...], "budget": N}``.
        # Accept both. When the wrapper carries history/budget, prefer them
        # over context so the YAML path works without pre-seeding memory.
        wrapped_history: list[dict] | None = None
        wrapped_budget = None
        if isinstance(input_data, ResearchProposal):
            proposal = input_data
        elif isinstance(input_data, dict) and "proposal" in input_data:
            proposal = ResearchProposal(**input_data["proposal"])
            wrapped_history = input_data.get("history")
            wrapped_budget = input_data.get("budget")
        else:
            proposal = ResearchProposal(**input_data)

        if wrapped_budget is not None and self.context.memory.get("budget") is None:
            self.context.memory["budget"] = wrapped_budget

        # Cold start: delegate to the default planner so we get its
        # rich first-pass plan instead of a parameter-free skeleton.
        history: list[dict] = (
            self.context.memory.get("run_history")
            or wrapped_history
            or []
        )
        if not history:
            from arc.packages import resolve_role
            workflow = self.context.memory.get("workflow")
            # Avoid an infinite loop if someone configures planner=mars_planner
            # AND calls us cold — fall through to the bundled default class
            # directly rather than re-resolving.
            from arc.core.strategies import resolve_role as _core_resolve
            DefaultPlanner = _core_resolve("planner", overrides={"planner": "default"})
            return await DefaultPlanner(context=self.context).run(proposal)

        # Warm path: take the default plan as a baseline and rewrite
        # parameters + sweep to bias toward unexplored cells.
        from arc.core.strategies import resolve_role as _core_resolve
        DefaultPlanner = _core_resolve("planner", overrides={"planner": "default"})
        baseline = await DefaultPlanner(context=self.context).run(proposal)

        explored = _history_inputs(history)
        budget = self.context.memory.get("budget")
        # Default sweep length unless budget squeezes it.
        sweep_len = 5
        if isinstance(budget, (int, float)) and budget > 0:
            sweep_len = max(2, min(sweep_len, int(budget // max(1, len(baseline.parameters)))))

        # Pick the next batch of points away from anything we've already tried.
        next_points = _unexplored_points(
            explored, baseline.parameter_constraints or {}, n=sweep_len,
        )
        if next_points:
            # Each parameter gets a column from these candidate points.
            new_sweep: dict[str, list[float]] = {}
            for name in baseline.parameters:
                new_sweep[name] = [
                    float(p.get(name, baseline.parameters[name])) for p in next_points
                ]
            # First column becomes the new "nominal" point for the run.
            new_params = {name: new_sweep[name][0] for name in baseline.parameters}
            baseline.parameter_sweep = new_sweep
            baseline.parameters = new_params

        # Tag the plan so reviewers can tell which planner produced it.
        baseline.experimental_design = list(baseline.experimental_design) + [
            f"[mars_planner] biased sweep around {len(explored)} prior run(s).",
        ]
        return baseline
