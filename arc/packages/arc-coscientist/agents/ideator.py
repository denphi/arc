"""Co-Scientist-inspired ARC ideator strategy.

This adapter borrows the cloned Co-Scientist repository's hypothesis style
without importing or changing the upstream code. Full durable tournament runs
are handled by ``CoScientistSupervisorAgent``; this ideator is the lightweight
ARC research-loop strategy.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from arc.contracts.agent import AgentContract
from arc.schemas.research import ResearchGoal, ResearchProposal


class _CandidatePool(BaseModel):
    candidates: list[ResearchProposal] = Field(default_factory=list)


class CoScientistIdeatorAgent(AgentContract):
    name = "ideator"
    description = "Co-Scientist-style hypothesis pool generator for ARC goals."

    async def run(self, input_data: ResearchGoal | dict[str, Any]) -> ResearchProposal:
        goal = input_data if isinstance(input_data, ResearchGoal) else ResearchGoal(**input_data)
        provider = self.context.memory.get("provider")

        candidates: list[ResearchProposal] = []
        if provider is not None:
            candidates = await self._provider_candidates(provider, goal)
        if not candidates:
            candidates = self._deterministic_candidates(goal)

        selected = self._select_and_record(candidates, goal)
        self.context.memory["coscientist_last_selected"] = selected.model_dump()
        return selected

    async def _provider_candidates(
        self,
        provider: Any,
        goal: ResearchGoal,
    ) -> list[ResearchProposal]:
        prompt = self._prompt(goal)
        try:
            pool = await provider.complete_structured(prompt, _CandidatePool)
            if isinstance(pool, _CandidatePool) and pool.candidates:
                return pool.candidates
        except Exception:  # noqa: BLE001
            pass

        # Some ARC providers expose only text completion. Keep the fallback
        # deliberately strict: if it is not JSON, we fall back to deterministic
        # candidates instead of guessing.
        try:
            raw = await provider.complete(prompt)
            data = json.loads(raw)
            records = data.get("candidates", data if isinstance(data, list) else [])
            return [ResearchProposal(**r) for r in records if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            return []

    def _prompt(self, goal: ResearchGoal) -> str:
        n = self._num_candidates()
        target = ", ".join((goal.target or {}).keys()) or "the requested outcome"
        constraints = json.dumps(goal.constraints or {}, indent=2, default=str)
        context = _arc_context(self.context.memory)
        context_block = f"\nARC context from prior attempts:\n{context}\n" if context else ""
        return f"""
You are acting as an ARC adapter for a Co-Scientist-style hypothesis engine.

Generate {n} distinct, tournament-ready research proposals for this goal:

Goal: {goal.goal}
Domain: {goal.domain or "general"}
Target outputs: {target}
Constraints:
{constraints}
{context_block}

Use the Co-Scientist pattern:
- Generation: propose specific mechanisms, entities, and anticipated outcomes.
- Reflection: note what would make each hypothesis testable and falsifiable.
- Ranking: make each proposal comparable against the others.
- Evolution: vary the strategy across candidates; do not produce paraphrases.
- Meta-review: prefer hypotheses that can become executable ARC experiments.

Return JSON matching this schema:
{{"candidates": [{{
  "hypothesis": "...",
  "objective": "...",
  "variables": ["..."],
  "methodology": "...",
  "expected_outcomes": "...",
  "evaluation_metrics": ["..."],
  "risk_level": "low|medium|high"
}}]}}
""".strip()

    def _deterministic_candidates(self, goal: ResearchGoal) -> list[ResearchProposal]:
        terms = _keywords(goal.goal)
        subject = " / ".join(terms[:3]) if terms else (goal.domain or "the system")
        target = ", ".join((goal.target or {}).keys()) or "the primary outcome"
        constraint_terms = [
            f"{k}={v}" for k, v in (goal.constraints or {}).items()
            if isinstance(v, (str, int, float, bool))
        ][:3]
        constraints = "; ".join(constraint_terms) or "the stated design constraints"

        return [
            ResearchProposal(
                hypothesis=(
                    f"A mechanism-focused sweep over {subject} will reveal a controllable "
                    f"driver of {target} under {constraints}."
                ),
                objective=f"Identify which mechanistic variables most strongly control {target}.",
                variables=[*terms[:4], target],
                methodology=(
                    "Run a structured parameter sweep, rank outcomes by distance to target, "
                    "and compare the top candidates against a conservative baseline."
                ),
                expected_outcomes=(
                    "One or two variables should dominate the response, creating a clear "
                    "first experiment for the next ARC planning step."
                ),
                evaluation_metrics=[target, "sensitivity", "distance_to_target"],
                risk_level="medium",
            ),
            ResearchProposal(
                hypothesis=(
                    f"Competing explanations for {subject} can be separated by a factorial "
                    f"experiment that perturbs the most uncertain assumptions."
                ),
                objective=(
                    "Discriminate between plausible mechanisms before committing "
                    "to optimization."
                ),
                variables=[*terms[:3], "assumption_flag", target],
                methodology=(
                    "Use a small fractional-factorial design to test main effects and "
                    "interactions, then reject hypotheses whose predicted direction is wrong."
                ),
                expected_outcomes=(
                    "The experiment should expose which assumptions are fragile and which "
                    "mechanisms remain viable after direct comparison."
                ),
                evaluation_metrics=["effect_direction", "interaction_strength", target],
                risk_level="low",
            ),
            ResearchProposal(
                hypothesis=(
                    f"An evolved hybrid strategy that combines the strongest prior pattern "
                    f"with an out-of-distribution perturbation will improve {target}."
                ),
                objective=(
                    "Search beyond the obvious local design space while keeping "
                    "feasibility checks."
                ),
                variables=[*terms[:3], "perturbation_scale", target],
                methodology=(
                    "Start from the best known or default configuration, introduce bounded "
                    "perturbations, and retain only candidates that satisfy "
                    "feasibility constraints."
                ),
                expected_outcomes=(
                    "A small set of non-obvious candidates should outperform the baseline or "
                    "clarify why the current design space is already constrained."
                ),
                evaluation_metrics=["baseline_delta", "constraint_violations", target],
                risk_level="high",
            ),
        ]

    def _select_and_record(
        self, candidates: list[ResearchProposal], goal: ResearchGoal,
    ) -> ResearchProposal:
        from arc.packages.arc_sim2l_agents.hypothesis import select as _select

        result = _select(candidates, goal)
        ranked = list(result["ranked"])
        primary = result["primary"] or candidates[0]
        summary = [
            {
                "hypothesis": entry["proposal"].hypothesis,
                "scores": entry["scores"],
                "rejected": entry["rejected"],
                "reason": entry["reason"],
                "source": "arc-coscientist",
            }
            for entry in ranked
        ]
        self.context.memory["ideator_candidates"] = summary
        self.context.memory["ideator_selection_rationale"] = result["rationale"]
        self.context.memory["coscientist_hypothesis_pool"] = {
            "goal": goal.goal,
            "source": "arc-coscientist",
            "mode": "provider" if self.context.memory.get("provider") else "deterministic",
            "candidates": summary,
            "selected": primary.model_dump(),
        }
        alternate = result.get("alternate")
        if alternate is not None:
            self.context.memory["ideator_alternate"] = alternate.model_dump()
        return primary

    def _num_candidates(self) -> int:
        try:
            n = int((self.context.config or {}).get("ARC_COSCIENTIST_CANDIDATES", 5))
        except (TypeError, ValueError, AttributeError):
            n = 5
        return max(3, min(n, 8))


def _keywords(text: str) -> list[str]:
    stop = {
        "a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by",
        "via", "with", "using", "that", "this", "is", "are", "be", "i", "want",
        "identify", "hypothesis", "hypotheses", "about",
    }
    words = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    out: list[str] = []
    for word in words:
        if word not in stop and len(word) > 2 and word not in out:
            out.append(word)
    return out


def _arc_context(memory: dict[str, Any]) -> str:
    lines: list[str] = []
    history = memory.get("run_history") or []
    if history:
        lines.append("Recent ARC runs:")
        for run in history[-3:]:
            lines.append(
                f"- inputs={run.get('inputs', {})} outputs={run.get('outputs', {})} "
                f"status={run.get('status', '')}"
            )
    retry_context = memory.get("retry_context") or []
    if retry_context:
        lines.append("Recent ARC retry reasons:")
        for note in retry_context[-3:]:
            if not isinstance(note, dict):
                continue
            lines.append(
                f"- reason={note.get('reason', 'retry')} "
                f"required_outputs={note.get('required_outputs', [])} "
                f"actual_outputs={list((note.get('actual_outputs') or {}).keys())}"
            )
    required_outputs = memory.get("required_outputs") or []
    if required_outputs:
        lines.append(f"Required output keys for the next artifact: {required_outputs}")
    return "\n".join(lines)
