# A complete worked artifact

A correct, minimal Sim2L artifact end to end. Pattern-match against it when the
constraints in `SKILL.md` leave something ambiguous.

## Plan

```
objective   : Predict how silicon's band gap narrows as temperature rises.
methodology : Varshni empirical relation.
parameters  : temperature_k in [0, 1200]
target      : bandgap_ev: 1.05
```

## `workflow.py`

```python
"""Temperature dependence of the silicon band gap (Varshni relation)."""

# Varshni parameters for silicon.
_E0_EV = 1.1700      # gap at 0 K
_ALPHA_EV_PER_K = 4.730e-4
_BETA_K = 636.0


def simulate(**inputs):
    """Return the silicon band gap at a given absolute temperature.

    Eg(T) = E0 - (alpha * T^2) / (T + beta)
    """
    temperature_k = float(inputs.get("temperature_k", 300.0))
    if temperature_k < 0.0:
        temperature_k = 0.0

    narrowing_ev = (_ALPHA_EV_PER_K * temperature_k ** 2) / (temperature_k + _BETA_K)
    bandgap_ev = _E0_EV - narrowing_ev

    return {
        "bandgap_ev": bandgap_ev,
        "narrowing_ev": narrowing_ev,
    }
```

Note what this does and doesn't do:

- Reads its one input through `.get()` with the same default the schema
  declares.
- Returns `bandgap_ev` — spelled exactly as the target key, so the run can be
  approved. `narrowing_ev` rides along as a diagnostic; extra numeric outputs
  are fine.
- Guards a physically impossible input rather than raising. A `ValueError` here
  would be reported as a failed simulation.
- No imports at all. `math` was available and simply wasn't needed.
- Module-level constants are fine and are the right place for magic numbers.

## `sim2l.yaml`

```yaml
name: silicon_bandgap_varshni
description: Silicon band gap versus temperature via the Varshni relation.

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
  narrowing_ev:
    type: Number
    units: eV
    description: Reduction from the 0 K gap.
```

`min`/`max` come straight from the plan's parameter range — that is what lets a
DoE or Bayesian planner sample the space without inventing bounds.

## The JSON you return

```json
{
  "name": "silicon_bandgap_varshni",
  "description": "Silicon band gap versus temperature via the Varshni relation.",
  "files": {
    "workflow.py": "\"\"\"Temperature dependence…\"\"\"\n\n_E0_EV = 1.1700\n…",
    "sim2l.yaml": "name: silicon_bandgap_varshni\ndescription: …\n"
  },
  "notes": "Varshni parameters are the standard silicon values (E0=1.17 eV, alpha=4.73e-4 eV/K, beta=636 K). Valid to roughly 1000 K; above that the empirical fit degrades. Target bandgap_ev=1.05 is reached near 490 K."
}
```

The `notes` field is where assumptions and validity limits belong — the
reviewer reads it, and it is the only channel for "this is right *provided*…".

## A failing version, and why

```python
import numpy as np                          # rejected: not on the allow-list

def simulate(**inputs):
    T = inputs["temperature_k"]             # rejected: KeyError when omitted
    eg = 1.17 - (4.73e-4 * T**2) / (T + 636)
    return {"Eg": float(eg), "curve": np.linspace(0, T, 100).tolist()}
    #        ^ wrong key: target was bandgap_ev, so this can never be approved
    #                       ^ list output: not a scalar
```

Four independent failures, three of which are rejected before the file is ever
imported.
