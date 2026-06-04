# Memory & provenance

*The durable stores under a session, and the optional vector-memory /
knowledge-graph wiring.*

## Core stores (`arc/memory/`)

| Store | File | Holds |
|---|---|---|
| `ArtifactRegistry` | `artifact_registry.py` | registered artifacts on disk (atomic writes; path-traversal guarded) |
| `ResultsStore` | `results_store.py` | saved `ExecutionResult`s |
| `ProvenanceLog` | `provenance.py` | an append-only JSONL log of every loop action |

These are always present (the "core memory" layer).

## Extended memory (optional)

`MemoryHooks` (`arc/memory/hooks.py`) wires the **optional**
`arc-vector-memory` and `arc-knowledge-graph` extensions into the loop. When
enabled, a register → save → review cycle:

- indexes artifact name/description, run outputs/metrics, and review summaries
  into the vector store;
- records graph edges (`session → artifact → result → variable`, `→ review`);
- exposes semantic search to agents via
  `AgentContext.memory["memory_search"]`.

When neither extension is enabled it is a **clean no-op**. See
{doc}`../packages/extensions` for enabling them.

## API reference

```{eval-rst}
.. automodule:: arc.memory.artifact_registry
   :members:
   :undoc-members:

.. automodule:: arc.memory.results_store
   :members:
   :undoc-members:

.. automodule:: arc.memory.provenance
   :members:
   :undoc-members:

.. automodule:: arc.memory.hooks
   :members:
   :undoc-members:
```
