"""Hypothesis-space helpers for the default ideator (design/todo.md item 2).

ARC's normal path used to start from exactly one proposal; a weak first
hypothesis propagated into every downstream artifact and run. This module
makes the hypothesis space a small first-class object on the *default*
path (no separate orchestration package required):

  * generate several candidate proposals,
  * score each on novelty / feasibility / sim2l-fit / testability /
    measurable-outputs / domain-constraint axes (deterministic, no LLM),
  * select one primary plus, when useful, one diverse alternate,
  * report the ranking + rejection rationale so callers can store it.

A future ``arc-coscientist`` package can layer a richer
generate→reflect→rank→evolve→meta-review tournament on top of the same
candidate/score objects; this is the lightweight in-core version.

Provider-less mode is deterministic: candidate synthesis and scoring use
no randomness, so tests and demos reproduce exactly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from arc.schemas.research import ResearchGoal, ResearchProposal


class HypothesisCandidates(BaseModel):
    """Wrapper schema so a provider can return several proposals at once.

    Asking for the whole pool in one structured call keeps the ideator to a
    single provider round-trip (deterministic in tests) while still giving
    the ranking pass more than one candidate to choose between.
    """

    candidates: list[ResearchProposal] = Field(default_factory=list)


# ── Scoring ─────────────────────────────────────────────────────────────

_RISK_FEASIBILITY = {"low": 1.0, "medium": 0.7, "high": 0.4}

_WEAK_PHRASES = (
    "a simulation workflow can test",
    "the outputs reveal whether the hypothesis is supported",
)


def _measurable_output_score(proposal: ResearchProposal, goal: ResearchGoal) -> float:
    """Reward proposals whose metrics/variables cover the goal's target keys."""
    target_keys = {k.lower() for k in (goal.target or {})}
    if not target_keys:
        # No explicit target — reward having concrete, non-trivial metrics.
        return min(1.0, len(proposal.evaluation_metrics) / 3.0)
    tracked = {m.lower() for m in proposal.evaluation_metrics}
    tracked |= {v.lower() for v in proposal.variables}
    covered = sum(
        1 for tk in target_keys
        if any(tk in t or t in tk for t in tracked)
    )
    return covered / len(target_keys)


def _testability_score(proposal: ResearchProposal) -> float:
    score = 0.0
    if proposal.evaluation_metrics:
        score += 0.5
    if proposal.variables:
        score += 0.3
    if proposal.methodology and len(proposal.methodology) > 20:
        score += 0.2
    return min(1.0, score)


def _novelty_score(proposal: ResearchProposal, seen: list[ResearchProposal]) -> float:
    """1.0 when distinct from already-ranked candidates, lower for near-dupes."""
    hyp = (proposal.hypothesis or "").lower()
    tokens = set(hyp.split())
    if not tokens:
        return 0.0
    worst = 1.0
    for other in seen:
        other_tokens = set((other.hypothesis or "").lower().split())
        if not other_tokens:
            continue
        overlap = len(tokens & other_tokens) / max(len(tokens | other_tokens), 1)
        worst = min(worst, 1.0 - overlap)
    return worst


def _specificity_score(proposal: ResearchProposal) -> float:
    """Penalise the generic stub phrasing; reward concrete hypotheses."""
    hyp = (proposal.hypothesis or "").lower()
    if not hyp:
        return 0.0
    if any(phrase in hyp for phrase in _WEAK_PHRASES):
        return 0.2
    # Longer, specific hypotheses with a comparative/quantitative flavour
    # score higher than one-liners.
    score = min(1.0, len(hyp) / 120.0)
    if any(w in hyp for w in ("increase", "decrease", "maximise", "maximize",
                              "minimise", "minimize", "compared", "versus",
                              "as a function of", "vary")):
        score = min(1.0, score + 0.3)
    return score


def score_candidate(
    proposal: ResearchProposal, goal: ResearchGoal, seen: list[ResearchProposal],
) -> dict[str, float]:
    """Return per-axis scores plus a weighted ``total`` in [0, 1].

    Deterministic: depends only on the proposal, the goal, and the
    already-ranked ``seen`` candidates (for the novelty axis).
    """
    feasibility = _RISK_FEASIBILITY.get((proposal.risk_level or "medium").lower(), 0.6)
    novelty = _novelty_score(proposal, seen)
    measurable = _measurable_output_score(proposal, goal)
    testability = _testability_score(proposal)
    specificity = _specificity_score(proposal)
    sim2l_fit = 1.0 if proposal.variables and proposal.evaluation_metrics else 0.5

    axes = {
        "feasibility": feasibility,
        "novelty": novelty,
        "measurable_outputs": measurable,
        "testability": testability,
        "specificity": specificity,
        "sim2l_fit": sim2l_fit,
    }
    weights = {
        "feasibility": 0.15,
        "novelty": 0.15,
        "measurable_outputs": 0.25,
        "testability": 0.2,
        "specificity": 0.15,
        "sim2l_fit": 0.1,
    }
    total = sum(axes[a] * weights[a] for a in axes)
    axes["total"] = round(total, 4)
    return axes


def _is_duplicate(proposal: ResearchProposal, kept: list[ResearchProposal]) -> bool:
    hyp = (proposal.hypothesis or "").strip().lower()
    for other in kept:
        other_hyp = (other.hypothesis or "").strip().lower()
        if not hyp or not other_hyp:
            continue
        a, b = set(hyp.split()), set(other_hyp.split())
        if a and b and len(a & b) / max(len(a | b), 1) > 0.85:
            return True
    return False


def rank_candidates(
    candidates: list[ResearchProposal], goal: ResearchGoal,
) -> list[dict[str, Any]]:
    """Score, deduplicate, and order candidates best-first.

    Returns a list of ``{"proposal", "scores", "rejected", "reason"}``
    dicts. Near-duplicate hypotheses are marked ``rejected`` (they stay in
    the record so callers can show what was pruned) but excluded from
    selection. Order is by ``scores.total`` descending.
    """
    ranked: list[dict[str, Any]] = []
    kept: list[ResearchProposal] = []
    for proposal in candidates:
        if not isinstance(proposal, ResearchProposal):
            continue
        scores = score_candidate(proposal, goal, kept)
        entry: dict[str, Any] = {"proposal": proposal, "scores": scores}
        if _is_duplicate(proposal, kept):
            entry["rejected"] = True
            entry["reason"] = "near-duplicate of a higher-ranked candidate"
        else:
            entry["rejected"] = False
            entry["reason"] = ""
            kept.append(proposal)
        ranked.append(entry)
    ranked.sort(key=lambda e: (e["rejected"], -e["scores"]["total"]))
    return ranked


def select(
    candidates: list[ResearchProposal], goal: ResearchGoal,
) -> dict[str, Any]:
    """Pick the primary proposal and (when useful) a diverse alternate.

    Returns ``{"primary", "alternate", "ranked", "rationale"}``. ``primary``
    is the highest-scoring non-duplicate. ``alternate`` is the next
    non-duplicate whose hypothesis is meaningfully different (novelty
    against the primary ≥ 0.4), or ``None`` when the pool is too small or
    too homogeneous. Always returns a valid ``primary`` when at least one
    candidate is a real ``ResearchProposal``.
    """
    ranked = rank_candidates(candidates, goal)
    accepted = [e for e in ranked if not e["rejected"]]
    if not accepted:
        return {"primary": None, "alternate": None, "ranked": ranked, "rationale": ""}

    primary_entry = accepted[0]
    primary = primary_entry["proposal"]
    alternate = None
    for entry in accepted[1:]:
        if _novelty_score(entry["proposal"], [primary]) >= 0.4:
            alternate = entry["proposal"]
            break

    top = primary_entry["scores"]
    rationale = (
        f"Selected best of {len(ranked)} candidate(s): total={top['total']} "
        f"(measurable_outputs={top['measurable_outputs']:.2f}, "
        f"testability={top['testability']:.2f}, novelty={top['novelty']:.2f}, "
        f"feasibility={top['feasibility']:.2f})."
    )
    return {
        "primary": primary,
        "alternate": alternate,
        "ranked": ranked,
        "rationale": rationale,
    }


# ── Deterministic stub-mode candidate synthesis ─────────────────────────


def stub_candidates(goal: ResearchGoal) -> list[ResearchProposal]:
    """Several deterministic candidates for provider-less mode.

    Each candidate frames the goal through a distinct lens so the ranking
    pass has real choices, yet the set is fully reproducible (no RNG, no
    clock). The first mirrors the historical default-ideator stub so
    existing behaviour is preserved as one of the options.
    """
    target_keys = list((goal.target or {}).keys())
    metrics = (
        ["execution_success", "output_quality", *target_keys]
        if target_keys else
        ["execution_success", "output_quality", "metric_improvement"]
    )
    variables = (
        ["input_parameter", *target_keys]
        if target_keys else
        ["input_parameter", "output_metric"]
    )

    baseline = ResearchProposal(
        hypothesis=f"A simulation workflow can test: {goal.goal}",
        objective=goal.goal,
        variables=variables,
        methodology=(
            "Create a Sim2L artifact and evaluate outputs across selected parameters."
        ),
        expected_outcomes="The outputs reveal whether the hypothesis is supported.",
        evaluation_metrics=metrics,
        risk_level="medium",
    )

    target_phrase = (
        f" toward target {goal.target}" if target_keys else ""
    )
    sweep = ResearchProposal(
        hypothesis=(
            f"Systematically varying the input parameters changes the measured "
            f"outputs{target_phrase} for: {goal.goal}"
        ),
        objective=f"Map how outputs vary with inputs for: {goal.goal}",
        variables=variables,
        methodology=(
            "Build a Sim2L artifact and sweep its inputs across a structured "
            "grid, comparing each output against the prior point."
        ),
        expected_outcomes=(
            "A response surface showing which inputs most strongly drive the "
            "target outputs."
        ),
        evaluation_metrics=metrics,
        risk_level="low",
    )

    optimisation = ResearchProposal(
        hypothesis=(
            f"An optimiser can find inputs that maximise the desired outputs"
            f"{target_phrase} for: {goal.goal}"
        ),
        objective=f"Optimise inputs to reach the target outputs for: {goal.goal}",
        variables=variables,
        methodology=(
            "Build a Sim2L artifact and drive an optimisation search over its "
            "inputs against the target metrics."
        ),
        expected_outcomes=(
            "A best-found input configuration and its distance to the target."
        ),
        evaluation_metrics=metrics,
        risk_level="medium",
    )

    return [baseline, sweep, optimisation]
