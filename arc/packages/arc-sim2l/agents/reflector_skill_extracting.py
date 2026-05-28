"""Reflector that persists learned heuristics as markdown skills.

Drop-in replacement for ``ReflectorAgent``. Does everything the default
reflector does (history bookkeeping, next-parameters extraction,
provenance recording) **plus** writes a per-run markdown skill into
``~/.sim2l/<session-id>/skills/learned/`` so the lesson survives the
session and can be re-loaded by a future chat.

What gets written
-----------------

For each *approved* review, or each review with weaknesses + concrete
recommendations, we emit a file named ``<slug>-<short-hash>.md`` with
this shape::

    # learned_skill: <slug>
    Generated from session <id>, iteration <n>.

    ## Context
    Goal: <primary_goal>
    Status: <approved | not approved>

    ## What worked
    - <strengths joined>

    ## What didn't
    - <weaknesses joined>

    ## Recommendations for next time
    - <recommendations joined>

    ## Suggested parameters
    <next_parameters as JSON>

The file is the *primary* artifact this reflector adds; the dict we
return upstream is unchanged so every consumer downstream (provenance,
the next iteration's reviewer) keeps working without changes.

Why session-scoped (not workspace-scoped)
-----------------------------------------

Skills are tied to the question the user was asking. Two parallel
sessions on different goals would clobber each other if the path were
``workspace/skills/``. We anchor at ``~/.sim2l/<session-id>/`` so each
session owns its learnings; a future "import skills from session X"
command can copy across.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import textwrap
from pathlib import Path
from typing import Any

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


def _session_skills_dir(session_id: str) -> Path | None:
    """Resolve the ``learned/`` directory for a session, creating it.

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
        target = base / "skills" / "learned"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not resolve skills dir for session %s: %s", session_id, exc)
        return None


def _render_skill(
    *,
    session_id: str,
    iteration: int,
    primary_goal: str,
    review: ReviewResult,
    next_parameters: dict[str, Any],
) -> tuple[str, str]:
    """Render the skill content + a slug for its filename."""
    slug = _slugify(primary_goal or review.summary or "lesson")
    short = _short_hash({
        "goal": primary_goal,
        "summary": review.summary,
        "next_parameters": next_parameters,
    })
    filename = f"{slug}-{short}.md"

    status = "approved" if review.approved else "not approved"
    strengths = "\n".join(f"- {s}" for s in review.strengths) or "- (none recorded)"
    weaknesses = "\n".join(f"- {w}" for w in review.weaknesses) or "- (none recorded)"
    recs = "\n".join(f"- {r}" for r in review.recommendations) or "- (none recorded)"
    next_block = (
        f"```json\n{json.dumps(next_parameters, indent=2, default=str)}\n```"
        if next_parameters else "_(none)_"
    )

    body = textwrap.dedent(
        f"""\
        # learned_skill: {slug}

        Generated from session `{session_id}`, iteration {iteration}.

        ## Context
        - Goal: {primary_goal or "(unspecified)"}
        - Status: {status}
        - Review summary: {review.summary or "(none)"}

        ## What worked
        {strengths}

        ## What didn't
        {weaknesses}

        ## Recommendations for next time
        {recs}

        ## Suggested parameters
        {next_block}
        """
    )
    return filename, body


class SkillExtractingReflectorAgent(ReflectorAgent):
    """Reflector that writes a markdown skill per actionable review.

    Inherits the default reflector's bookkeeping (run_history, next
    parameters, provenance) — overrides ``run`` to add the skill-file
    write side effect after the parent returns the lessons dict.
    """

    name = "skill_extracting_reflector"
    description = (
        "Default reflector behaviour + writes a markdown skill into "
        "``~/.sim2l/<session-id>/skills/learned/`` for each approved or "
        "actionable review so lessons survive past the session."
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

        target_dir = _session_skills_dir(self.context.session_id)
        if target_dir is None:
            return lessons

        try:
            primary_goal = str(self.context.memory.get("primary_goal") or "")
            iteration = int(getattr(self.context, "iteration", 0) or 0)
            filename, body = _render_skill(
                session_id=self.context.session_id,
                iteration=iteration,
                primary_goal=primary_goal,
                review=review,
                next_parameters=lessons.get("next_parameters") or {},
            )
            (target_dir / filename).write_text(body, encoding="utf-8")
            lessons = dict(lessons)
            lessons["skill_file"] = str(target_dir / filename)
        except Exception as exc:  # noqa: BLE001 — skill write is best-effort
            logger.warning("Failed to write learned skill: %s", exc)

        return lessons
