# Execution vs publish

*Two orthogonal axes the design keeps strictly separate: where a workflow
**runs**, and where its artifacts and results get **published**.*

This is the single most important distinction in ARC's runtime. Conflating
them leads to confusion about what `ARC_RUNTIME_ADAPTER` controls (it does
*not* control persistence) and where results go (the backend, not the
adapter).

| Axis | Question it answers | Chosen by | Default |
|---|---|---|---|
| **Runtime adapter** | *Where does the workflow execute?* | `ARC_RUNTIME_ADAPTER` | `LocalRuntimeAdapter` |
| **Backend actions** | *Where do artifacts + results get published?* | `resolve_backend(adapter)` (inferred or `ARC_BACKEND`) | `NoopBackend` (publishes nothing) |

You can mix them freely: run **locally** and publish to **sim2l**, or run on a
**cluster** and publish **nowhere**.

## Runtime adapter — the Execute step

The runtime adapter implements `RuntimeAdapterContract`: it prepares inputs,
validates an artifact (schema/imports/AST safety), runs it, and collects
outputs/logs/metrics. ARC ships:

- `LocalRuntimeAdapter` — runs generated `simulate()` in a spawned subprocess
  with a wall-clock timeout, **no sim2l import on the run path**.
- `Sim2LRuntimeAdapter` — uses the sim2l isolated executor + run database
  (`ARC_RUNTIME_ADAPTER=sim2l` / `service`).
- Package-provided remote adapters: `docker`, `slurm`, `k8s`
  (`arc-docker` / `arc-slurm` / `arc-k8s`).

See {doc}`../core/runtime-adapters`.

## Backend actions — the Register / Persist / Record steps

The **publish** steps are `BackendActions`, **not** agent roles. Three
methods, each returning a small status dict:

- `register_artifact(artifact)`
- `persist_result(artifact, execution, inputs)`
- `record_execution(artifact, execution, inputs, outputs)`

ARC ships three backends ([`arc/runtime/backend.py`](../reference/api/index)):

| Backend | When active | Behaviour |
|---|---|---|
| `NoopBackend` | default | Every action is a silent no-op — ARC runs fully local with no shared persistence. |
| `Sim2lBackend` | sim2l importable + the adapter supports it | Routes register/persist/record into the sim2l catalog/results services. |
| `GitHubBackend` | `GITHUB_TOKEN` + `ARC_GITHUB_REPO` set | Commits artifacts (and optionally run records) to a GitHub repo via the Contents API. |

`resolve_backend(adapter)` picks one: a `Sim2lBackend` when sim2l is active
and the adapter supports it, else a `GitHubBackend` when configured, else the
silent `NoopBackend`. Each publish call goes through `safe_backend_action`,
which catches and reports backend errors rather than failing the run.

## Why the separation matters

- **Local-first by default.** With `NoopBackend`, ARC needs no services and
  no network — the same code path the test suite runs.
- **Independent evolution.** Adding a new publish target (e.g. an S3 backend)
  is a `BackendActions` implementation; adding a new execution target (e.g. a
  cluster) is a `RuntimeAdapterContract` implementation. Neither touches the
  other.
- **Not a role.** Because publish is a backend, not a strategy, it isn't
  selectable via `/strategy` and isn't part of the role catalogue. See
  {doc}`../packages/roles`.

See also: {doc}`../core/backends`, {doc}`../contracts/index`,
{doc}`../guides/sim2l-services`.
