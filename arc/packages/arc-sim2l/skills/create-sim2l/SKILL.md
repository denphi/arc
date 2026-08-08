---
name: create-sim2l
description: Turn an experiment plan into a runnable Sim2L artifact — a workflow.py defining simulate(**inputs) -> dict plus the sim2l.yaml declaring its input and output schema. Use when a research plan has been approved and needs executable code, or when an existing artifact must be rebuilt because its schema no longer matches the plan. Emits the artifact files as JSON; it does not write to disk or register anything.
output_format: json
allowed-tools: Read, Write, Edit
metadata:
  package: arc-sim2l
  role: builder
  produces: sim2l-artifact
---

# Create a Sim2L artifact

You are generating the two files that make a Sim2L artifact runnable. Return
them as JSON — the caller writes them to disk, registers the artifact, and
executes it. Nothing you emit is reviewed by a human before it runs, so the
constraints below are hard requirements, not style preferences.

## Inputs you receive

| Key | Meaning |
|---|---|
| `objective` | What the simulation must compute, in plain language. |
| `methodology` | The approach to implement (analytic model, finite difference, empirical fit…). |
| `parameters` | Input names, and ranges or defaults where the plan fixed them. |
| `target` | Optional `{output_key: desired_value}`. Read the naming rule below — it decides whether the run can ever be approved. |
| `build_context` | Optional prior findings, prior artifacts, or domain constraints to honour. |

Anything absent is yours to choose. Prefer the simplest model that answers the
objective; a correct closed-form expression beats an elaborate solver that
times out.

## The `workflow.py` contract

Exactly one public function, with this signature:

```python
def simulate(**inputs) -> dict:
    ...
```

- **Read every input** with `inputs.get("name", default)`. Never index
  `inputs["name"]` — a caller may omit any key, and a `KeyError` reads to the
  operator as a crashed simulation rather than a missing argument.
- **Give every input a default in the signature body.** The executor reconciles
  caller inputs against `sim2l.yaml`, and a field that declares no default is
  *omitted* rather than fabricated, so your `.get()` fallback is what actually
  runs.
- **Return a flat `dict` of numbers.** Floats and ints only — no lists, no
  strings, no nested dicts, no numpy arrays. Outputs are indexed, compared
  across runs, and fed to optimizers that assume scalars.
- **Name outputs as valid snake_case Python identifiers.**
- **Never return an empty dict.** `{}` is treated as a failed build.

### Target keys must match exactly

When `target` is given, the primary output key must be spelled *exactly* as the
target key. A target of `bandgap_ev: 1.1` requires an output named
`bandgap_ev` — not `Eg_total`, not `band_gap_eV`, not `energy_gap`. Approval is
an exact key match, so a renamed output can never be approved no matter how
correct the physics is.

### Imports are allow-listed

Static analysis rejects the file before it is ever imported. Only these stdlib
modules are permitted:

```
math      cmath     itertools   functools   operator
statistics  random  decimal     fractions
collections heapq   bisect      array
typing    abc       dataclasses enum
```

`numpy`, `scipy`, `pandas` and every other third-party package are rejected.
Implement the numerics by hand — `math` plus a loop is almost always enough at
this scale. `from X import Y` is fine when `X` is on the list.

### Constructs that are rejected outright

`eval`, `exec`, `compile`, `open`, `input`, `__import__`, `globals`, `locals`,
`vars`, `getattr`, `setattr`, `type`, `super`, and any name or attribute
starting with `__` (`__class__`, `__globals__`, `__subclasses__`, …). Dunder
*subscripts* are caught too, including ones assembled from string
concatenation. `__name__` is the single exception, so an
`if __name__ == "__main__":` self-test footer is allowed.

These are refused whether or not they would have been harmful. Write plain
arithmetic and the checker stays out of your way.

### Budgets

- Source under 64 KiB and under 5000 AST nodes — aim for well under 60 lines.
- `simulate()` runs in a spawned subprocess with a 30-second wall clock by
  default. An unbounded `while` loop is a build failure, not a slow run. Cap
  every iteration count and every convergence loop.

## The `sim2l.yaml` contract

```yaml
name: silicon_bandgap
description: Temperature dependence of the silicon band gap (Varshni).

inputs:
  temperature_k:
    type: Number
    default: 300.0
    units: K
    min: 0.0
    max: 1200.0
    description: Absolute temperature.

outputs:
  bandgap_ev:
    type: Number
    units: eV
    description: Band gap at the requested temperature.
```

- Field types are `Integer`, `Number`, `Text`, `Array`, `Image`, `Element`,
  `Boolean`, `List`, `Dict`. Common spellings (`float`, `string`, `bool`, `int`)
  are normalized; an unrecognised type silently becomes `Text`, which is rarely
  what you meant — use a canonical name.
- `units`, `min`, `max`, `choices` and any other keys are preserved verbatim and
  used by catalog indexing, input reconciliation, and the optimizers. Declaring
  `min`/`max` is what lets a planner sample the space sensibly, so supply them
  whenever the plan implies a range.
- **Inputs** may declare `default`; **outputs** must not (it is stripped).
- Every key in `inputs` must be one your `simulate()` reads, and every key in
  `outputs` must be one it returns. A mismatch surfaces later as a schema error
  and forces a rebuild.

## Return format

Return one JSON object and nothing else — no prose, no markdown fences:

```json
{
  "name": "silicon_bandgap",
  "description": "One sentence on what the artifact computes.",
  "files": {
    "workflow.py": "…full file contents…",
    "sim2l.yaml": "…full file contents…"
  },
  "notes": "Assumptions, the model used, and anything the reviewer should check."
}
```

`name` must be a snake_case identifier — it becomes the artifact name in the
catalog. Put the file contents in as literal strings with real newlines;
do not wrap them in code fences inside the JSON.

## Before you answer

Read your own `simulate()` once more and confirm:

1. It parses, and defines `simulate` exactly once.
2. Every `inputs.get()` key appears in `sim2l.yaml` under `inputs`, and every
   returned key appears under `outputs`.
3. If a target was given, its key is returned verbatim.
4. Every import is on the allow-list; no blocked names appear anywhere.
5. Running it with the declared defaults returns a non-empty dict of numbers
   and terminates well inside 30 seconds.

`references/` holds the full field-type table, the complete rejected-construct
list, and a worked artifact you can pattern-match against — read them if the
constraints above leave something ambiguous.
