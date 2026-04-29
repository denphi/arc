"""Decomposes a high-level research goal into a set of sub-experiments."""

from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ResearchGoal, ResearchProposal


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
        if provider:
            prompt = (
                f"You are a research coordinator using a MARS-style strategy.\n\n"
                f"Decompose the following research goal into 2-4 focused sub-experiments "
                f"that can each be independently planned and executed:\n\n"
                f"Goal: {goal.goal}\n"
                f"Domain: {goal.domain}\n"
                f"Constraints: {goal.constraints}\n\n"
                f"For each sub-experiment, provide a ResearchProposal."
            )
            # Return a single proposal as the primary experiment for now.
            proposal = await provider.complete_structured(prompt, ResearchProposal)
            return {
                "primary_proposal": proposal.model_dump(),
                "sub_experiments": [proposal.model_dump()],
            }

        primary_proposal = ResearchProposal(
            hypothesis=f"Sub-experiment: {goal.goal}",
            objective=goal.goal,
            variables=["input_parameter", "output_metric"],
            methodology="Iteratively test parameter values using the MARS search strategy.",
            expected_outcomes="Identify the optimal parameter value for the objective.",
            evaluation_metrics=["output_quality", "convergence_rate"],
            risk_level="low",
        )
        return {
            "primary_proposal": primary_proposal.model_dump(),
            "sub_experiments": [primary_proposal.model_dump()],
        }
