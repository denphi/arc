# The Sim2L artifact contract in full

Authoritative detail behind the summary in `SKILL.md`. Each rule below is
enforced by a specific piece of ARC, named so you can check the current
behaviour rather than trusting this file.

## Field types

`arc/sim2l_schema.py` normalizes every declared type. Canonical names:

| Type | Use for |
|---|---|
| `Number` | Real-valued scalars. The default when nothing is declared. |
| `Integer` | Counts, indices, grid sizes. |
| `Boolean` | Flags. |
| `Text` | Free strings. Also the fallback for an unrecognised type name. |
| `Array` | Numeric arrays (schema-level only — `simulate()` still returns scalars). |
| `List` | Heterogeneous sequences. |
| `Dict` | Nested mappings. |
| `Image` | Image payloads. |
| `Element` | Chemical elements. |

Accepted spellings that normalize to a canonical type: `number`/`float`/`double`
→ `Number`; `integer`/`int` → `Integer`; `text`/`string`/`str` → `Text`;
`boolean`/`bool` → `Boolean`; `array` → `Array`; `list` → `List`;
`dict`/`object`/`map` → `Dict`; `image` → `Image`; `element` → `Element`.

An unrecognised name falls back to `Text` — never to `Number`. Declaring
`type: real` therefore gives you a text field and a downstream type error, so
stick to the canonical spellings.

### Shorthand form

A scalar instead of a mapping is read as the default, with the type inferred:

```yaml
inputs:
  temperature_k: 300.0     # ≡ {type: Number, default: 300.0}
```

Fine for a quick draft; prefer the full form so `units`, `min`, and `max`
survive into the catalog and the planners.

### Defaults

Inputs may declare `default`; outputs may not (it is stripped on load). A field
declaring no default is **omitted** from the reconciled input dict rather than
being given a fabricated value — ARC used to inject `1.0`, which silently ran
simulations at parameter values nobody chose. Your `inputs.get(name, default)`
fallback is what runs in that case.

## Input reconciliation

`arc/runtime/_adapter_common.py::reconcile_inputs` builds the dict passed to
`simulate()`:

1. Start from every schema-declared `default`.
2. Overlay caller-supplied values — but only for keys the schema declares.

So a caller passing an undeclared key has it dropped, and a declared key the
caller omitted arrives at its schema default. This is why the schema and the
`.get()` defaults should agree: disagreement means the artifact behaves
differently depending on which path invoked it.

## Static safety analysis

`arc/runtime/workflow_safety.py::check_workflow_source` parses the source and
rejects it before import. It is a lint, not a sandbox — but a rejection is a
hard build failure, so treat it as a compiler.

**Allowed imports** (`STRICT_ALLOWED_IMPORTS`):

```
math  cmath  itertools  functools  operator  statistics  random
decimal  fractions  collections  heapq  bisect  array
typing  abc  dataclasses  enum
```

Only the *source module* is checked, so `from itertools import product` and
`from math import *` are both fine. `from . import x` is rejected — there is no
package to be relative to.

**Blocked names**, as a call, a bare name, an attribute, or a constant
subscript:

```
__import__  eval  exec  compile  open  input  breakpoint
globals  locals  vars  dir  getattr  setattr  delattr
type  super  object
__class__  __bases__  __mro__  __subclasses__  __subclasshook__
__dict__  __globals__  __builtins__  __getattribute__  __getattr__
__setattr__  __delattr__  __init_subclass__  __loader__  __spec__
__file__  __module__  __base__  __qualname__  __name__
mro  bases  subclasses
```

Any identifier beginning with `__` is rejected by the same rule. The one
exception is a bare read of `__name__`, so the `if __name__ == "__main__":`
footer that coding agents reflexively emit is allowed — but `obj.__name__` as an
attribute is not.

Constant folding runs on subscripts, so `d["__cla" + "ss__"]` and
`d["_" * 2 + "class" + "_" * 2]` are caught alongside the literal spelling.

**Budgets:** 64 KiB of source, 5000 AST nodes. Both are checked before anything
executes, so an oversized file fails fast rather than slowly.

## Execution

`arc/runtime/executor.py` runs the artifact:

- Default mode spawns a child process with a wall clock from
  `ARC_LOCAL_EXEC_TIMEOUT` (30 s unless set). Exceeding it terminates the child
  and reports a timeout — indistinguishable, from the operator's side, from an
  infinite loop. Bound every loop.
- Outputs round-trip through JSON with a numpy `.tolist()`/`.item()` fallback,
  which is why non-scalar returns are a problem: they survive the trip and then
  break the consumers that assume numbers.
- `ARC_LOCAL_EXECUTOR=inprocess` skips the subprocess for speed in tests. It
  provides no isolation, so the static checks are the only guard there.

The threat model is stated plainly in that module: subprocess execution gives
**process isolation, not sandboxing**. The child runs with the caller's full
privileges. The allow-list exists to keep honest generated code inside a
predictable envelope, not to contain hostile code.

## What happens after you return

1. The caller writes `files` into `{registry_root}/{artifact_id}/{version}/`.
2. `ArtifactRegistry.register` records it and writes `arc_record.json`.
3. `validate-sim2l` checks the structure and optionally runs a test execution.
4. On success, the artifact becomes executable through any runtime adapter, and
   is publishable to the catalog by the active backend.

A schema mismatch discovered at step 3 forces a rebuild, which costs a full
model round-trip. The five checks at the end of `SKILL.md` exist to catch those
before you answer.
