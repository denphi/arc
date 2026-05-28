"""Reflector alternates: skill-extracting + failure-clustering.

Both alternates inherit the default ``ReflectorAgent`` so the bookkeeping
side effects (history append, next-parameters extraction, returned
lessons dict) must remain identical — the strategy slot can swap one
for another with no caller change.

The skill-extracting variant adds a *file write* under the session
directory. The failure-clustering variant adds an in-memory clusters
dict reachable via ``context.memory["failure_clusters"]``.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult


pytestmark = pytest.mark.chat


# ── Helpers ─────────────────────────────────────────────────────────────


def _ctx(memory=None, session_id="test-session-abc123"):
    return SimpleNamespace(
        session_id=session_id,
        iteration=0,
        memory=dict(memory or {}),
    )


def _exec(outputs=None, status="completed", metrics=None):
    return ExecutionResult(
        run_id="run-1",
        status=status,
        outputs=outputs if outputs is not None else {"bandgap_ev": 1.1},
        logs=[],
        metrics=metrics or {},
    )


def _review(**kw):
    defaults = dict(
        approved=False,
        summary="not yet at target",
        strengths=[],
        weaknesses=[],
        recommendations=[],
    )
    defaults.update(kw)
    return ReviewResult(**defaults)


def _resolve(name):
    """Load a strategy by name via the resolver — the way the loop does it."""
    from arc.core.strategies import resolve_role
    return resolve_role("reflector", overrides={"reflector": name})


# ── Strategy resolver lookup ───────────────────────────────────────────


def test_default_reflector_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("reflector").__name__ == "ReflectorAgent"


def test_skill_extracting_resolves():
    assert _resolve("skill_extracting").__name__ == "SkillExtractingReflectorAgent"


def test_failure_clustering_resolves():
    assert _resolve("failure_clustering").__name__ == "FailureClusteringReflectorAgent"


# ── Contract compatibility ─────────────────────────────────────────────


def test_skill_extracting_run_returns_same_lessons_shape():
    """Same keys as the default reflector so downstream consumers
    (ReflectionPhase, provenance) keep working."""
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "x"})
    lessons = asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    for key in ("approved", "strengths", "weaknesses",
                "recommendations", "next_parameters", "should_continue"):
        assert key in lessons


def test_failure_clustering_run_returns_same_lessons_shape():
    cls = _resolve("failure_clustering")
    ctx = _ctx()
    lessons = asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    for key in ("approved", "strengths", "weaknesses",
                "recommendations", "next_parameters", "should_continue"):
        assert key in lessons


def test_skill_extracting_appends_to_run_history():
    """Parent bookkeeping survives — run_history must grow."""
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "study x"})
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec(metrics={"thickness": 5.0})))
    assert len(ctx.memory["run_history"]) == 1
    assert ctx.memory["run_history"][0]["outputs"] == {"bandgap_ev": 1.1}


def test_failure_clustering_appends_to_run_history():
    cls = _resolve("failure_clustering")
    ctx = _ctx()
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec(metrics={"thickness": 5.0})))
    assert len(ctx.memory["run_history"]) == 1


# ── Skill-extracting reflector: file output ────────────────────────────


@pytest.fixture
def tmp_session_root(tmp_path, monkeypatch):
    """Resolve session storage to a tmp directory so the skill write
    is sandboxed and doesn't pollute the user's home.

    The repo's conftest already sets ``SIM2L_HOME`` per-test; we honour
    that location rather than fight it.
    """
    return Path(os.environ["SIM2L_HOME"])


def _learned_dir(tmp_session_root, session_id):
    return tmp_session_root / session_id / "skills" / "learned"


def test_skill_extracting_writes_file_on_approval(tmp_session_root):
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "design silicon nanowire"},
               session_id="test-session-write")
    review = _review(
        approved=True,
        summary="hit target",
        strengths=["execution completed", "bandgap matched"],
    )
    lessons = asyncio.run(cls(context=ctx).run(review, execution=_exec()))

    learned = _learned_dir(tmp_session_root, "test-session-write")
    files = list(learned.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "learned_skill" in body
    assert "design silicon nanowire" in body
    assert "approved" in body
    assert "execution completed" in body
    # Returned dict carries a pointer back to the file.
    assert lessons.get("skill_file") == str(files[0])


def test_skill_extracting_writes_file_on_actionable_failure(tmp_session_root):
    """Not approved + weaknesses + recommendations → still worth a skill."""
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "study x"}, session_id="test-actionable")
    review = _review(
        approved=False,
        weaknesses=["bandgap missed by 30%"],
        recommendations=["lower temperature"],
    )
    asyncio.run(cls(context=ctx).run(review, execution=_exec()))

    files = list(_learned_dir(tmp_session_root, "test-actionable").glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "bandgap missed by 30%" in body
    assert "lower temperature" in body


def test_skill_extracting_skips_empty_review(tmp_session_root):
    """Not approved AND no weaknesses/recommendations → nothing actionable,
    no file written."""
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "study x"}, session_id="test-empty")
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    learned = _learned_dir(tmp_session_root, "test-empty")
    if learned.exists():
        assert list(learned.glob("*.md")) == []


def test_skill_extracting_filename_is_stable_across_runs(tmp_session_root):
    """Identical reviews collide on the same filename (idempotent write)."""
    cls = _resolve("skill_extracting")
    ctx = _ctx({"primary_goal": "design x"}, session_id="test-stable")
    review = _review(
        approved=True,
        summary="hit target",
        strengths=["good"],
    )
    asyncio.run(cls(context=ctx).run(review, execution=_exec()))
    asyncio.run(cls(context=ctx).run(review, execution=_exec()))

    files = list(_learned_dir(tmp_session_root, "test-stable").glob("*.md"))
    assert len(files) == 1


def test_skill_extracting_swallows_disk_errors(monkeypatch):
    """If the skill write raises, the reflector must still return a
    valid lessons dict — never crash the loop."""
    cls = _resolve("skill_extracting")

    # Force the write to fail at the point of write_text — every other
    # part of the path resolution still works, so we exercise the
    # try/except around the file write specifically.
    real_write_text = Path.write_text

    def _explode(self, *args, **kwargs):
        if self.suffix == ".md" and "learned" in str(self):
            raise PermissionError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _explode)

    ctx = _ctx({"primary_goal": "x"}, session_id="test-baddisk")
    lessons = asyncio.run(cls(context=ctx).run(
        _review(approved=True, strengths=["ok"]),
        execution=_exec(),
    ))
    # Default lessons keys survived; no skill_file because the write failed.
    assert "approved" in lessons
    assert "skill_file" not in lessons


# ── Failure-clustering reflector ───────────────────────────────────────


def test_failure_clustering_groups_failed_status():
    """Two failed runs with the same error line → one cluster of count 2."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({"run_history": [
        {"inputs": {"x": 1}, "outputs": {}, "status": "failed",
         "error": "DivergenceError: SCF did not converge"},
        {"inputs": {"x": 2}, "outputs": {}, "status": "failed",
         "error": "DivergenceError: SCF did not converge"},
        {"inputs": {"x": 3}, "outputs": {"y": 1.0}, "status": "completed"},
    ]})
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    clusters = ctx.memory.get("failure_clusters")
    assert clusters
    # The shared SCF failure dominates.
    top = clusters[0]
    assert top["count"] == 2
    assert "did not converge" in top["signature"] or "scf" in top["signature"].lower()


def test_failure_clustering_groups_all_nan_outputs():
    """Completed runs whose only numeric outputs are NaN form their own cluster."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({"run_history": [
        {"inputs": {"x": 1}, "outputs": {"y": float("nan")}, "status": "completed"},
        {"inputs": {"x": 2}, "outputs": {"y": float("nan")}, "status": "completed"},
    ]})
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    clusters = ctx.memory.get("failure_clusters")
    assert clusters
    assert clusters[0]["signature"] == "all-numeric-outputs-nan"


def test_failure_clustering_groups_far_from_target():
    """Runs whose every target key is >50% off form a single cluster."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "run_history": [
            {"inputs": {"x": 1}, "outputs": {"bandgap_ev": 3.0}, "status": "completed"},
            {"inputs": {"x": 2}, "outputs": {"bandgap_ev": 0.1}, "status": "completed"},
        ],
    })
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    clusters = ctx.memory.get("failure_clusters")
    assert clusters
    assert clusters[0]["signature"] == "far-from-target"
    assert clusters[0]["count"] == 2


def test_failure_clustering_skips_healthy_runs():
    """If all recent runs hit the target, no clusters are recorded."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "run_history": [
            {"inputs": {"x": 1}, "outputs": {"bandgap_ev": 1.10}, "status": "completed"},
            {"inputs": {"x": 2}, "outputs": {"bandgap_ev": 1.12}, "status": "completed"},
        ],
    })
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    assert "failure_clusters" not in ctx.memory


def test_failure_clustering_clears_stale_clusters():
    """If prior iteration set failure_clusters but recent runs are healthy,
    the stale entry must be removed."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({
        "target": {"bandgap_ev": 1.1},
        "failure_clusters": [{"signature": "old", "count": 5, "reason": "x", "entries": []}],
        "run_history": [
            {"inputs": {"x": 1}, "outputs": {"bandgap_ev": 1.10}, "status": "completed"},
        ],
    })
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    assert "failure_clusters" not in ctx.memory


def test_failure_clustering_sorted_by_size():
    """When there are multiple clusters, the biggest is first."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({"run_history": [
        # 3× SCF failure
        {"inputs": {"x": 1}, "outputs": {}, "status": "failed",
         "error": "SCF did not converge"},
        {"inputs": {"x": 2}, "outputs": {}, "status": "failed",
         "error": "SCF did not converge"},
        {"inputs": {"x": 3}, "outputs": {}, "status": "failed",
         "error": "SCF did not converge"},
        # 1× different failure
        {"inputs": {"x": 4}, "outputs": {}, "status": "failed",
         "error": "Memory exhausted"},
    ]})
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    clusters = ctx.memory["failure_clusters"]
    assert len(clusters) == 2
    assert clusters[0]["count"] == 3
    assert clusters[1]["count"] == 1


def test_failure_clustering_caps_entries_per_cluster():
    """Clusters expose at most 3 example entries so the UI doesn't drown.

    The parent reflector appends *this* execution to history and trims
    to the last 10 entries, so a 10-failure seed plus our healthy
    execution leaves 9 failures in the cluster — that's fine; the
    assertion is about the entries cap, not the absolute count.
    """
    cls = _resolve("failure_clustering")
    history = [
        {"inputs": {"x": i}, "outputs": {}, "status": "failed",
         "error": "same error"}
        for i in range(10)
    ]
    ctx = _ctx({"run_history": history})
    asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    clusters = ctx.memory["failure_clusters"]
    assert clusters[0]["count"] >= 8  # most of the failed seed entries
    assert len(clusters[0]["entries"]) == 3


def test_failure_clusters_also_surfaced_in_lessons():
    """The dict returned to the loop carries the clusters too, not just memory."""
    cls = _resolve("failure_clustering")
    ctx = _ctx({"run_history": [
        {"inputs": {"x": 1}, "outputs": {}, "status": "failed",
         "error": "SCF did not converge"},
    ]})
    lessons = asyncio.run(cls(context=ctx).run(_review(), execution=_exec()))
    assert "failure_clusters" in lessons
    assert lessons["failure_clusters"][0]["count"] == 1
