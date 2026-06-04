# Executor & safety

*How ARC runs untrusted, often LLM-authored `simulate()` code: a spawned
subprocess gated by static AST checks.*

This page documents the mechanism; the threat model is in
{doc}`../architecture/security`.

## Static checks (`arc/runtime/workflow_safety.py`)

`check_workflow_source(source, allowed_imports=…)` rejects source that:

- exceeds `MAX_SOURCE_BYTES` (64 KiB) or `MAX_AST_NODES` (5 000);
- imports anything outside the allow-list
  (`BUILDER_ALLOWED_IMPORTS = {math, cmath, itertools}` for the builder, or
  the stricter `STRICT_ALLOWED_IMPORTS`);
- uses `Subscript` access to dunder strings or names starting with `__`.

`check_workflow_source_safe(source)` returns `(ok, reason)` for the builder.

## Subprocess execution

`run_simulate_with_timeout(code, calls)` runs the validated code in a
`multiprocessing.get_context("spawn")` worker (never `fork`) with
`build_safe_globals(...)`, a wall-clock timeout, and descendant-process
cleanup on timeout. The worker globals live in this importable module so the
spawn boundary can pickle them (the package directory is hyphenated and can't
be imported via the dotted form).

## API reference

```{eval-rst}
.. automodule:: arc.runtime.workflow_safety
   :members:
   :undoc-members:
```
