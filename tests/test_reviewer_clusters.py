"""ReflectiveReviewer consumes ``memory['failure_clusters']``.

The failure_clustering reflector stamps a clusters list onto memory at
the end of each iteration. On the *next* iteration the reflective
reviewer reads it before deciding ``strategy``. A dominant cluster
(≥ 2 entries of the same signature) escalates the strategy to
``"explore"`` even when the linear stagnation streak hasn't tripped.

This file pins six things:

  * No clusters in memory → existing strategy decision unchanged.
  * Single-entry cluster (below dominance threshold) → unchanged.
  * Dominant cluster → strategy switches to ``"explore"``.
  * Weakness + recommendation strings mention the cluster's reason
    so the chat surfaces *what* is failing systemically.
  * Stagnation still works on its own when no clusters are present.
  * Approval still beats every other signal — clusters never block
    a target hit.

Also covers the end-to-end recipe wire-up via the ``failure-aware``
recipe so /recipe apply does what users expect.
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
        session_id="test-clusters",
        iteration=0,
        memory=dict(memory or {}),
    )


def _exec(outputs=None, status="completed", metrics=None):
    return ExecutionResult(
        run_id="run-1",
        status=status,
        outputs=outputs if outputs is not None else {"bandgap_ev": 1.5},
        logs=[],
        metrics=metrics or {"thickness": 5.0, "temperature": 300.0},
    )


def _reflective_cls():
    """Strategy resolver returns the reflective reviewer class."""
    from arc.core.strategies import resolve_role
    return resolve_role("reviewer", overrides={"reviewer": "reflective"})


# ── No clusters → existing behaviour ───────────────────────────────────


def test_no_clusters_strategy_stays_step():
    """First iteration: no clusters, no streak — strategy should be 'step'."""
    cls = _reflective_cls()
    ctx = _ctx({"target": {"bandgap_ev": 1.1}})
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert review.strategy == "step"


def test_single_entry_cluster_does_not_trigger_escalation():
    """One-off failure shouldn't be enough — needs at least 2."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "lone-error", "reason": "scf-fail",
             "count": 1, "entries": [{}]},
        ],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert review.strategy == "step"


# ── Dominant cluster → explore ─────────────────────────────────────────


def test_dominant_cluster_forces_explore():
    """≥ 2 entries in the top cluster → strategy must be 'explore'."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "all-numeric-outputs-nan",
             "reason": "every numeric output was NaN",
             "count": 4, "entries": [{}, {}, {}, {}]},
        ],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert review.strategy == "explore"


def test_cluster_surfaces_reason_in_weaknesses():
    """The reason string should land in weaknesses so the chat shows it."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "all-numeric-outputs-nan",
             "reason": "every numeric output was NaN",
             "count": 3, "entries": [{}, {}, {}]},
        ],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert any("Systemic failure" in w for w in review.weaknesses)
    assert any("3 prior runs" in w for w in review.weaknesses)
    assert any("every numeric output was NaN" in w for w in review.weaknesses)


def test_cluster_surfaces_recommendation():
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "far-from-target", "reason": "far-from-target",
             "count": 3, "entries": [{}, {}, {}]},
        ],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert any(
        "switch parameter regime" in r.lower() or "regime" in r.lower()
        for r in review.recommendations
    )


def test_cluster_escalation_beats_streak_logic():
    """A 2-entry cluster wins even when the linear streak isn't long enough.

    Streak threshold is 3; we set up a 2-entry cluster + no streak data
    and confirm the strategy is still 'explore'.
    """
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "x", "reason": "y", "count": 2, "entries": [{}, {}]},
        ],
        "run_history": [],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    assert review.strategy == "explore"


# ── Existing logic still works ─────────────────────────────────────────


def test_stagnation_path_still_fires_without_clusters():
    """Streak escalation must keep working when nothing seeded clusters."""
    cls = _reflective_cls()
    # 3 prior non-improving runs.
    history = [
        {"inputs": {"x": 1}, "outputs": {"bandgap_ev": 1.5}, "status": "completed"},
        {"inputs": {"x": 2}, "outputs": {"bandgap_ev": 1.5}, "status": "completed"},
        {"inputs": {"x": 3}, "outputs": {"bandgap_ev": 1.5}, "status": "completed"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history})
    review = asyncio.run(cls(context=ctx).run(_exec(outputs={"bandgap_ev": 1.5})))
    assert review.strategy == "explore"


def test_approval_beats_dominant_cluster():
    """Even if there's a dominant cluster, hitting the target wins."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "x", "reason": "y", "count": 5, "entries": [{}]},
        ],
    })
    # Output matches the target exactly.
    review = asyncio.run(cls(context=ctx).run(_exec(outputs={"bandgap_ev": 1.1})))
    assert review.approved is True
    assert review.strategy == "stop"


def test_invalid_cluster_shape_is_silently_ignored():
    """A garbage clusters value shouldn't crash the reviewer."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": "not a list",
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    # Falls through to step; no crash.
    assert review.strategy in ("step", "explore")


def test_cluster_summary_text_format():
    """Sanity check the format of the weakness string — count + reason."""
    cls = _reflective_cls()
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [
            {"signature": "convergence-fail",
             "reason": "SCF did not converge",
             "count": 7, "entries": []},
        ],
    })
    review = asyncio.run(cls(context=ctx).run(_exec()))
    matching = [w for w in review.weaknesses if "Systemic failure" in w]
    assert matching
    assert "7" in matching[0]
    assert "SCF did not converge" in matching[0]


# ── Recipe wiring ─────────────────────────────────────────────────────


def test_failure_aware_recipe_validates():
    """The shipped recipe references only known roles + impls."""
    from arc.core.recipes import get_recipe, validate_recipe

    recipe = get_recipe("failure-aware")
    assert recipe is not None
    assert recipe.strategies["reviewer"] == "reflective"
    assert recipe.strategies["reflector"] == "failure_clustering"
    assert validate_recipe(recipe) == []


def test_failure_aware_recipe_applies_through_resolver(monkeypatch):
    """End-to-end: /recipe apply failure-aware → resolver returns the
    paired reviewer + reflector classes."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.packages import resolve_role
    from tests.fakes import make_workflow

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = ChatState(workflow=make_workflow())
    asyncio.run(run(state, ["apply", "failure-aware"]))

    assert resolve_role("reviewer", state.workflow).__name__ == "ReflectiveReviewerAgent"
    assert resolve_role("reflector", state.workflow).__name__ == "FailureClusteringReflectorAgent"


# ── End-to-end: reflector → reviewer signal flow ──────────────────────


def test_reflector_then_reviewer_passes_clusters_through_memory():
    """Simulate the iteration boundary: reflector writes clusters, the
    next reviewer call sees them via memory and escalates."""
    from arc.core.strategies import resolve_role

    ReflectorCls = resolve_role("reflector", overrides={"reflector": "failure_clustering"})
    ReviewerCls = resolve_role("reviewer", overrides={"reviewer": "reflective"})

    history_with_failures = [
        {"inputs": {"x": 1}, "outputs": {}, "status": "failed",
         "error": "DivergenceError"},
        {"inputs": {"x": 2}, "outputs": {}, "status": "failed",
         "error": "DivergenceError"},
        {"inputs": {"x": 3}, "outputs": {}, "status": "failed",
         "error": "DivergenceError"},
    ]
    ctx = _ctx({"target": {"bandgap_ev": 1.1}, "run_history": history_with_failures})

    # Iteration N: reflector runs at the end of the iteration. We pass a
    # minimal review since the reflector inherits the default behaviour
    # for bookkeeping — what matters here is that it populates clusters.
    asyncio.run(ReflectorCls(context=ctx).run(
        ReviewResult(approved=False, summary="x"),
        execution=_exec(),
    ))
    assert "failure_clusters" in ctx.memory

    # Iteration N+1: the reviewer reads what the reflector left behind.
    review = asyncio.run(ReviewerCls(context=ctx).run(_exec(outputs={"bandgap_ev": 1.5})))
    assert review.strategy == "explore"
    assert any("Systemic failure" in w for w in review.weaknesses)
