"""ComparativeReviewerAgent — deterministic relative-improvement reviewer.

Drop-in replacement for ``ReviewerAgent`` that never calls an LLM. Each
test pins one branch of the verdict matrix:

  status≠completed    → not approved, weakness flags failure
  no output match     → not approved, weakness flags it
  target met          → approved + strategy=stop
  first finite run    → not approved, "baseline" strength
  improvement ≥ 1%    → not approved, "improved" strength
  marginal (<1%)      → "marginal" + "below threshold" weakness
  regression          → not approved, regression weakness
  stagnation streak   → strategy=explore

Plus contract-compat checks (same ReviewResult shape, no LLM calls)
and strategy-resolver wiring.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult


pytestmark = pytest.mark.chat


def _ctx(memory=None):
    return SimpleNamespace(
        session_id="test-cmp-reviewer",
        iteration=0,
        memory=dict(memory or {}),
    )


def _exec(outputs=None, status="completed", metrics=None):
    return ExecutionResult(
        run_id="run-1",
        status=status,
        outputs=outputs if outputs is not None else {"bandgap_ev": 1.5},
        logs=[],
        metrics=metrics or {"thickness": 5.0},
    )


def _cls():
    """Resolve the comparative reviewer class through the public resolver
    so the test exercises the registration as well."""
    from arc.core.strategies import resolve_role
    return resolve_role("reviewer", overrides={"reviewer": "comparative"})


# ── Strategy resolver wiring ──────────────────────────────────────────


def test_resolver_returns_comparative_class():
    assert _cls().__name__ == "ComparativeReviewerAgent"


def test_default_reviewer_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("reviewer").__name__ == "ReviewerAgent"


# ── Contract compatibility ────────────────────────────────────────────


def test_returns_review_result_shape():
    review = asyncio.run(_cls()(context=_ctx()).run(_exec()))
    assert isinstance(review, ReviewResult)


def test_does_not_call_provider(monkeypatch):
    """Pin the 'no LLM' guarantee — even a provider on memory must not
    be consulted."""
    called: list = []

    class _FailProvider:
        async def complete_structured(self, *a, **kw):
            called.append(("complete_structured", a, kw))
            raise AssertionError("Provider must not be called")
        async def complete(self, *a, **kw):
            called.append(("complete", a, kw))
            raise AssertionError("Provider must not be called")

    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "provider": _FailProvider()})
    asyncio.run(_cls()(context=ctx).run(_exec()))
    assert called == []


# ── Failure short-circuits ────────────────────────────────────────────


def test_status_failed_returns_not_approved():
    review = asyncio.run(_cls()(context=_ctx({"target": {"bandgap_ev": 1.1}})).run(
        _exec(status="failed", outputs={"bandgap_ev": 0.5}),
    ))
    assert review.approved is False
    assert "step" == review.strategy
    assert any("failed" in w.lower() for w in review.weaknesses)


def test_no_outputs_returns_not_approved():
    review = asyncio.run(_cls()(context=_ctx({"target": {"bandgap_ev": 1.1}})).run(
        _exec(outputs={}),
    ))
    assert review.approved is False
    assert review.iteration_complete is False
    assert review.weaknesses


def test_no_target_key_match_flags_weakness():
    """A target that doesn't appear in outputs produces a clear weakness."""
    review = asyncio.run(_cls()(context=_ctx({"target": {"bandgap_ev": 1.1}})).run(
        _exec(outputs={"glass_transition_k": 450.0}),
    ))
    assert review.approved is False
    assert any("did not match" in w.lower() or "match" in w.lower()
               or "no output key" in w.lower() for w in review.weaknesses)


# ── Verdict matrix ────────────────────────────────────────────────────


def test_target_met_approves_and_stops():
    """Output within 0.25% of target → approved + strategy=stop."""
    ctx = _ctx({"target": {"bandgap_ev": 1.1}})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.1})))
    assert review.approved is True
    assert review.strategy == "stop"
    assert review.iteration_complete is True
    assert any("Target reached" in s for s in review.strengths)


def test_first_finite_run_logs_baseline():
    """No prior runs → fitness is the baseline; not approved unless on target."""
    ctx = _ctx({"target": {"bandgap_ev": 1.1}})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.5})))
    assert review.approved is False
    assert any("baseline" in s.lower() for s in review.strengths)


def test_meaningful_improvement_strengthens():
    """Prior best=1.5, current=1.2 → ~20% better (was 36% off vs 9% off)."""
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.5}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.2})))
    assert review.approved is False
    assert any("Improved" in s for s in review.strengths)
    # Not marginal — no "below threshold" weakness.
    assert not any("threshold" in w.lower() for w in review.weaknesses)


def test_marginal_improvement_flags_weakness():
    """Improvement smaller than 1% relative → marginal."""
    # Prior fitness: |1.500 - 1.1|/1.1 = 0.3636...
    # Current fitness: |1.499 - 1.1|/1.1 = 0.3627...
    # Improvement: ~0.25%, below 1% threshold.
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.500}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.499})))
    assert review.approved is False
    assert any("Marginal" in s for s in review.strengths)
    assert any("threshold" in w.lower() or "noise" in w.lower()
               for w in review.weaknesses)


def test_regression_logs_weakness():
    """Current run is worse than the prior best → regression weakness."""
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.2}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.5})))
    assert review.approved is False
    assert any("Regression" in w for w in review.weaknesses)


# ── Stagnation ────────────────────────────────────────────────────────


def test_stagnation_streak_switches_strategy_to_explore():
    """3+ consecutive non-improving runs → strategy switches to explore."""
    # 3 prior runs at the same fitness (no progress), current also same.
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.50}, "status": "completed"},
        {"inputs": {}, "outputs": {"bandgap_ev": 1.50}, "status": "completed"},
        {"inputs": {}, "outputs": {"bandgap_ev": 1.50}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.50})))
    assert review.strategy == "explore"
    assert any("non-improving" in r.lower() or "explore" in r.lower()
               for r in review.recommendations)


def test_improvement_resets_streak():
    """Improving on the prior best clears the stagnation streak."""
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.50}, "status": "completed"},
        {"inputs": {}, "outputs": {"bandgap_ev": 1.50}, "status": "completed"},
        # Improvement here resets the streak before the current run.
        {"inputs": {}, "outputs": {"bandgap_ev": 1.30}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.25})))
    # Current improved on the new prior best → strategy stays at step.
    assert review.strategy == "step"


# ── Summary line ──────────────────────────────────────────────────────


def test_summary_includes_fitness_and_improvement_numbers():
    history = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.5}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(_cls()(context=ctx).run(_exec(outputs={"bandgap_ev": 1.2})))
    assert "fit=" in review.summary
    assert "improvement=" in review.summary
    assert "streak=" in review.summary


# ── Helpers ───────────────────────────────────────────────────────────


def test_relative_improvement_handles_infinite_prior():
    from arc.packages.arc_sim2l_agents.reviewer_comparative import (
        _relative_improvement,
    )
    # No prior → any finite current counts as infinite improvement.
    assert _relative_improvement(0.5, float("inf")) == float("inf")
    # Both infinite → 0.
    assert _relative_improvement(float("inf"), float("inf")) == 0.0


def test_relative_improvement_handles_zero_prior():
    from arc.packages.arc_sim2l_agents.reviewer_comparative import (
        _relative_improvement,
    )
    # Even at best_prior=0, we shouldn't divide by zero.
    result = _relative_improvement(0.5, 0.0)
    assert result < 0  # regression — current is worse than 0.
