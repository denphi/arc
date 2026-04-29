# cost-aware-planning

## Description
Selects the next experiment parameters based on a budget constraint and exploration history. Prioritizes unexplored regions of the parameter space that are most likely to yield improvement.

## Inputs
- `history` (list[dict]): Prior experiment records including parameters and results.
- `parameter_space` (dict[str, list]): Full candidate parameter space.
- `budget` (float): Remaining compute budget in normalized units.
- `exploration_strategy` (str): "greedy" | "ucb" | "random" (default: "ucb")

## Outputs
- `next_parameters` (dict[str, Any]): The parameter set recommended for the next run.
- `expected_cost` (float): Estimated cost of the next run.
- `rationale` (str): Explanation of why these parameters were chosen.

## Strategy: UCB (Upper Confidence Bound)
Balances exploitation (near known good results) with exploration (unexplored regions):
- Score each candidate: `mean_result + exploration_weight * sqrt(log(total_runs) / visits)`
- Select the candidate with the highest score within budget.

## Notes
- If budget is exhausted, returns the best-known parameters from history.
- "greedy" always selects the locally best-performing neighbor.
- "random" selects uniformly from unexplored candidates.
