from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal


_REFINE_PROMPT = """\
You are a scientific experiment planner. Revise the experiment plan below based on the user's feedback.

Current plan:
  Parameters       : {parameters}
  Parameter sweep  : {parameter_sweep}
  Success criteria : {success_criteria}

User feedback: {feedback}

Return a revised JSON object with exactly the same fields as before:
- "proposal": (copy unchanged from the current plan)
- "artifact_strategy": "create_new_sim2l"
- "parameters": revised dict
- "parameter_sweep": revised dict
- "success_criteria": revised list

Apply the feedback precisely. Keep all string values concise. No extra keys.
"""

_PLAN_PROMPT = """\
You are a scientific experiment planner.

Convert the following research proposal into a concrete experiment plan.

Hypothesis  : {hypothesis}
Objective   : {objective}
Variables   : {variables}
Methodology : {methodology}
Target      : {target}

Return a JSON object with exactly these fields:
- "proposal": (copy the proposal fields below unchanged)
- "artifact_strategy": "create_new_sim2l"
- "parameters": dict mapping EVERY physically meaningful input variable to its default numeric value
- "parameter_sweep": dict mapping each input variable to a list of 4-6 numeric values that span a physically relevant range and bracket the target if one is given
- "success_criteria": list of 2-3 short strings (each under 60 chars), including proximity to target if one is given

Rules:
- Use domain-appropriate parameter names (e.g. "effective_mass", "temperature", "strain"), not generic ones like "input_parameter"
- All parameter values must be numbers, not strings
- Keep all string values concise. Do not add extra keys.
"""


class PlannerAgent(AgentContract):
    name = "planner"
    description = "Converts a research proposal into a concrete experiment plan."

    async def run(self, input_data: ResearchProposal) -> ExperimentPlan:
        proposal = (
            input_data
            if isinstance(input_data, ResearchProposal)
            else ResearchProposal(**input_data)
        )

        provider = self.context.memory.get("provider")
        if provider:
            target = getattr(input_data, "target", {}) or {}
            prompt = _PLAN_PROMPT.format(
                hypothesis=proposal.hypothesis[:300],
                objective=proposal.objective[:200],
                variables=proposal.variables,
                methodology=proposal.methodology[:300],
                target=target or "none specified",
            )
            try:
                plan = await provider.complete_structured(
                    prompt, ExperimentPlan, max_tokens=1024
                )
                # Ensure artifact_strategy is always a short safe string.
                plan.artifact_strategy = "create_new_sim2l"
                return plan
            except Exception:
                pass  # fall through to stub

        return ExperimentPlan(
            proposal=proposal,
            artifact_strategy="create_new_sim2l",
            parameters={"input_parameter": 1.0},
            parameter_sweep={"input_parameter": [0.5, 1.0, 1.5, 2.0]},
            success_criteria=[
                "artifact validates without errors",
                "test execution completes",
                "outputs are non-empty and parseable",
            ],
        )

    async def refine(self, plan: ExperimentPlan, feedback: str) -> ExperimentPlan:
        """Revise an existing plan based on free-text user feedback."""
        provider = self.context.memory.get("provider")
        if provider:
            prompt = _REFINE_PROMPT.format(
                parameters=plan.parameters,
                parameter_sweep=plan.parameter_sweep,
                success_criteria=plan.success_criteria,
                feedback=feedback[:500],
            )
            try:
                revised = await provider.complete_structured(prompt, ExperimentPlan, max_tokens=1024)
                revised.artifact_strategy = "create_new_sim2l"
                # Keep original proposal — refinement only touches parameters/sweep/criteria.
                revised.proposal = plan.proposal
                return revised
            except Exception:
                pass
        return plan  # fall back to unchanged plan if no provider
