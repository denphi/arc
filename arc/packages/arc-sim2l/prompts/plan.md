# Planning Prompt

You are a scientific experiment planner. Convert the following research proposal into a concrete, executable experiment plan.

## Research Proposal
- **Hypothesis**: {hypothesis}
- **Objective**: {objective}
- **Variables**: {variables}
- **Methodology**: {methodology}

## Task
Define:
1. A concrete set of default parameters with realistic values
2. A parameter sweep range for the primary variable(s)
3. Measurable success criteria aligned with the evaluation metrics
4. An artifact strategy (create_new_sim2l | modify_existing)

## Output Format
Return a JSON object matching the ExperimentPlan schema.
