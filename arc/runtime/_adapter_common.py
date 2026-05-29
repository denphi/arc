"""Runtime-agnostic helpers shared by every ``RuntimeAdapter``.

The input/output schema handling is identical regardless of *where* a
workflow runs (locally, in a container, on a cluster), so it lives here
rather than being copied into each adapter. ``LocalRuntimeAdapter`` uses
these today; the Docker/Slurm/K8s adapters (package-provided, see
design/todo.md items 5-6) reuse the same functions so reconciliation
stays consistent across backends.

These are pure functions — no I/O, no async — so they're trivially
testable and safe to call from inside ``asyncio.to_thread`` workers.
"""

from __future__ import annotations

from typing import Any


def reconcile_inputs(
    input_schema: dict[str, Any],
    inputs: dict[str, Any],
    *,
    default_value: float = 1.0,
) -> dict[str, Any]:
    """Overlay caller ``inputs`` onto the artifact's declared defaults.

    Starts from each declared field's ``default`` (``default_value`` when a
    field declares none), then overlays the caller's values — but only for
    keys the schema declares, when a schema exists. This prevents an
    "unexpected fields" error when an LLM-generated ``simulate`` uses
    different parameter names than the caller supplied, while still letting
    a schema-less artifact accept arbitrary inputs.
    """
    defaults = {
        key: (field.get("default", default_value) if isinstance(field, dict) else default_value)
        for key, field in (input_schema or {}).items()
    }
    return {
        **defaults,
        **{
            key: value
            for key, value in (inputs or {}).items()
            if not input_schema or key in input_schema
        },
    }


def filter_outputs(
    output_schema: dict[str, Any],
    raw_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project ``raw_outputs`` onto the schema-declared output keys.

    When the artifact declares an output schema, keep exactly those keys
    (filling missing ones with ``None`` so the shape is predictable).
    With no declared schema, pass the raw outputs through unchanged.
    """
    raw_outputs = raw_outputs or {}
    if not output_schema:
        return raw_outputs
    return {key: raw_outputs.get(key) for key in output_schema}
