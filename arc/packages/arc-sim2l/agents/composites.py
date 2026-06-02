"""Per-role composite strategy agents (design/todo.md item 5).

A *composite* runs an ordered stack of single strategies for one role and
merges their output with deterministic, role-specific rules. The stack
syntax (``default embeddings materials_project`` / ``a+b`` / ``a, b``) is
parsed in :mod:`arc.core.strategies`; this module supplies the merge
behaviour so a stack does something meaningful for every role instead of
silently collapsing to the default.

Design choices (see todo.md item 5 "Cross-cutting implementation notes"):

  * One composite class per role — merge logic lives here, not hidden in
    the resolver.
  * Single-strategy behaviour is unchanged: the resolver only builds a
    composite when the selector names more than one strategy.
  * A failing component never aborts the stack (logged + skipped) unless
    that would leave nothing to return.
  * ``strategy_names`` is set by the resolver on a generated subclass; the
    instance reads it to know which components to run.

Each composite resolves its components with ``resolve_role(role,
overrides={role: name})`` so component lookup honours the same package
filter / fallback the single path uses.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ExperimentPlan, ResearchGoal, ResearchProposal
from arc.schemas.review import ReviewResult

logger = logging.getLogger(__name__)


class _CompositeBase(AgentContract):
    """Shared plumbing: resolve + instantiate each named component."""

    role: str = ""
    strategy_names: tuple[str, ...] = ()

    def _components(self):
        from arc.core.strategies import resolve_role
        registry = self.context.memory.get("component_registry")
        loaded_packages = set(registry.list_packages()) if registry is not None else None
        for name in self.strategy_names:
            try:
                cls = resolve_role(
                    self.role, overrides={self.role: name}, config={},
                    loaded_packages=loaded_packages,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "composite %s: could not resolve component %r (%s) — skipping",
                    self.role, name, exc,
                )
                continue
            yield name, cls(context=self.context)

    async def run(self, input_data: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError


def _record(self_, key: str, value: Any) -> None:
    try:
        self_.context.memory[f"composite_{key}"] = value
    except Exception:  # noqa: BLE001
        pass


# ── ideator ─────────────────────────────────────────────────────────────


class CompositeIdeatorAgent(_CompositeBase):
    """Run each ideator with the same goal; synthesise one proposal.

    With a provider, the candidate proposals are handed to the hypothesis
    ranker (item 2) so the strongest wins. Without one, the first complete
    proposal is selected. Alternate hypotheses/objectives are recorded on
    ``memory['ideator_candidates']``.
    """

    name = "ideator_composite"
    description = "Runs multiple ideators and synthesises one proposal."
    role = "ideator"

    async def run(self, input_data: ResearchGoal) -> ResearchProposal:
        goal = input_data if isinstance(input_data, ResearchGoal) else ResearchGoal(**input_data)
        candidates: list[ResearchProposal] = []
        for _name, agent in self._components():
            try:
                proposal = await agent.run(goal)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite ideator component %s failed: %s", _name, exc)
                continue
            if isinstance(proposal, ResearchProposal):
                candidates.append(proposal)

        if not candidates:
            # Degenerate: fall back to the default ideator alone.
            from arc.core.strategies import resolve_role
            cls = resolve_role("ideator", overrides={"ideator": "default"})
            return await cls(context=self.context).run(goal)

        from arc.packages.arc_sim2l_agents.hypothesis import select as _select
        result = _select(candidates, goal)
        primary = result["primary"] or candidates[0]
        self.context.memory["ideator_candidates"] = [
            {"hypothesis": e["proposal"].hypothesis, "scores": e["scores"],
             "rejected": e["rejected"], "reason": e["reason"]}
            for e in result["ranked"]
        ]
        self.context.memory["ideator_selection_rationale"] = result["rationale"]
        return primary


# ── planner ─────────────────────────────────────────────────────────────


class CompositePlannerAgent(_CompositeBase):
    """Generate multiple plans and combine their exploration policy.

    Merge rules (todo.md): first plan is the base artifact strategy;
    ``parameters`` first-writer-wins; ``parameter_constraints`` intersect
    numeric ranges; ``parameter_sweep`` concatenates unique values;
    ``experimental_design`` labels appended from each planner.
    """

    name = "planner_composite"
    description = "Runs multiple planners and merges their exploration policy."
    role = "planner"

    async def run(self, input_data: ResearchProposal) -> ExperimentPlan:
        plans: list[ExperimentPlan] = []
        for _name, agent in self._components():
            try:
                plan = await agent.run(input_data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite planner component %s failed: %s", _name, exc)
                continue
            if isinstance(plan, ExperimentPlan):
                plans.append(plan)

        if not plans:
            from arc.core.strategies import resolve_role
            cls = resolve_role("planner", overrides={"planner": "default"})
            return await cls(context=self.context).run(input_data)

        base = plans[0]
        parameters: dict[str, Any] = {}
        constraints: dict[str, dict[str, Any]] = {}
        sweep: dict[str, list[Any]] = {}
        design: list[str] = []

        for plan in plans:
            for k, v in (plan.parameters or {}).items():
                parameters.setdefault(k, v)  # first-writer-wins
            _merge_constraints(constraints, plan.parameter_constraints or {})
            _merge_sweep(sweep, plan.parameter_sweep or {})
            for label in (plan.experimental_design or []):
                if label not in design:
                    design.append(label)

        success_criteria = list(base.success_criteria)
        for plan in plans[1:]:
            for c in plan.success_criteria:
                if c not in success_criteria:
                    success_criteria.append(c)

        return ExperimentPlan(
            proposal=base.proposal,
            artifact_strategy=base.artifact_strategy,
            parameters=parameters or base.parameters,
            parameter_sweep=sweep,
            parameter_constraints=constraints,
            experimental_design=design,
            success_criteria=success_criteria,
        )


def _merge_constraints(into: dict[str, dict[str, Any]], extra: dict[str, dict[str, Any]]) -> None:
    """Intersect numeric ranges when both define min/max; else first-writer-wins."""
    for key, spec in extra.items():
        if key not in into:
            into[key] = dict(spec)
            continue
        cur = into[key]
        for bound, op in (("min", max), ("max", min)):
            a, b = cur.get(bound), spec.get(bound)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                cur[bound] = op(a, b)
            elif b is not None and a is None:
                cur[bound] = b


def _merge_sweep(into: dict[str, list[Any]], extra: dict[str, list[Any]]) -> None:
    for key, values in extra.items():
        bucket = into.setdefault(key, [])
        for v in values:
            if v not in bucket:
                bucket.append(v)


# ── builder ─────────────────────────────────────────────────────────────


class CompositeBuilderAgent(_CompositeBase):
    """Try builder backends in fallback order — first success wins.

    Not a parallel merge: run the first builder; if it raises or yields no
    artifact, pass to the next. Failed attempts are recorded on
    ``memory['composite_builder_failures']``.
    """

    name = "builder_composite"
    description = "Runs builder backends in fallback order; first success wins."
    role = "builder"

    async def run(self, input_data: ExperimentPlan) -> ArtifactDraft:
        failures: list[str] = []
        for _name, agent in self._components():
            try:
                draft = await agent.run(input_data)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{_name}: {exc}")
                logger.warning("composite builder %s failed: %s", _name, exc)
                continue
            if draft is not None:
                if failures:
                    self.context.memory["composite_builder_failures"] = failures
                return draft
        self.context.memory["composite_builder_failures"] = failures
        # Everything failed — re-raise the default builder so the caller sees
        # a real error rather than None.
        from arc.core.strategies import resolve_role
        cls = resolve_role("builder", overrides={"builder": "default"})
        return await cls(context=self.context).run(input_data)


# ── validator ───────────────────────────────────────────────────────────


class CompositeValidatorAgent(_CompositeBase):
    """Run every validator and aggregate reports.

    ``passed = all(report.passed)``; errors/warnings concatenated;
    evaluations merged under namespaced keys (``strategy:evaluator``) to
    avoid collisions.
    """

    name = "validator_composite"
    description = "Runs multiple validators and aggregates their reports."
    role = "validator"

    async def run(self, input_data) -> Any:
        if isinstance(input_data, dict) and "outputs" in input_data:
            return await self.validate(input_data["outputs"], target=input_data.get("target"))
        return await self.validate(input_data)

    async def validate(self, outputs: dict[str, Any], *, target: dict[str, Any] | None = None) -> Any:
        from arc.schemas.research import ValidatorReport

        passed = True
        errors: list[str] = []
        warnings: list[str] = []
        evaluations: dict[str, dict[str, Any]] = {}
        ran_any = False

        for name, agent in self._components():
            validate = getattr(agent, "validate", None)
            if validate is None:
                continue
            try:
                report = await validate(outputs, target=target)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite validator component %s failed: %s", name, exc)
                continue
            ran_any = True
            passed = passed and bool(getattr(report, "passed", True))
            errors.extend(getattr(report, "errors", []) or [])
            warnings.extend(getattr(report, "warnings", []) or [])
            for ev_name, verdict in (getattr(report, "evaluations", {}) or {}).items():
                evaluations[f"{name}:{ev_name}"] = verdict

        if not ran_any:
            return ValidatorReport(passed=True)
        return ValidatorReport(
            passed=passed, errors=errors, warnings=warnings, evaluations=evaluations,
        )


# ── reviewer ────────────────────────────────────────────────────────────


class CompositeReviewerAgent(_CompositeBase):
    """Run several reviewers and form a consensus review.

    Approval requires *all* reviewers to approve. The summary concatenates
    each reviewer's rationale; ``next_parameters`` prefers the first
    non-empty suggestion. Strengths/weaknesses/recommendations are unioned.
    """

    name = "reviewer_composite"
    description = "Runs multiple reviewers and forms a consensus review."
    role = "reviewer"

    async def run(self, input_data: ExecutionResult) -> ReviewResult:
        reviews: list[tuple[str, ReviewResult]] = []
        for name, agent in self._components():
            try:
                review = await agent.run(input_data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite reviewer component %s failed: %s", name, exc)
                continue
            if isinstance(review, ReviewResult):
                reviews.append((name, review))

        if not reviews:
            from arc.core.strategies import resolve_role
            cls = resolve_role("reviewer", overrides={"reviewer": "default"})
            return await cls(context=self.context).run(input_data)

        approved = all(r.approved for _, r in reviews)
        summary = " | ".join(f"[{n}] {r.summary}" for n, r in reviews if r.summary)
        strengths = _union(r.strengths for _, r in reviews)
        weaknesses = _union(r.weaknesses for _, r in reviews)
        recommendations = _union(r.recommendations for _, r in reviews)
        next_parameters = next((r.next_parameters for _, r in reviews if r.next_parameters), {})
        iteration_complete = all(r.iteration_complete for _, r in reviews)
        # Strategy: most conservative wins (stop > explore > step).
        strategies = [r.strategy for _, r in reviews]
        strategy = "stop" if "stop" in strategies else ("explore" if "explore" in strategies else "step")

        return ReviewResult(
            approved=approved, summary=summary or "Consensus review.",
            strengths=strengths, weaknesses=weaknesses, recommendations=recommendations,
            next_parameters=next_parameters, iteration_complete=iteration_complete,
            strategy=strategy,
        )


def _union(iterables) -> list[str]:
    out: list[str] = []
    for it in iterables:
        for x in (it or []):
            if x not in out:
                out.append(x)
    return out


# ── reflector ───────────────────────────────────────────────────────────


class CompositeReflectorAgent(_CompositeBase):
    """Run reflectors for side effects, then merge lessons.

    Each reflector runs with the same review/execution (preserving side
    effects like learned skills / failure clusters). Lessons are merged by
    unique key; a failing reflector does not block the rest.
    """

    name = "reflector_composite"
    description = "Runs multiple reflectors and merges their lessons."
    role = "reflector"

    async def run(self, input_data: ReviewResult, execution: ExecutionResult | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        seen_lessons: list[str] = []
        for name, agent in self._components():
            try:
                lessons = await agent.run(input_data, execution=execution)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite reflector component %s failed: %s", name, exc)
                continue
            if not isinstance(lessons, dict):
                continue
            for k, v in lessons.items():
                if k == "recommendations" and isinstance(v, list):
                    base = merged.setdefault("recommendations", [])
                    for rec in v:
                        sig = str(rec)
                        if sig not in seen_lessons:
                            seen_lessons.append(sig)
                            base.append(rec)
                else:
                    merged.setdefault(k, v)
        return merged


# ── optimizer ───────────────────────────────────────────────────────────


class CompositeOptimizerAgent(_CompositeBase):
    """Treat optimizers as candidate generators under a shared budget.

    Each optimizer gets a share of the generation/pop budget; the global
    best by the shared fitness wins. Per-optimizer histories are recorded
    under ``history`` tagged with their strategy name.
    """

    name = "optimizer_composite"
    description = "Runs multiple optimizers and keeps the global best candidate."
    role = "optimizer"

    async def run(self, artifact, target: dict[str, Any], max_generations: int = 10,
                  pop_size: int = 8, **kwargs) -> dict[str, Any]:
        components = list(self._components())
        if not components:
            from arc.core.strategies import resolve_role
            cls = resolve_role("optimizer", overrides={"optimizer": "default"})
            return await cls(context=self.context).run(
                artifact, target, max_generations=max_generations, pop_size=pop_size, **kwargs,
            )

        # Split the generation budget so the *total* across optimizers never
        # exceeds ``max_generations`` (review finding C — the earlier
        # ``max_generations // n`` gave that floor to *every* optimizer, so
        # three optimizers tripled the budget). ``divmod`` distributes the
        # remainder to the first ``rem`` optimizers; components that would get
        # zero generations are skipped (more optimizers than budget allows).
        n = len(components)
        base, rem = divmod(max_generations, n)
        best: dict[str, Any] | None = None
        combined_history: list[dict[str, Any]] = []
        for i, (name, agent) in enumerate(components):
            gen_budget = base + (1 if i < rem else 0)
            if gen_budget <= 0:
                logger.debug(
                    "composite optimizer: no budget left for %s "
                    "(max_generations=%s, %d optimizers) — skipping",
                    name, max_generations, n,
                )
                continue
            try:
                result = await agent.run(
                    artifact, target, max_generations=gen_budget, pop_size=pop_size,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite optimizer component %s failed: %s", name, exc)
                continue
            if not isinstance(result, dict):
                continue
            for h in (result.get("history") or []):
                entry = dict(h) if isinstance(h, dict) else {"value": h}
                entry["optimizer"] = name
                combined_history.append(entry)
            if best is None or _fitness_of(result) < _fitness_of(best):
                best = dict(result)
                best["optimizer"] = name

        if best is None:
            from arc.core.strategies import resolve_role
            cls = resolve_role("optimizer", overrides={"optimizer": "default"})
            return await cls(context=self.context).run(
                artifact, target, max_generations=max_generations, pop_size=pop_size, **kwargs,
            )
        best["history"] = combined_history
        return best


def _fitness_of(result: dict[str, Any]) -> float:
    """Lower is better — the GA fitness is a distance-to-target metric where
    ``inf`` is the worst (no target key matched). A missing/unparseable value
    therefore maps to ``inf`` so it never beats a real candidate."""
    val = result.get("best_fitness")
    try:
        return float(val)
    except (TypeError, ValueError):
        return float("inf")


# ── curator ─────────────────────────────────────────────────────────────


class CompositeCuratorAgent(_CompositeBase):
    """Run curators as ordered normalizers — each receives the prior output."""

    name = "curator_composite"
    description = "Runs curators in sequence; each refines the previous output."
    role = "curator"

    async def run(self, artifact: Any) -> Any:
        current = artifact
        for name, agent in self._components():
            try:
                current = await agent.run(current)
            except Exception as exc:  # noqa: BLE001
                logger.debug("composite curator component %s failed: %s", name, exc)
                continue
        return current


# ── registry of composites by role ──────────────────────────────────────

COMPOSITE_BY_ROLE: dict[str, type[_CompositeBase]] = {
    "ideator": CompositeIdeatorAgent,
    "planner": CompositePlannerAgent,
    "builder": CompositeBuilderAgent,
    "validator": CompositeValidatorAgent,
    "reviewer": CompositeReviewerAgent,
    "reflector": CompositeReflectorAgent,
    "optimizer": CompositeOptimizerAgent,
    "curator": CompositeCuratorAgent,
    # searcher keeps its existing dedicated composite in searcher.py.
}
