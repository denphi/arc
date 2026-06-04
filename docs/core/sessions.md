# Sessions & state

*A session is a unit of work with its own id and directory. This page covers
what is stored, where, and by whom.*

## Session paths

`arc.session` mints session ids and resolves per-session paths under
`~/.sim2l/code/<session_id>/` (override the root with `SIM2L_HOME`):

| Path | Holds |
|---|---|
| `artifacts/` | the `ArtifactRegistry` |
| `runs/` | the `ResultsStore` |
| `provenance.jsonl` | the `ProvenanceLog` |
| `<db>` | the sim2l run database (sim2l adapter only) |
| `session_state.json` | persisted strategy/recipe/package state (API path) |

## The three distinct history records

These are easy to conflate; ARC uses all three (see also the README):

| Record | What it is | Persisted by |
|---|---|---|
| **`run_history`** | structured per-iteration run summaries (inputs, outputs, status, review) — the authoritative experiment log | workflow / API / CLI |
| **Thread (transcript)** | the UI's display timeline of typed messages + command results | the browser UI (`ui_thread.json`) |
| **CLI line history** | a single global REPL line-editing history (up-arrow recall) | the CLI chat loop (`~/.../.arc_chat_history`) |

## What's persisted across turns

Session meta + state carry `run_history`, `target`, `next_parameters`,
`schema_registry`, `primary_goal`, `refinements`, **`packages`** (the session
enabled/disabled set), `agent_overrides`, and the active recipe. The chat and
UI hydrate these back onto `AgentContext.memory` between turns.

## API reference

```{eval-rst}
.. automodule:: arc.session
   :members:
   :undoc-members:

.. automodule:: arc.api.session_state
   :members:
   :undoc-members:
```
