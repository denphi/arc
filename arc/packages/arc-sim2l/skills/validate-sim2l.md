# validate-sim2l

## Description
Validates a Sim2L artifact for structural correctness and schema compliance. Optionally runs a test execution using the configured runtime adapter.

## Inputs
- `artifact` (ArtifactRecord): The artifact to validate.
- `adapter` (RuntimeAdapterContract): The runtime adapter to use for test execution.
- `run_test` (bool, default false): Whether to perform a test execution in addition to structural validation.

## Outputs
- `result` (ValidationResult): Validation outcome including errors, warnings, and optional test run ID.

## Steps
1. Check that the artifact path exists.
2. Verify that `sim2l.yaml` is present and parseable.
3. Verify that `workflow.py` (or equivalent) is present.
4. If `run_test` is true: invoke `adapter.run()` with default inputs and verify the result is `status: completed`.
5. Return a `ValidationResult`.

## Error Conditions
- `artifact path does not exist` — DRAFT not correctly written to disk.
- `sim2l.yaml missing or unparseable` — artifact schema is incomplete.
- `test run failed` — execution error during validation; see logs for details.
