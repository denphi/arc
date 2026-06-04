# Backend actions

*Where artifacts and results get published. The Register / Persist / Record
steps — **not** agent roles.*

`BackendActions` is the **publish** half of
{doc}`../architecture/execution-vs-publish`. `resolve_backend(adapter)` picks
one; each call goes through `safe_backend_action(...)` so a backend error is
reported, not fatal.

## Bundled backends (`arc/runtime/backend.py`)

| Backend | Active when | Behaviour |
|---|---|---|
| `NoopBackend` | default | every action a silent no-op |
| `Sim2lBackend` | sim2l importable + adapter supports it | routes into the sim2l catalog/results services |
| `GitHubBackend` | `GITHUB_TOKEN` + `ARC_GITHUB_REPO` | commits artifacts/records to a repo via the Contents API |

## The contract

```python
class BackendActions:
    def is_active(self) -> bool: ...
    async def register_artifact(self, artifact) -> dict: ...
    async def persist_result(self, artifact, execution, inputs) -> dict: ...
    async def record_execution(self, artifact, execution, inputs, outputs) -> dict: ...
```

To add a publish target, implement `BackendActions` — it is *not* a strategy
and not selectable via `/strategy`.

## API reference

```{eval-rst}
.. automodule:: arc.runtime.backend
   :members:
   :undoc-members:
```
