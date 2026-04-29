from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult


def _sim_inputs_from_metrics(metrics: dict, schema_keys: set) -> dict:
    """Return only the keys from metrics that are actual simulation inputs."""
    if schema_keys:
        return {k: v for k, v in metrics.items() if k in schema_keys}
    # No schema available — exclude known non-input metric keys
    _NON_INPUT = {"duration_seconds", "squid_id", "execution_id", "status", "cache_hit"}
    return {k: v for k, v in metrics.items()
            if k not in _NON_INPUT and isinstance(v, (int, float))}


class ReflectorAgent(AgentContract):
    name = "reflector"
    description = "Stores lessons from a review back into context for the next iteration."

    async def run(
        self,
        input_data: ReviewResult,
        execution: ExecutionResult | None = None,
    ) -> dict[str, Any]:
        review = (
            input_data
            if isinstance(input_data, ReviewResult)
            else ReviewResult(**input_data)
        )

        # Schema keys from the current artifact (set by builder via context).
        schema_keys: set = set(
            self.context.memory.get("current_artifact", None)
            and self.context.memory["current_artifact"].metadata.get("sim2l_inputs", {}).keys()
            or []
        )

        # Accumulate run history — store only real simulation inputs, not metrics noise.
        history = self.context.memory.setdefault("run_history", [])
        if execution:
            sim_inputs = _sim_inputs_from_metrics(execution.metrics or {}, schema_keys)
            history.append({
                "inputs": sim_inputs,
                "outputs": execution.outputs,
                "status": execution.status,
            })
        # Keep only the last 10 entries to avoid ballooning context
        self.context.memory["run_history"] = history[-10:]

        # Store reviewer's next_parameters — filtered to schema keys only.
        if review.next_parameters:
            next_p = dict(review.next_parameters)
            if schema_keys:
                next_p = {k: v for k, v in next_p.items() if k in schema_keys}
            if next_p:
                self.context.memory["next_parameters"] = next_p

        lessons = {
            "approved": review.approved,
            "strengths": review.strengths,
            "weaknesses": review.weaknesses,
            "recommendations": review.recommendations,
            "next_parameters": review.next_parameters,
            "should_continue": not review.approved and not review.iteration_complete,
        }

        provenance = self.context.memory.get("provenance")
        if provenance:
            provenance.record(
                session_id=self.context.session_id,
                action="reflection",
                agent=self.name,
                outputs=lessons,
            )

        return lessons
