"""Reflector that groups recent failed runs by error pattern.

Drop-in replacement for ``ReflectorAgent``. Does everything the default
reflector does (history, next-parameters, provenance) **plus** scans the
accumulated ``run_history`` for failed/non-improving runs, clusters them
by their error signature, and stamps the result onto
``context.memory["failure_clusters"]``.

Why bother?
-----------

When a sweep produces 8 runs and 5 of them fail with the same NaN
output or the same convergence error, the reviewer doesn't notice —
it judges each run in isolation. The clusters dict lets the next
reviewer (especially the ``reflective`` one) say "all your high-T runs
NaN'd; lower the temperature ceiling" instead of just "try different
parameters."

What "error pattern" means
--------------------------

For each historical entry we synthesize a coarse signature:

  * ``status != "completed"`` runs use the first log line (truncated).
  * Completed runs with all-NaN / all-None numeric outputs get
    ``"all-numeric-outputs-nan"``.
  * Completed runs that match a target poorly (>50% off on every key
    where a target exists) get ``"far-from-target"``.

Each cluster carries the entries that share the signature so the chat
or a future LLM consumer can surface the *worst* outliers without
re-deriving the cluster.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any

from arc.packages.arc_sim2l_agents.reflector import ReflectorAgent
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult

logger = logging.getLogger(__name__)


_FAR_FROM_TARGET_THRESHOLD = 0.5  # 50% relative error → "far"


def _numeric_value(value: Any) -> float | None:
    """Coerce a value to float, returning None when not interpretable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _classify_entry(entry: dict, target: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(cluster_key, human_reason)`` for a failed-ish entry, or None.

    Returns None for entries that look healthy — we only cluster things
    worth surfacing.
    """
    status = entry.get("status")
    outputs = entry.get("outputs") or {}

    if status != "completed":
        msg = ""
        for source in ("error", "summary", "log"):
            val = entry.get(source)
            if val:
                msg = str(val).splitlines()[0]
                break
        signature = (msg or f"status-{status or 'unknown'}").strip()[:60]
        return signature, f"status={status!r}: {signature}"

    # Completed but all-numeric outputs are NaN/None → most likely the
    # workflow crashed silently mid-compute.
    numeric_values: list[float] = []
    for v in outputs.values():
        nv = _numeric_value(v)
        if nv is not None:
            numeric_values.append(nv)
    if numeric_values and all(math.isnan(v) for v in numeric_values):
        return "all-numeric-outputs-nan", "every numeric output was NaN"

    # Far-from-target check only fires when the goal had a target.
    if target:
        far_keys: list[str] = []
        for tk, tv in target.items():
            tv_num = _numeric_value(tv)
            if tv_num is None:
                continue
            # Match exact or case-insensitive output keys (cheaper than
            # the reviewer's _keys_match — clusters are best-effort).
            ov = outputs.get(tk)
            if ov is None and tk.lower() != tk:
                ov = outputs.get(tk.lower())
            ov_num = _numeric_value(ov)
            if ov_num is None:
                continue
            rel = abs(ov_num - tv_num) / max(abs(tv_num), 1e-12)
            if rel > _FAR_FROM_TARGET_THRESHOLD:
                far_keys.append(tk)
        if far_keys and len(far_keys) == sum(
            1 for tk in target if _numeric_value(target.get(tk)) is not None
        ):
            return (
                "far-from-target",
                f"all target keys >50% off ({', '.join(far_keys)})",
            )

    return None


def build_failure_clusters(
    history: list[dict],
    target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of clusters sorted by descending size.

    Each cluster: ``{"signature": str, "count": int,
    "reason": str, "entries": [<the worst examples>]}``. The entries
    list is capped at 3 so downstream consumers (and the chat UI) don't
    drown in noise.
    """
    target = target or {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    reasons: dict[str, str] = {}

    for entry in history:
        if not isinstance(entry, dict):
            continue
        classified = _classify_entry(entry, target)
        if classified is None:
            continue
        signature, reason = classified
        grouped[signature].append(entry)
        # Keep the first reason we saw for this signature — they're all
        # equivalent by construction.
        reasons.setdefault(signature, reason)

    clusters = [
        {
            "signature": sig,
            "reason": reasons[sig],
            "count": len(entries),
            "entries": entries[:3],
        }
        for sig, entries in grouped.items()
    ]
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


class FailureClusteringReflectorAgent(ReflectorAgent):
    """Reflector that groups recent failed/non-improving runs by error pattern."""

    name = "failure_clustering_reflector"
    description = (
        "Default reflector behaviour + groups runs in ``run_history`` by "
        "their failure signature (status, all-NaN outputs, far-from-target). "
        "Result lands on ``memory['failure_clusters']`` so the next reviewer "
        "and the chat UI can surface systemic failure modes instead of "
        "treating each failure in isolation."
    )

    async def run(
        self,
        input_data: ReviewResult,
        execution: ExecutionResult | None = None,
    ) -> dict[str, Any]:
        lessons = await super().run(input_data, execution=execution)

        try:
            history = self.context.memory.get("run_history") or []
            target = self.context.memory.get("target") or {}
            clusters = build_failure_clusters(history, target=target)
            if clusters:
                self.context.memory["failure_clusters"] = clusters
                lessons = dict(lessons)
                lessons["failure_clusters"] = clusters
            else:
                # Clear any stale clusters from a prior iteration — if
                # the recent runs are healthy we shouldn't keep telling
                # the next reviewer about week-old failures.
                self.context.memory.pop("failure_clusters", None)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("Failure clustering failed: %s", exc)

        return lessons
