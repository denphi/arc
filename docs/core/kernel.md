# Kernel

*The top-level object that boots ARC: it owns the registry, event bus, and
session manager, loads packages, and initialises extensions.*

`Kernel` (`arc/core/kernel.py`) is the composition root for a full ARC
process (the CLI `info` command and the API/UI servers build one). The
{doc}`orchestrator` builds a lighter registry directly for a single loop; the
kernel is the full startup path.

## Lifecycle

```python
from arc.core.kernel import Kernel

kernel = Kernel(config_path="arc.toml")   # parses arc.toml (cached)
await kernel.startup()                    # load .env, packages, extensions
...                                        # use kernel.registry / sessions / events
await kernel.shutdown()                   # shut down extensions
```

`startup()`:

1. `load_env()` — populate `os.environ` from `.env` (process env wins).
2. Resolve package paths from `[packages].paths`.
3. **Filter** them through `filter_package_paths(...)` so
   `[packages].enabled/disabled` is honoured (the same helper the orchestrator
   uses — see {doc}`enable-disable <../packages/enable-disable>`).
4. `load_packages(...)` — register every package's `provides.*` into the
   registry.
5. `_load_extensions()` — for each enabled `[extensions.<name>]` (or a
   package-declared extension definition), import the class and call
   `initialize(config, scoped_registry)`. The registry passed in is a
   **package-scoped proxy** so components an extension registers inherit the
   extension's package as their source (keeps `/package disable` effective for
   extension-created components).

## What it owns

| Attribute | Type | Purpose |
|---|---|---|
| `registry` | `ComponentRegistry` | every loaded component — see {doc}`registry` |
| `events` | `EventBus` | in-process pub/sub |
| `sessions` | `SessionManager` | session bookkeeping |
| `config` / `config_path` | `dict` / `Path` | the parsed `arc.toml` |

## API reference

```{eval-rst}
.. automodule:: arc.core.kernel
   :members:
   :undoc-members:
   :show-inheritance:
```
