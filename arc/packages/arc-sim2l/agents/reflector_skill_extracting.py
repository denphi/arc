"""Reflector that persists learned heuristics as markdown skills.

Drop-in replacement for ``ReflectorAgent``. Does everything the default
reflector does (history bookkeeping, next-parameters extraction,
provenance recording) **plus** writes a per-run markdown skill into
``~/.sim2l/<session-id>/skills/learned/`` so the lesson survives the
session and can be re-loaded by a future chat.

What gets written
-----------------

Actionable failures are stored as non-invocable candidate records. An approved
review promotes a reusable Agent Skills-style bundle under ``skills/learned``.
When a prior candidate exists for the same goal, the promoted skill records it
as evidence that the recommendation was followed by a successful run.

Candidates are JSON records containing the failed review and recommendations.
Promoted bundles contain validated procedures plus frontmatter evidence linking
the approved iteration to the candidate it corrected. The dict returned
upstream keeps the default reflector's keys and adds a candidate or skill path.

Why session-scoped (not workspace-scoped)
-----------------------------------------

Skills are tied to the question the user was asking. Two parallel
sessions on different goals would clobber each other if the path were
``workspace/skills/``. We anchor at ``~/.sim2l/<session-id>/`` so each
session owns its learnings; the existing import/export surfaces copy bundles
across sessions and shared libraries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from arc.packages.arc_sim2l_agents.reflector import ReflectorAgent
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult

logger = logging.getLogger(__name__)


def _slugify(text: str, *, maxlen: int = 40) -> str:
    """Lower-case, kebab-case slug suitable for a filename."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:maxlen] or "lesson").strip("-")


def _short_hash(payload: dict) -> str:
    """8-char stable hash so two reviews with identical content collide
    (and overwrite the same file) rather than fragmenting."""
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _is_worth_writing(review: ReviewResult) -> bool:
    """Skip reviews that have nothing actionable.

    Writing a file for every neutral "execution completed but I had
    nothing to say" review would litter the directory; we restrict to
    approvals (the recipe worked!) and explicit weakness+recommendation
    pairs (here's what failed and what to try next).
    """
    if review.approved:
        return True
    return bool(review.weaknesses) and bool(review.recommendations)


def _session_skill_dirs(session_id: str) -> tuple[Path, Path] | None:
    """Resolve the candidate and learned directories for a session.

    Falls back to ``None`` if the session-paths helper raises (e.g.
    session-id sanitisation rejected the input) — the reflector should
    never crash because of a disk-side detail.
    """
    try:
        from arc.session import session_paths
        paths = session_paths(session_id)
        # session_paths exposes individual paths, not the root — pick
        # any one and walk up to the session dir.
        base = Path(paths["artifacts"]).parent
        candidates = base / "skills" / "candidates"
        learned = base / "skills" / "learned"
        candidates.mkdir(parents=True, exist_ok=True)
        learned.mkdir(parents=True, exist_ok=True)
        return candidates, learned
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not resolve skills dir for session %s: %s", session_id, exc)
        return None


def _candidate_payload(
    *,
    session_id: str,
    iteration: int,
    primary_goal: str,
    review: ReviewResult,
    next_parameters: dict[str, Any],
) -> dict[str, Any]:
    """Build a durable but non-invocable lesson candidate."""
    slug = _slugify(primary_goal or review.summary or "lesson")
    payload = {
        "kind": "skill_candidate",
        "name": slug,
        "session_id": session_id,
        "iteration": iteration,
        "goal": primary_goal,
        "approved": False,
        "summary": review.summary,
        "strengths": list(review.strengths),
        "weaknesses": list(review.weaknesses),
        "recommendations": list(review.recommendations),
        "next_parameters": next_parameters,
    }
    payload["candidate_id"] = _short_hash({
        "goal": primary_goal,
        "summary": review.summary,
        "weaknesses": review.weaknesses,
        "recommendations": review.recommendations,
        "next_parameters": next_parameters,
    })
    return payload


def _matching_candidate(
    candidates_dir: Path,
    primary_goal: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest actionable failure candidate for this goal."""
    matches: list[tuple[float, Path, dict[str, Any]]] = []
    for path in candidates_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("goal") == primary_goal and not payload.get("promoted_to"):
                matches.append((path.stat().st_mtime, path, payload))
        except (OSError, ValueError):
            continue
    if not matches:
        return None
    _, path, payload = max(matches, key=lambda item: item[0])
    return path, payload


def _render_promoted_skill(
    *,
    session_id: str,
    iteration: int,
    primary_goal: str,
    review: ReviewResult,
    next_parameters: dict[str, Any],
    candidate: dict[str, Any] | None,
) -> tuple[str, str]:
    """Render a portable, evidence-bearing ``SKILL.md``."""
    slug = _slugify(primary_goal or review.summary or "lesson")
    evidence = {
        "session_id": session_id,
        "success_iteration": iteration,
        "success_summary": review.summary,
        "candidate_id": candidate.get("candidate_id") if candidate else None,
        "failure_iteration": candidate.get("iteration") if candidate else None,
    }
    short = _short_hash({"goal": primary_goal, "evidence": evidence})
    name = f"{slug}-{short}"
    description = (
        f"Validated research lesson for {primary_goal or slug}; promoted after an approved run."
    )
    frontmatter = {
        "name": name,
        "description": description[:1024],
        "compatibility": "ARC research workflows",
        "metadata": {"arc": {"evidence": evidence, "validated": True}},
    }
    recommendations = (
        list(candidate.get("recommendations") or []) if candidate else list(review.recommendations)
    )
    weaknesses = list(candidate.get("weaknesses") or []) if candidate else []
    lines = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).rstrip(),
        "---",
        "",
        f"# {name}",
        "",
        "## When to use",
        f"Use this validated lesson when working on: {primary_goal or slug}.",
        "",
        "## Validated procedure",
    ]
    lines.extend(f"- {item}" for item in recommendations or review.strengths)
    if weaknesses:
        lines.extend(["", "## Failure this corrected"])
        lines.extend(f"- {item}" for item in weaknesses)
    lines.extend([
        "",
        "## Success evidence",
        f"- Session: `{session_id}`",
        f"- Approved iteration: {iteration}",
        f"- Review summary: {review.summary or '(none)'}",
    ])
    if next_parameters:
        lines.extend([
            "",
            "## Suggested parameters",
            "```json",
            json.dumps(next_parameters, indent=2, default=str),
            "```",
        ])
    return name, "\n".join(lines) + "\n"


class SkillExtractingReflectorAgent(ReflectorAgent):
    """Reflector that stores failure candidates and promotes approved lessons.

    Inherits the default reflector's bookkeeping (run_history, next
    parameters, provenance) — overrides ``run`` to add the skill-file
    write side effect after the parent returns the lessons dict.
    """

    name = "skill_extracting_reflector"
    description = (
        "Default reflector behaviour + stores failed-review candidates and "
        "promotes approved lessons into portable skill bundles."
    )

    async def run(
        self,
        input_data: ReviewResult,
        execution: ExecutionResult | None = None,
    ) -> dict[str, Any]:
        lessons = await super().run(input_data, execution=execution)

        # Reload as a ReviewResult for type-safe attribute access; the
        # parent already accepted dict-shaped input via the same trick.
        review = (
            input_data
            if isinstance(input_data, ReviewResult)
            else ReviewResult(**input_data)
        )

        if not _is_worth_writing(review):
            return lessons

        target_dirs = _session_skill_dirs(self.context.session_id)
        if target_dirs is None:
            return lessons
        candidates_dir, learned_dir = target_dirs

        try:
            primary_goal = str(self.context.memory.get("primary_goal") or "")
            iteration = int(getattr(self.context, "iteration", 0) or 0)
            lessons = dict(lessons)
            if review.approved:
                candidate_match = _matching_candidate(candidates_dir, primary_goal)
                candidate_path, candidate = candidate_match or (None, None)
                name, body = _render_promoted_skill(
                    session_id=self.context.session_id,
                    iteration=iteration,
                    primary_goal=primary_goal,
                    review=review,
                    next_parameters=lessons.get("next_parameters") or {},
                    candidate=candidate,
                )
                bundle_dir = learned_dir / name
                bundle_dir.mkdir(parents=True, exist_ok=True)
                skill_path = bundle_dir / "SKILL.md"
                skill_path.write_text(body, encoding="utf-8")
                lessons["skill_file"] = str(skill_path)
                lessons["skill_evidence"] = (
                    candidate.get("candidate_id") if candidate else "approved_run"
                )
                if candidate is not None and candidate_path is not None:
                    candidate["promoted_to"] = name
                    candidate["promoted_iteration"] = iteration
                    candidate_path.write_text(
                        json.dumps(candidate, indent=2, sort_keys=True, default=str),
                        encoding="utf-8",
                    )
            else:
                candidate = _candidate_payload(
                    session_id=self.context.session_id,
                    iteration=iteration,
                    primary_goal=primary_goal,
                    review=review,
                    next_parameters=lessons.get("next_parameters") or {},
                )
                candidate_filename = (
                    f"{candidate['name']}-{candidate['candidate_id']}.json"
                )
                candidate_path = candidates_dir / candidate_filename
                candidate_path.write_text(
                    json.dumps(candidate, indent=2, sort_keys=True, default=str),
                    encoding="utf-8",
                )
                lessons["skill_candidate_file"] = str(candidate_path)
        except Exception as exc:  # noqa: BLE001 — skill write is best-effort
            logger.warning("Failed to write learned skill: %s", exc)

        return lessons
