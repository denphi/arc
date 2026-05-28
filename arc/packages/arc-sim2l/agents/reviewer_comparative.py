"""Comparative reviewer.

A deterministic reviewer that judges each run purely by relative
improvement over the best prior fitness in ``run_history``. Never
calls an LLM and never reads ``provider`` from memory.

Three use cases the default reviewer doesn't cover well:

  1. **No-LLM operation.** When the chat runs in stub mode or the user
     wants to bound LLM cost, the default reviewer still tries to
     hit the provider and falls back to a generic stub. This one
     produces structured verdicts without any LLM round-trip.

  2. **Benchmarking optimizers.** Comparing GA vs BayesOpt vs CMA-ES
     needs an *identical* judge between runs — an LLM-driven reviewer
     introduces a confound. This reviewer is the apples-to-apples
     judge for optimizer comparisons.

  3. **Auto-acceptance pipelines.** "Approve iff this run beat the
     prior best by ≥ N%" is a hard rule, not a judgment call.

Same ``_distance_to_target`` math as the reflective reviewer so the
two judges grade on the same scale.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.runtime.key_matching import keys_match as _keys_match
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult

logger = logging.getLogger(__name__)


# ── Thresholds ────────────────────────────────────────────────────────


# Approve when fitness ≤ this (0.25% relative target error).
_CONVERGENCE_THRESHOLD = 0.0025

# Treat improvements smaller than this as "marginal" — still better,
# but not enough to feel like progress on its own.
_MIN_MEANINGFUL_IMPROVEMENT = 0.01  # 1%

# Consecutive non-improving runs → switch to exploration strategy.
_STAGNATION_THRESHOLD = 3


# ── Fitness ───────────────────────────────────────────────────────────


def _distance_to_target(
    outputs: dict[str, Any],
    target: dict[str, Any],
    registry: dict | None = None,
) -> float:
    """Mean abs %-error across matched target keys. ``inf`` when no match.

    Mirrors :func:`arc.packages.arc_mars.agents.reflective_reviewer._distance_to_target`
    so the two reviewers grade runs identically — the only real
    difference between them is *what* the deterministic verdict layer
    decides given the numbers.
    """
    if not target or not outputs:
        return float("inf")
    matched = 0
    total = 0.0
    for tk, tv in target.items():
        for ok, ov in outputs.items():
            if _keys_match(tk, ok, registry):
                if isinstance(ov, (int, float)) and ov is not None:
                    total += abs((ov - tv) / max(abs(tv), 1e-12))
                    matched += 1
                break
    return total / matched if matched else float("inf")


def _relative_improvement(current: float, best_prior: float) -> float:
    """Return positive when ``current`` is better (lower) than ``best_prior``.

    Defined as ``(best_prior - current) / max(|best_prior|, 1e-12)``.
    A 1% improvement = 0.01. Negative means regression.
    """
    if best_prior == float("inf"):
        # No prior result; treat any finite current as infinite improvement
        # so the comparison logic doesn't have to special-case it.
        return float("inf") if current != float("inf") else 0.0
    denom = max(abs(best_prior), 1e-12)
    return (best_prior - current) / denom


# ── Agent ─────────────────────────────────────────────────────────────


class ComparativeReviewerAgent(AgentContract):
    """Deterministic reviewer that grades runs by relative improvement.

    Drop-in replacement for ``ReviewerAgent`` — same constructor, same
    ``run(execution: ExecutionResult) → ReviewResult`` contract.
    """

    name = "comparative_reviewer"
    description = (
        "Deterministic reviewer that grades each run purely by relative "
        "improvement over the best prior fitness in run_history. No LLM "
        "round-trip; useful for stub-mode operation, benchmarking "
        "optimizers, and auto-acceptance pipelines."
    )

    async def run(self, input_data: ExecutionResult) -> ReviewResult:
        execution = (
            input_data
            if isinstance(input_data, ExecutionResult)
            else ExecutionResult(**input_data)
        )

        target = self.context.memory.get("target", {})
        registry = self.context.memory.get("schema_registry", {})
        history: list[dict] = self.context.memory.get("run_history", []) or []
        outputs = execution.outputs or {}

        # ── Pathological inputs short-circuit ─────────────────────────
        if execution.status != "completed" or not outputs:
            return ReviewResult(
                approved=False,
                summary=(
                    f"status={execution.status!r}; no outputs to compare."
                    if not outputs
                    else f"status={execution.status!r}; cannot compare."
                ),
                strengths=[],
                weaknesses=[f"Execution {execution.status!r} produced no comparable outputs."],
                recommendations=[
                    "Re-run with the same parameters to confirm reproducibility, "
                    "or rebuild the artifact if the failure is structural.",
                ],
                next_parameters={},
                iteration_complete=False,
                strategy="step",
            )

        # ── Deterministic fitness signals ─────────────────────────────
        current_fit = _distance_to_target(outputs, target, registry)
        prior_fits = [
            _distance_to_target(
                entry.get("outputs", {}), target, registry,
            )
            for entry in history
        ]
        best_prior = min(prior_fits) if prior_fits else float("inf")
        improvement = _relative_improvement(current_fit, best_prior)

        # Stagnation streak counted the same way the reflective reviewer does.
        streak = 0
        running_best = float("inf")
        for fit in prior_fits:
            if fit < running_best:
                running_best = fit
                streak = 0
            else:
                streak += 1
        if improvement <= 0:
            streak += 1

        # ── Verdict ──────────────────────────────────────────────────
        approved = (
            bool(target)
            and current_fit <= _CONVERGENCE_THRESHOLD
        )

        if approved:
            strategy = "stop"
        elif streak >= _STAGNATION_THRESHOLD:
            strategy = "explore"
        else:
            strategy = "step"

        # ── Structured fields (no LLM prose) ─────────────────────────
        strengths: list[str] = []
        weaknesses: list[str] = []
        recommendations: list[str] = []

        if approved:
            strengths.append(
                f"Target reached: fit={current_fit:.3g} ≤ {_CONVERGENCE_THRESHOLD}."
            )
        elif current_fit == float("inf") and target:
            weaknesses.append("No output key matched the requested target.")
            recommendations.append(
                "Check parameter naming or rebuild the artifact to "
                "produce target output keys."
            )
        elif improvement == float("inf"):
            # First completed run with a finite fitness.
            strengths.append(
                f"Established baseline: fit={current_fit:.3g}."
            )
        elif improvement >= _MIN_MEANINGFUL_IMPROVEMENT:
            strengths.append(
                f"Improved on prior best ({best_prior:.3g} → {current_fit:.3g}, "
                f"+{improvement * 100:.2f}%)."
            )
        elif improvement > 0:
            strengths.append(
                f"Marginal improvement ({best_prior:.3g} → {current_fit:.3g}, "
                f"+{improvement * 100:.2f}%)."
            )
            weaknesses.append(
                f"Improvement below {_MIN_MEANINGFUL_IMPROVEMENT * 100:.1f}% "
                f"threshold — likely noise."
            )
        else:
            weaknesses.append(
                f"Regression: fit {best_prior:.3g} → {current_fit:.3g} "
                f"({improvement * 100:+.2f}%)."
            )

        if streak >= _STAGNATION_THRESHOLD and not approved:
            recommendations.append(
                f"{streak} consecutive non-improving runs — switch to "
                f"``explore`` to widen the search."
            )

        # Compact one-line summary the chat UI can render directly.
        summary = (
            f"fit={current_fit:.3g}; best_prior={best_prior:.3g}; "
            f"improvement={improvement * 100:+.2f}%; streak={streak}"
        )

        return ReviewResult(
            approved=approved,
            summary=summary,
            strengths=strengths,
            weaknesses=weaknesses,
            recommendations=recommendations,
            next_parameters={},  # leave optimisation to the planner/optimizer
            iteration_complete=approved,
            strategy=strategy,
        )
