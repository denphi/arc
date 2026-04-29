"""Improvement planner: decides what to change next based on reflection."""

from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal


class ImprovementPlannerAgent(AgentContract):
    name = "improvement_planner"
    description = (
        "Uses reflective memory to decide the next experiment: "
        "parameter adjustment, artifact modification, or hypothesis revision."
    )

    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        reflection: dict = input_data.get("reflection", {})
        history: list[dict] = input_data.get("history", [])

        improving: bool = reflection.get("improving", True)
        recommendations: list[str] = reflection.get("review", {}).get("recommendations", [])
        prior_results: list = reflection.get("prior_results", [])

        action = "adjust_parameters"
        if not improving and len(history) >= 3:
            action = "modify_artifact"
        elif not improving:
            action = "widen_parameter_sweep"

        explored = {
            entry.get("parameters", {}).get("input_parameter")
            for entry in history
        }
        all_candidates = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
        next_candidates = [v for v in all_candidates if v not in explored][:4]

        primary_proposal = input_data.get("proposal", {})
        if primary_proposal:
            plan = ExperimentPlan(
                proposal=ResearchProposal(**primary_proposal),
                artifact_strategy="create_new_sim2l" if action == "modify_artifact" else "reuse",
                parameters={"input_parameter": next_candidates[0] if next_candidates else 1.0},
                parameter_sweep={"input_parameter": next_candidates},
                success_criteria=[
                    "improvement over prior result",
                    "execution completes without error",
                ],
            )
        else:
            plan = None

        return {
            "action": action,
            "next_parameters": next_candidates,
            "recommendations": recommendations,
            "plan": plan.model_dump() if plan else None,
        }
