"""Reuse fit-scoring for catalog candidates.

Given a research goal and a ranked list of catalog hits (from the Searcher
role), this module assigns each hit a **fit score** in ``[0, 1]`` describing
how well an *already-deployed* artifact matches what the user is asking for.
The chat loop uses the score to decide whether to (a) reuse a catalog
artifact as-is, (b) adapt the closest one, or (c) build from scratch — and
only prompts the user when at least one hit clears a configurable threshold.

The score blends two signals:

  * **schema overlap** — fraction of the goal's target output keys the
    candidate already produces (deterministic, no provider needed). This is
    the strongest signal: an artifact that doesn't emit the quantity you
    want is rarely a good reuse no matter how related its prose is.
  * **semantic similarity** — how close the candidate's capability summary /
    description is to the goal text. Uses the provider's ``embed`` when
    available, otherwise a TF-cosine bag-of-words. Either way it's bounded
    in ``[0, 1]`` so the blend is stable.

Everything degrades gracefully: no provider → TF cosine; no target keys →
semantic only; no capability text → name + schema keys. The scorer never
raises into the loop.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from arc.schemas.research import ResearchGoal

logger = logging.getLogger(__name__)


# Weights for the blended score. Schema overlap dominates because emitting
# the requested quantity is the precondition for reuse; semantic similarity
# breaks ties between candidates that all (or none) match the schema.
_W_SCHEMA = 0.6
_W_SEMANTIC = 0.4

# Default fit threshold: below this, a hit isn't worth offering for reuse.
# Tunable via ARC_REUSE_THRESHOLD on the context config.
DEFAULT_REUSE_THRESHOLD = 0.45


_STOP = {
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by",
    "via", "with", "using", "that", "this", "is", "are", "be", "i", "want",
    "would", "like", "could", "should", "as", "on", "if", "it", "we", "you",
}


def _tokens(text: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    return [w for w in words if w not in _STOP and len(w) > 2]


def _tf(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    n = len(tokens)
    return {t: c / n for t, c in counts.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[k] * b[k] for k in common)
    da = sum(v * v for v in a.values()) ** 0.5
    db = sum(v * v for v in b.values()) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _flatten_keys(value: Any) -> set[str]:
    """Lowercased, underscore-stripped key set for loose schema matching."""
    if not isinstance(value, dict):
        return set()
    return {str(k).replace("_", "").lower() for k in value}


def candidate_text(hit: dict) -> str:
    """Best available descriptive text for a candidate, for semantic scoring.

    Prefers the curator-authored capability summary; falls back to the
    description, then name + schema keys, so a catalog entry written before
    the capability field existed still scores sensibly.
    """
    meta = hit.get("metadata") or {}
    cap = meta.get("capability") if isinstance(meta, dict) else None
    parts: list[str] = []
    if isinstance(cap, dict):
        parts.append(str(cap.get("summary") or ""))
        parts.extend(str(c) for c in (cap.get("capabilities") or []))
        parts.extend(str(t) for t in (cap.get("domain_tags") or []))
    parts.append(str(hit.get("description") or ""))
    parts.append(str(hit.get("name") or ""))
    parts.extend(str(k) for k in (hit.get("input_schema") or {}))
    parts.extend(str(k) for k in (hit.get("output_schema") or {}))
    parts.extend(str(t) for t in (hit.get("tags") or []))
    return " ".join(p for p in parts if p)


def _schema_overlap(target: dict, hit: dict) -> float | None:
    """Fraction of target output keys the candidate already emits.

    Returns ``None`` when there's no target to score against (the caller then
    relies on semantic similarity alone). Matching is loose — keys are
    compared after lowercasing and stripping underscores — so ``bandgap_eV``
    matches ``band_gap_ev``.
    """
    if not target:
        return None
    out_keys = _flatten_keys(hit.get("output_schema") or {})
    if not out_keys:
        return 0.0
    target_keys = {str(k).replace("_", "").lower() for k in target}
    if not target_keys:
        return None
    matched = sum(1 for k in target_keys if k in out_keys)
    return matched / len(target_keys)


async def _semantic_similarity(goal_text: str, hit: dict, provider) -> float:
    """Similarity of goal text to the candidate's descriptive text, in [0,1].

    Uses provider embeddings when available; otherwise TF-cosine. Both are
    clamped to [0, 1] (a cosine can be slightly negative for embeddings).
    """
    doc = candidate_text(hit)
    embed = getattr(provider, "embed", None) if provider else None
    if embed is not None and callable(embed):
        try:
            import asyncio

            async def _vec(text: str) -> list[float]:
                r = embed(text)
                if asyncio.iscoroutine(r):
                    r = await r
                if hasattr(r, "tolist"):
                    r = r.tolist()
                return [float(x) for x in (r or [])]

            gv, dv = await _vec(goal_text), await _vec(doc)
            if gv and dv:
                n = min(len(gv), len(dv))
                da = sum(v * v for v in gv) ** 0.5
                db = sum(v * v for v in dv) ** 0.5
                if da and db:
                    cos = sum(gv[i] * dv[i] for i in range(n)) / (da * db)
                    return max(0.0, min(1.0, cos))
        except Exception:  # noqa: BLE001 — fall through to TF
            logger.debug("embedding similarity failed; using TF", exc_info=True)
    return max(0.0, min(1.0, _cosine(_tf(_tokens(goal_text)), _tf(_tokens(doc)))))


async def score_fit(
    goal: ResearchGoal,
    hits: list[dict],
    provider=None,
) -> list[dict]:
    """Return hits annotated with a ``fit`` score, sorted best-first.

    Each returned dict is the original hit plus:

      * ``fit``       — blended score in [0, 1]
      * ``fit_parts`` — ``{"schema": float|None, "semantic": float}`` for
                        display / debugging

    Pure scoring — no I/O beyond the optional embedding call, and never
    raises into the caller (a per-hit failure scores 0 for that hit).
    """
    scored: list[dict] = []
    for hit in hits:
        try:
            schema = _schema_overlap(goal.target, hit)
            semantic = await _semantic_similarity(goal.goal, hit, provider)
            if schema is None:
                fit = semantic
            else:
                fit = _W_SCHEMA * schema + _W_SEMANTIC * semantic
            annotated = dict(hit)
            annotated["fit"] = round(fit, 4)
            annotated["fit_parts"] = {"schema": schema, "semantic": round(semantic, 4)}
            scored.append(annotated)
        except Exception as exc:  # noqa: BLE001 — one bad hit can't break ranking
            logger.debug("fit scoring failed for %s: %s", hit.get("name"), exc)
            annotated = dict(hit)
            annotated["fit"] = 0.0
            annotated["fit_parts"] = {"schema": None, "semantic": 0.0}
            scored.append(annotated)
    scored.sort(key=lambda h: h.get("fit", 0.0), reverse=True)
    return scored


def reuse_threshold(context) -> float:
    """Resolve the fit threshold from context config, with a safe default."""
    try:
        cfg = getattr(context, "config", None) or {}
        return float(cfg.get("ARC_REUSE_THRESHOLD", DEFAULT_REUSE_THRESHOLD))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_REUSE_THRESHOLD
