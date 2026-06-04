# Orchestrator

*`ResearchWorkflow` — the engine that runs one research iteration by executing
a registered workflow definition.*

`ResearchWorkflow` (`arc/orchestrator/workflow.py`) is what the CLI, API, UI,
and chat all instantiate to run the loop.

## Construction

`ResearchWorkflow(provider_name, token, model, base_url, session_id,
workflow_name, registry)` builds:

- the **registry** (or `_default_registry()`, with package
  enabled/disabled filtering applied);
- the **runtime adapter** (`_build_adapter`, honouring disabled packages) and
  its derived **backend** (`resolve_backend`);
- the **provider** (`_build_provider`, honouring disabled packages — `None` in
  stub mode);
- the artifact registry, results store, provenance log;
- the `AgentContext` (the shared `memory` blackboard);
- the **audit dispatcher** and **memory hooks**.

`refresh_disabled_packages()` re-resolves the provider + adapter (+ backend)
against the current session disabled set — called by the chat/UI
`/package disable` handlers so a mid-session disable invalidates live
provider/adapter instances.

## Running the loop

`run_once(goal) → dict`:

1. dispatch the `goal.received` audit phase;
2. look up the named workflow (`research-loop` by default) and run its steps;
3. return a result dict with `proposal`, `plan`, `artifact`, `validation`,
   `execution`, `review`, `reflection`, and per-step outputs.

## The YAML workflow engine

`_run_workflow_definition` walks the workflow's `steps`:

- each step is an `agent`, `skill`, or `adapter` step;
- a step's `agent` is resolved through the {doc}`strategy resolver <strategies>`
  for roles, else the registry (honouring `/package disable`);
- adapter steps may only call an **allow-listed** method
  (`run`, `prepare_inputs`, `validate_artifact`, `collect_*`, …) — a
  typo'd/malicious `method:` is refused;
- `conditions` (`if:` / `goto:`) drive branching with bounded iterations;
- per-step `on_error: retry|skip` policies;
- audit phases fire `.before`/`.after` around the relevant steps.

See {doc}`workflows` for the YAML format.

## API reference

```{eval-rst}
.. automodule:: arc.orchestrator.workflow
   :members: ResearchWorkflow
   :undoc-members:
```
