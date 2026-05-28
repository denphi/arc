"""Decomposes a high-level research goal into a set of sub-experiments."""

from typing import Any

from pydantic import BaseModel, Field

from arc.contracts.agent import AgentContract
from arc.schemas.research import ResearchGoal, ResearchProposal


class _Decomposition(BaseModel):
    """Wrapper so the provider can return *several* proposals in one call.

    ``complete_structured`` returns a single model, so to get a list of
    sub-experiments we wrap them in a field and parse the wrapper.
    """

    sub_experiments: list[ResearchProposal] = Field(default_factory=list)


def _stub_sub_experiments(goal: ResearchGoal) -> list[ResearchProposal]:
    """Deterministic decomposition for stub mode (no provider).

    Produces three *distinct* angles on the goal rather than one proposal
    repeated — coarse parameter scan, fine local refinement, and a
    robustness/sensitivity check — so downstream planners have genuinely
    different sub-experiments to schedule.
    """
    angles = [
        (
            "coarse_scan",
            "Broad parameter scan",
            "Map the response surface coarsely across the full parameter range.",
            "Identify the region of the parameter space worth refining.",
        ),
        (
            "local_refine",
            "Local refinement",
            "Refine around the most promising region from the coarse scan.",
            "Locate the parameter values that best meet the objective.",
        ),
        (
            "sensitivity",
            "Sensitivity / robustness",
            "Perturb inputs around the candidate optimum to test robustness.",
            "Confirm the result is stable to small input changes.",
        ),
    ]
    return [
        ResearchProposal(
            hypothesis=f"{title}: {goal.goal}",
            objective=f"{goal.goal} — {title.lower()}",
            variables=["value", "output_metric"],
            methodology=methodology,
            expected_outcomes=outcome,
            evaluation_metrics=["output_quality", "convergence_rate"],
            risk_level="low",
        )
        for _key, title, methodology, outcome in angles
    ]


class ExperimentDecomposerAgent(AgentContract):
    name = "experiment_decomposer"
    description = "Breaks a research goal into a set of focused sub-experiments."

    async def run(self, input_data: ResearchGoal | dict[str, Any]) -> dict[str, Any]:
        goal = (
            input_data
            if isinstance(input_data, ResearchGoal)
            else ResearchGoal(**input_data)
        )

        provider = self.context.memory.get("provider")
        sub_experiments: list[ResearchProposal] = []
        if provider:
            prompt = (
                "You are a research coordinator using a MARS-style strategy.\n\n"
                "Decompose the following research goal into 2-4 focused "
                "sub-experiments that can each be independently planned and "
                "executed. Make them genuinely distinct (e.g. a coarse scan, "
                "a local refinement, a robustness check) — do not repeat the "
                "same experiment.\n\n"
                f"Goal: {goal.goal}\n"
                f"Domain: {goal.domain}\n"
                f"Constraints: {goal.constraints}\n\n"
                "Return a 'sub_experiments' list of ResearchProposal objects."
            )
            try:
                result = await provider.complete_structured(prompt, _Decomposition)
                sub_experiments = list(result.sub_experiments)
            except Exception:
                sub_experiments = []

        if not sub_experiments:
            sub_experiments = _stub_sub_experiments(goal)

        return {
            "primary_proposal": sub_experiments[0].model_dump(),
            "sub_experiments": [p.model_dump() for p in sub_experiments],
        }
