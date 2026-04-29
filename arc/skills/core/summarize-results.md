# summarize-results

## Description
Produces a human-readable summary of one or more execution results.

## Inputs
- `results` (list[ExecutionResult]): The results to summarize.
- `format` (str): "text" | "markdown" | "json" (default: "markdown")

## Outputs
- `summary` (str): Formatted summary.
- `table` (list[dict]): Structured tabular data for all results.

## Steps
1. Extract `run_id`, `status`, key outputs, and key metrics from each result.
2. Format as a table.
3. Highlight best and worst results.
4. Return in the requested format.

## Example Output (markdown)
```
| run_id | status    | result | execution_success |
|--------|-----------|--------|-------------------|
| abc123 | completed | 2.0    | True              |
| def456 | completed | 3.0    | True              |

**Best**: def456 (result=3.0)
```
