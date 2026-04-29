"""MARS-inspired cost-aware experiment planner.

Decides what to experiment next based on prior results, cost budget,
and reflective memory. Artifact-agnostic.
"""

from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal


class MARSPlannerAgent(AgentContract):
    name = "mars_planner"
    description = (
        "MARS-inspired planner. Selects the next experiment based on "
        "prior results, cost budget, and reflection history."
    )

    async def run(self, input_data: dict[str, Any]) -> ExperimentPlan:
        proposal = ResearchProposal(**input_data["proposal"]) if "proposal" in input_data else None
        history: list[dict] = input_data.get("history", [])
        budget: float = input_data.get("budget", float("inf"))

        if proposal is None:
            raise ValueError("MARSPlannerAgent requires 'proposal' in input_data")

        # Cost-aware parameter selection: prefer unexplored regions.
        explored = {
            entry.get("parameters", {}).get("input_parameter")
            for entry in history
            if "parameters" in entry
        }
        candidates = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]
        next_values = [v for v in candidates if v not in explored][:3]
        if not next_values:
            next_values = [1.0]

        return ExperimentPlan(
            proposal=proposal,
            artifact_strategy="create_new_sim2l",
            parameters={"input_parameter": next_values[0]},
            parameter_sweep={"input_parameter": next_values},
            success_criteria=[
                "execution completes without error",
                "outputs are non-empty",
                "improvement over prior iteration",
            ],
        )
