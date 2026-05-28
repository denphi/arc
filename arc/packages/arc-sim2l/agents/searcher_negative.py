"""Negative-results searcher.

Drop-in alternative to the default ``KeywordSearcherAgent``. Instead of
surfacing the *best* prior results for a goal, it surfaces the *failed*
ones — runs that errored, produced all-NaN outputs, or landed far from
their target. The ideator's prompt then sees a "here's what didn't
work" block so the LLM can steer clear of known-bad parameter regions.

Why this is a distinct searcher
===============================

The keyword + embedding searchers answer "what's the closest existing
work?" — they bias toward success. But a campaign that already burned
ten runs hitting the same NaN wall benefits from the *opposite* signal:
"these inputs failed, don't propose them again." Splitting that into
its own searcher keeps each ranking strategy single-purpose and lets a
recipe pair it with the others (a future hybrid searcher could union
both).

How it classifies a failure
============================

Same heuristics the failure-clustering reflector uses, so the two
agree on what "failed" means:

  * ``status`` present and not ``completed`` → failed.
  * All numeric outputs are NaN → failed (silent mid-compute crash).
  * A target exists and every matched output is >50% off → far-from-target.

Runs that don't trip any of those are dropped — this searcher only
returns the negatives.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from arc.packages.arc_sim2l_agents.searcher import (
    _BaseSearcher,
    fetch_catalog,
    fetch_prior_results,
    goal_keywords,
)
from arc.schemas.research import ResearchGoal, SearchResult

logger = logging.getLogger(__name__)


_FAR_FROM_TARGET_THRESHOLD = 0.5  # 50% relative error → "far"


# ── Failure classification ──────────────────────────────────────────────


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_failed_run(record: dict, target: dict[str, Any]) -> tuple[bool, str]:
    """Classify a results record. Returns ``(is_failure, reason)``.

    ``record`` is one entry from the results service's ``/search``
    response. We tolerate both the live shape (``input_params`` /
    ``output_params``) and the in-memory shape (``inputs`` / ``outputs``)
    so the same classifier works against either source.
    """
    status = record.get("status")
    outputs = (
        record.get("output_params")
        or record.get("outputs")
        or {}
    )
    if not isinstance(outputs, dict):
        outputs = {}

    if isinstance(status, str) and status and status != "completed":
        return True, f"status={status!r}"

    numeric_values = [
        v for v in (_numeric(x) for x in outputs.values()) if v is not None
    ]
    if numeric_values and all(math.isnan(v) for v in numeric_values):
        return True, "all-numeric-outputs-nan"

    if target:
        far_keys: list[str] = []
        considered = 0
        for tk, tv in target.items():
            tv_num = _numeric(tv)
            if tv_num is None:
                continue
            considered += 1
            ov = outputs.get(tk)
            if ov is None and tk.lower() != tk:
                ov = outputs.get(tk.lower())
            ov_num = _numeric(ov)
            if ov_num is None:
                continue
            rel = abs(ov_num - tv_num) / max(abs(tv_num), 1e-12)
            if rel > _FAR_FROM_TARGET_THRESHOLD:
                far_keys.append(tk)
        if considered and far_keys and len(far_keys) == considered:
            return True, f"far-from-target ({', '.join(far_keys)})"

    return False, ""


def _annotate_failures(
    records: list[dict],
    target: dict[str, Any],
) -> list[dict]:
    """Filter ``records`` to failures and tag each with a ``_failure_reason``."""
    failures: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        is_fail, reason = _is_failed_run(record, target)
        if is_fail:
            tagged = dict(record)
            tagged["_failure_reason"] = reason
            failures.append(tagged)
    return failures


# ── Agent ───────────────────────────────────────────────────────────────


class NegativeResultsSearcherAgent(_BaseSearcher):
    """Searcher that surfaces failed prior runs of similar simulations.

    Returns the catalog candidates (so the ideator still knows the sims
    exist) tagged ``negative_results``, plus a ``prior_results`` list
    that contains *only* the failed runs, each annotated with the
    reason it's considered a failure.
    """

    name = "searcher_negative"
    description = (
        "Surfaces failed prior runs (errors, all-NaN outputs, "
        "far-from-target) of catalog simulations matching the goal, so "
        "the ideator can steer away from known-bad parameter regions. "
        "Returns nothing when no prior failures exist."
    )

    async def search(self, goal: ResearchGoal) -> SearchResult:
        catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
        results_url = os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

        keywords = goal_keywords(goal.goal)
        query = " ".join(keywords[:4])
        # Pull a slightly wider candidate set than the keyword searcher
        # since we'll discard candidates that have no recorded failures.
        # Search is best-effort context for the ideator — a transport
        # failure here must degrade to "no negatives", never crash the
        # loop. fetch_catalog already swallows its own errors; the guard
        # covers any future helper that doesn't.
        try:
            candidates = fetch_catalog(catalog_url, query, limit=10)
        except Exception as exc:  # noqa: BLE001
            logger.debug("negative-results catalog fetch failed: %s", exc)
            candidates = []
        if not candidates:
            return SearchResult(catalog_hits=[], prior_results=[])

        target = dict(goal.target or {})
        kept_hits: list[dict] = []
        all_failures: list[dict] = []

        # For each candidate, fetch a deeper slice of its history and
        # keep only the ones that actually have failures to report.
        for cand in candidates:
            name = cand.get("name", "")
            if not name:
                continue
            recent = fetch_prior_results(results_url, name, limit=10)
            failures = _annotate_failures(recent, target)
            if not failures:
                continue
            hit = dict(cand)
            tags = list(hit.get("tags") or [])
            tags.append("negative_results")
            hit["tags"] = tags
            # Stamp a count so the ideator prompt can say "N prior failures".
            meta = dict(hit.get("metadata") or {})
            meta["negative_result_count"] = len(failures)
            hit["metadata"] = meta
            kept_hits.append(hit)
            all_failures.extend(failures)

        return SearchResult(catalog_hits=kept_hits, prior_results=all_failures)
