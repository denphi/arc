# Runtime adapters

*Where a workflow runs. Selected by `ARC_RUNTIME_ADAPTER`; implements
`RuntimeAdapterContract` (validate, prepare inputs, run, collect).*

A runtime adapter is the **Execute** half of {doc}`../architecture/execution-vs-publish`.
ARC selects one at workflow construction via `_build_adapter(...)`.

## Bundled adapters

| `ARC_RUNTIME_ADAPTER` | Adapter | Notes |
|---|---|---|
| `local` (default) / `python` | `LocalRuntimeAdapter` | runs `simulate()` in a spawned subprocess; no sim2l import on the run path |
| `sim2l` / `sim2l-local` | `Sim2LRuntimeAdapter` | sim2l isolated executor + run database |
| `service` / `sim2l-service` | `Sim2LRuntimeAdapter` | as above with `ARC_STORAGE_MODE=required` |
| `auto` | local or sim2l | sim2l when importable, else local |
| `docker` / `slurm` / `k8s` | package adapters | from `arc-docker` / `arc-slurm` / `arc-k8s` |

A package adapter whose package is **disabled** for the session falls back to
the local adapter (`_build_adapter(..., disabled_packages=…)`). See
{doc}`../packages/enable-disable`.

## The contract

`RuntimeAdapterContract` (`arc/contracts/adapter.py`):

- `validate_artifact(artifact) → ValidationResult`
- `prepare_inputs(artifact, parameters) → dict`
- `run(artifact, inputs) → ExecutionResult`
- `get_status(run_id)` / `collect_outputs` / `collect_logs` / `collect_metrics`

Remote adapters (docker/slurm/k8s) share a submit→poll→collect base
(`arc/runtime/_adapter_common.py`).

## API reference

```{eval-rst}
.. automodule:: arc.runtime.local
   :members:
   :undoc-members:

.. automodule:: arc.runtime.sim2l_adapter
   :members:
   :undoc-members:
```
