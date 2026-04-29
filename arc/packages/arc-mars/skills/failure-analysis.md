# failure-analysis

## Description
Analyzes a failed or suboptimal execution to identify root causes and suggest corrective actions.

## Inputs
- `result` (ExecutionResult): The failed or suboptimal result.
- `artifact` (ArtifactRecord): The artifact that was executed.
- `plan` (ExperimentPlan): The plan under which it ran.

## Outputs
- `failure_type` (str): "execution_error" | "empty_output" | "validation_failure" | "timeout" | "quality_failure"
- `root_cause` (str): Identified root cause.
- `corrective_actions` (list[str]): Ranked list of recommended fixes.
- `recoverable` (bool): Whether the failure can be resolved without modifying the artifact.

## Failure Classification
| Condition | Type |
|---|---|
| `result.status != "completed"` | execution_error |
| `result.outputs` is empty | empty_output |
| Validation errors present | validation_failure |
| Execution time exceeded limit | timeout |
| Outputs present but metrics fail criteria | quality_failure |

## Corrective Action Priorities
1. Fix execution errors (import errors, syntax errors) → modify artifact
2. Fix empty outputs (missing output assignment) → modify artifact
3. Fix validation failures (schema mismatch) → update sim2l.yaml
4. Address quality failures (bad parameters) → adjust parameter sweep
