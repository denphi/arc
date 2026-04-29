# propose-next-iteration

## Description
Synthesizes reflection, comparison, and cost analysis to propose the next experiment iteration. Returns a concrete ExperimentPlan.

## Inputs
- `reflection` (dict): Output from `reflective-search`.
- `comparison` (dict | None): Output from `compare-experiments`, if available.
- `cost_plan` (dict | None): Output from `cost-aware-planning`, if available.
- `current_plan` (ExperimentPlan): The plan from the previous iteration.
- `history` (list[dict]): Full experiment history.

## Outputs
- `next_plan` (ExperimentPlan): Proposed plan for the next iteration.
- `change_summary` (str): What changed from the previous plan and why.
- `action` (str): "adjust_parameters" | "modify_artifact" | "revise_hypothesis" | "stop"

## Decision Logic
1. If `reflection.should_pivot` is true and `reflection.pattern == "plateau"`:
   → action = "modify_artifact"
2. If `reflection.pattern == "improving"` and unexplored candidates remain:
   → action = "adjust_parameters" using `cost_plan.next_parameters`
3. If `reflection.pattern == "degrading"`:
   → action = "revise_hypothesis"
4. If success criteria are all met:
   → action = "stop"
