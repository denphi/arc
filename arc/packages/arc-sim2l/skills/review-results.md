# review-results

## Description
Analyzes execution results and produces a human-readable summary with pass/fail determination and actionable recommendations.

## Inputs
- `result` (ExecutionResult): The execution result to review.
- `success_criteria` (list[str]): The criteria from the experiment plan.
- `domain_evaluator` (optional): A domain package evaluator (e.g., from arc-materials).

## Outputs
- `review` (ReviewResult): Structured review with approval status, strengths, weaknesses, and recommendations.

## Steps
1. Check `result.status == "completed"`.
2. Check `result.outputs` is non-empty.
3. If a `domain_evaluator` is provided: run domain-specific checks on outputs.
4. Match outputs against `success_criteria`.
5. Assemble `ReviewResult`.

## Domain Evaluator Interface
If provided, the domain evaluator must implement:
```python
def evaluate(outputs: dict) -> dict[str, bool | str]
```
Returning a dict of criterion → pass/fail/value.
