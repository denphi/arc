# reflective-search

## Description
Analyzes the experiment history to identify patterns, dead ends, and promising directions. Used by the improvement planner to decide whether to continue the current search branch or pivot.

## Inputs
- `history` (list[dict]): Prior experiment records.
- `current_result` (ExecutionResult): The most recent result.
- `success_criteria` (list[str]): Criteria from the experiment plan.

## Outputs
- `pattern` (str): Identified trend — "improving" | "plateau" | "degrading" | "noisy"
- `should_pivot` (bool): Whether to change strategy or artifact.
- `pivot_reason` (str | None): Why a pivot is recommended.
- `best_result` (dict): The best result seen so far.
- `summary` (str): One-paragraph analysis.

## Pattern Detection Rules
- **improving**: Results monotonically improve over last 3 runs.
- **plateau**: Results vary < 5% over last 3 runs.
- **degrading**: Results monotonically worsen over last 3 runs.
- **noisy**: No consistent trend; high variance.

## Pivot Criteria
Recommend a pivot when:
- Pattern is "plateau" or "degrading" for >= 3 consecutive runs.
- No unexplored candidates remain in the parameter space.
- The experiment has reached `max_iterations`.
