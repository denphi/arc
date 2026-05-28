"""Improvement planner: decides what to change next based on reflection."""

from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal


def _numeric_param_names(plan: dict | None, history: list[dict]) -> list[str]:
    """Discover the numeric parameter names to vary.

    Prefer the plan's declared ``parameter_constraints`` / ``parameters``;
    fall back to whatever keys prior runs actually swept. This replaces the
    old hardcoded ``"input_parameter"`` convention (TODO item 11) so the
    planner adapts to the real artifact schema.
    """
    names: list[str] = []
    if plan:
        for key in ("parameter_constraints", "parameters", "parameter_sweep"):
            block = plan.get(key) or {}
            if isinstance(block, dict):
                names.extend(block.keys())
    if not names:
        for entry in history:
            params = entry.get("parameters") or {}
            names.extend(params.keys())
    # Preserve first-seen order, dedup.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _candidates_for(name: str, plan: dict | None, history: list[dict]) -> list[float]:
    """Next sweep points for one parameter, avoiding already-explored values.

    Spreads samples across the parameter's constraint range (``[min, max]``
    from the plan) when known, otherwise across the span of values seen in
    history, otherwise a generic positive range. Drops values already tried.
    """
    explored = {
        entry.get("parameters", {}).get(name)
        for entry in history
        if isinstance(entry.get("parameters"), dict)
    }
    explored.discard(None)

    lo, hi = None, None
    if plan:
        constraints = (plan.get("parameter_constraints") or {}).get(name) or {}
        lo = constraints.get("min")
        hi = constraints.get("max")
    if lo is None or hi is None:
        seen_vals = [v for v in explored if isinstance(v, (int, float))]
        if seen_vals:
            lo = lo if lo is not None else min(seen_vals)
            hi = hi if hi is not None else max(seen_vals) * 2 or 1.0
    if lo is None or hi is None:
        lo, hi = 0.1, 5.0

    lo, hi = float(lo), float(hi)
    if hi <= lo:
        hi = lo + 1.0
    steps = 8
    grid = [round(lo + (hi - lo) * i / (steps - 1), 6) for i in range(steps)]
    # Tolerance-based "already explored" check — exact float equality would
    # almost never match a freshly-rounded grid point against a raw prior
    # value, so dedup with a relative-ish tolerance instead.
    explored_nums = [v for v in explored if isinstance(v, (int, float))]

    def _is_new(v: float) -> bool:
        return all(abs(v - e) > 1e-6 for e in explored_nums)

    candidates = [v for v in grid if _is_new(v)]
    return candidates[:4] or grid[:4]


class ImprovementPlannerAgent(AgentContract):
    name = "improvement_planner"
    description = (
        "Uses reflective memory to decide the next experiment: "
        "parameter adjustment, artifact modification, or hypothesis revision."
    )

    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        input_data = input_data or {}
        # ``reflection`` may arrive as a dict or as a ReviewResult model
        # (the mars workflows feed ``reflect.output`` straight in, and the
        # reflective_reviewer returns a ReviewResult). Normalise to a dict.
        raw_reflection = input_data.get("reflection") or {}
        if hasattr(raw_reflection, "model_dump"):
            reflection: dict = raw_reflection.model_dump()
        elif isinstance(raw_reflection, dict):
            reflection = raw_reflection
        else:
            reflection = {}
        history: list[dict] = input_data.get("history") or []

        # ``improving`` may be a top-level flag (legacy) or inferred from a
        # ReviewResult: an approved/"step" review is treated as improving,
        # an "explore"/"stop" strategy as not.
        if "improving" in reflection:
            improving = bool(reflection["improving"])
        elif "strategy" in reflection:
            improving = reflection.get("strategy") == "step"
        else:
            improving = True
        recommendations: list[str] = (
            reflection.get("recommendations")
            or (reflection.get("review") or {}).get("recommendations", [])
            or []
        )

        action = "adjust_parameters"
        if not improving and len(history) >= 3:
            action = "modify_artifact"
        elif not improving:
            action = "widen_parameter_sweep"

        primary_proposal = input_data.get("proposal") or {}
        # The plan, if the reflection/workflow carried one, tells us the real
        # parameter names + constraints to sweep over.
        plan_hint = (
            input_data.get("plan")
            or reflection.get("plan")
            or (reflection.get("review") or {}).get("plan")
        )

        param_names = _numeric_param_names(plan_hint, history)
        sweep: dict[str, list[float]] = {
            name: _candidates_for(name, plan_hint, history) for name in param_names
        }
        nominal = {name: vals[0] for name, vals in sweep.items() if vals}
        # Back-compat: the mars-iterative-improvement workflow feeds
        # ``improve.output.next_parameters`` straight into the run step's
        # parameters. Keep it a flat dict of name → first candidate.
        next_parameters = nominal

        if primary_proposal:
            plan = ExperimentPlan(
                proposal=ResearchProposal(**primary_proposal),
                artifact_strategy="create_new_sim2l" if action == "modify_artifact" else "reuse",
                parameters=nominal or {"value": 1.0},
                parameter_sweep=sweep,
                success_criteria=[
                    "improvement over prior result",
                    "execution completes without error",
                ],
            )
        else:
            plan = None

        return {
            "action": action,
            "next_parameters": next_parameters,
            "recommendations": recommendations,
            "plan": plan.model_dump() if plan else None,
        }
