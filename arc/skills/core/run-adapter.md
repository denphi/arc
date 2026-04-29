# run-adapter

## Description
Invokes the configured runtime adapter to execute an artifact with given inputs.

## Inputs
- `artifact` (ArtifactRecord): The artifact to execute.
- `inputs` (dict): Input parameters for the run.
- `adapter_name` (str, default "local"): Which adapter to use.

## Outputs
- `result` (ExecutionResult): The execution result including outputs, logs, and metrics.

## Steps
1. Look up the adapter by `adapter_name` from the kernel registry.
2. Call `adapter.prepare_inputs(artifact, inputs)`.
3. Call `adapter.run(artifact, prepared_inputs)`.
4. Return the `ExecutionResult`.

## Notes
- The local adapter runs in-process (placeholder behavior in MVP).
- The sim2l adapter delegates to the Sim2L execution API.
- All results are automatically saved to the results store.
