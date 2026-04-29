"""Reflective reviewer: evaluates results in the context of prior iterations."""

from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.execution import ExecutionResult
from arc.schemas.research import ExperimentPlan
from arc.schemas.review import ReviewResult


class ReflectiveReviewerAgent(AgentContract):
    name = "reflective_reviewer"
    description = (
        "Evaluates results in the context of the full experiment history "
        "and identifies patterns for the improvement planner."
    )

    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        plan = ExperimentPlan(**input_data["plan"])
        result = ExecutionResult(**input_data["result"])
        history: list[dict] = input_data.get("history", [])

        prior_results = [
            entry.get("result", {}).get("outputs", {}).get("result")
            for entry in history
            if "result" in entry
        ]
        current_result = result.outputs.get("result")

        improving = (
            bool(prior_results)
            and current_result is not None
            and current_result > prior_results[-1]
        ) if prior_results else True

        approved = result.status == "completed" and bool(result.outputs) and improving

        review = ReviewResult(
            approved=approved,
            summary=(
                f"Result {current_result}. "
                + ("Improving over prior iteration." if improving else "No improvement detected.")
            ),
            strengths=["Structured output produced"] if result.outputs else [],
            weaknesses=(
                []
                if improving
                else ["No improvement over prior result — consider different parameters"]
            ),
            recommendations=[
                "Explore different parameter regions.",
                "Consider modifying the artifact if no improvement after 3 iterations.",
            ],
            iteration_complete=approved and len(history) >= 3,
        )

        return {
            "review": review.model_dump(),
            "improving": improving,
            "prior_results": prior_results,
            "current_result": current_result,
        }
