# compare-experiments

## Description
Compares two or more experiment results and produces a structured comparison report.

## Inputs
- `results` (list[ExecutionResult]): The results to compare.
- `metrics` (list[str]): Which metrics to compare (defaults to all).
- `baseline_run_id` (str | None): If set, compare all results against this baseline.

## Outputs
- `comparison` (dict): Metric-by-metric comparison table.
- `winner` (str): The run_id of the best result.
- `delta` (dict): Absolute and relative differences between results.
- `summary` (str): One-paragraph comparison narrative.

## Comparison Method
For each metric:
1. Extract the value from each result's `metrics` dict or `outputs` dict.
2. Compute absolute delta and percentage change relative to baseline (or first result).
3. Rank results by the primary metric (first in `metrics` list).

## Notes
- Only metrics present in all results are compared.
- If `baseline_run_id` is not set, the first result in the list is treated as the baseline.
