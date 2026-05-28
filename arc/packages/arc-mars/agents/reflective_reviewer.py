"""Reflective reviewer agent.

Drop-in alternative to ``ReviewerAgent``. Same constructor + same
``run(execution: ExecutionResult) → ReviewResult`` contract, so the
``reviewer`` strategy slot in :mod:`arc.core.strategies` can pick it up
without any caller change.

Difference vs the default reviewer: this one cross-references the prior
``context.memory["run_history"]`` and ``context.memory["reflections"]``
entries to decide whether the current run actually *improves* on what
the loop has already seen. The default reviewer judges each run against
the target in isolation; the reflective variant adds three signals:

  * **Trend** — is the best-so-far fitness improving? If not, recommend
    a different parameter regime instead of more of the same.
  * **Repeat detection** — flag runs whose inputs are within ε of a
    prior run (the user almost certainly meant to vary something).
  * **Stagnation** — when N consecutive runs have failed to improve,
    suggest switching to a different strategy (``explore`` instead of
    ``step``).

When the LLM provider is configured we still call it for the qualitative
fields (strengths / weaknesses / summary), but the verdict + strategy
are computed deterministically from the history.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.agent import AgentContract
from arc.runtime.key_matching import keys_match as _keys_match
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────


_STAGNATION_THRESHOLD = 3  # consecutive non-improving runs → switch strategy
_CLUSTER_DOMINANCE_THRESHOLD = 2  # ≥ this many entries in the top failure cluster → escalate to explore


def _distance_to_target(
    outputs: dict[str, Any],
    target: dict[str, Any],
    registry: dict | None = None,
) -> float:
    """Return mean abs %-error across matched target keys, or ∞ if no match.

    Mirrors the GA's ``_fitness`` so the two agents grade results on the
    same scale.
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


def _inputs_match(
    a: dict[str, Any], b: dict[str, Any], *, tol: float = 0.01,
) -> bool:
    """Return True if every shared key matches within ``tol`` relative."""
    common = set(a) & set(b)
    if not common:
        return False
    for k in common:
        av, bv = a.get(k), b.get(k)
        if not isinstance(av, (int, float)) or not isinstance(bv, (int, float)):
            if av != bv:
                return False
            continue
        denom = max(abs(av), abs(bv), 1e-12)
        if abs(av - bv) / denom > tol:
            return False
    return True


# ── Agent ───────────────────────────────────────────────────────────────


class ReflectiveReviewerAgent(AgentContract):
    """Reviewer that grades a run against the trend in run_history."""

    name = "reflective_reviewer"
    description = (
        "Reviewer that grades each run against the trend in run_history "
        "instead of in isolation. Flags repeats, detects stagnation, and "
        "switches to ``explore`` strategy when steps stop improving."
    )

    async def run(self, input_data: ExecutionResult | dict) -> ReviewResult:
        # Two callers, two input shapes:
        #   * as a resolver ``reviewer`` strategy → a bare ``ExecutionResult``;
        #   * in the mars YAML workflows → a wrapper dict
        #     ``{"plan": ..., "result": <ExecutionResult|dict>, "history": [...]}``.
        wrapped_history: list[dict] | None = None
        if isinstance(input_data, ExecutionResult):
            execution = input_data
        elif isinstance(input_data, dict) and "result" in input_data:
            result = input_data.get("result")
            wrapped_history = input_data.get("history")
            if result is None:
                execution = ExecutionResult(run_id="none", status="error", outputs={})
            elif isinstance(result, ExecutionResult):
                execution = result
            else:
                execution = ExecutionResult(**result)
        else:
            execution = ExecutionResult(**input_data)

        target = self.context.memory.get("target", {})
        registry = self.context.memory.get("schema_registry", {})
        history: list[dict] = (
            self.context.memory.get("run_history")
            or wrapped_history
            or []
        )
        outputs = execution.outputs or {}

        # ── Deterministic signals ────────────────────────────────────
        current_fit = _distance_to_target(outputs, target, registry)
        prior_fits = [
            _distance_to_target(
                entry.get("outputs", {}), target, registry,
            )
            for entry in history
        ]
        best_prior = min(prior_fits) if prior_fits else float("inf")
        improving = current_fit < best_prior

        # Repeat detection: any prior run with near-identical inputs?
        repeat_of = None
        current_inputs = execution.metrics or {}
        for entry in history:
            prior_inputs = entry.get("inputs", {}) or {}
            if _inputs_match(current_inputs, prior_inputs):
                repeat_of = entry
                break

        # Stagnation: count consecutive non-improving runs (latest first).
        streak = 0
        running_best = float("inf")
        for fit in prior_fits:
            if fit < running_best:
                running_best = fit
                streak = 0
            else:
                streak += 1
        if not improving:
            streak += 1

        # Failure-clusters signal: the failure_clustering reflector
        # leaves a snapshot of recent systemic failures on memory. We
        # treat a *dominant* cluster (>= threshold entries of the same
        # signature) as a strong escalator — the next iteration should
        # widen the search instead of nudging the same parameter set.
        # Empty/missing means the prior reflector didn't run or saw
        # nothing systemic, so we just fall through to the streak logic.
        failure_clusters: list[dict] = (
            self.context.memory.get("failure_clusters") or []
        )
        dominant_cluster: dict | None = (
            failure_clusters[0]
            if failure_clusters and isinstance(failure_clusters[0], dict)
            and failure_clusters[0].get("count", 0) >= _CLUSTER_DOMINANCE_THRESHOLD
            else None
        )

        # ── Verdict ──────────────────────────────────────────────────
        approved = (
            execution.status == "completed"
            and bool(outputs)
            and bool(target)
            and current_fit <= 0.0025  # 0.25% — matches the GA threshold
        )

        if approved:
            strategy = "stop"
        elif dominant_cluster is not None:
            # Systemic failure detected on the prior iteration — escalate
            # to explore regardless of streak. This is the wiring between
            # the failure_clustering reflector and the reflective
            # reviewer: clusters → strategy change.
            strategy = "explore"
        elif streak >= _STAGNATION_THRESHOLD:
            strategy = "explore"
        else:
            strategy = "step"

        # ── Qualitative summary ──────────────────────────────────────
        weaknesses: list[str] = []
        recommendations: list[str] = []
        if repeat_of is not None:
            weaknesses.append("Inputs nearly identical to a prior run.")
            recommendations.append("Vary at least one parameter to extract new information.")
        if not improving and prior_fits:
            weaknesses.append(
                f"No improvement over best prior fitness ({best_prior:.3g})."
            )
        if streak >= _STAGNATION_THRESHOLD:
            recommendations.append(
                f"{streak} consecutive non-improving runs — switch strategy ({strategy})."
            )
        if current_fit == float("inf") and target:
            weaknesses.append("No output key matched the requested target.")
        if dominant_cluster is not None:
            count = dominant_cluster.get("count", 0)
            reason = dominant_cluster.get("reason") or dominant_cluster.get("signature", "")
            weaknesses.append(
                f"Systemic failure across {count} prior runs: {reason}"
            )
            recommendations.append(
                "Switch parameter regime rather than nudging current values — "
                f"the existing search is producing repeat failures ({reason})."
            )

        strengths: list[str] = []
        if execution.status == "completed":
            strengths.append("Execution completed.")
        if improving and prior_fits:
            strengths.append(
                f"Improved on prior best ({best_prior:.3g} → {current_fit:.3g})."
            )

        summary = (
            f"fit={current_fit:.3g}; best_prior={best_prior:.3g}; "
            f"improving={improving}; streak={streak}"
        )

        return ReviewResult(
            approved=approved,
            summary=summary,
            strengths=strengths,
            weaknesses=weaknesses,
            recommendations=recommendations,
            next_parameters={},  # leave to the planner/optimizer
            iteration_complete=approved,
            strategy=strategy,
        )
