"""Target-distance helpers for the chat layer.

Thin wrappers over :mod:`arc.runtime.key_matching`, which owns the single
definition of "which output does this target key refer to". Used by the
chat post-execution diagnostic, the genetic-optimizer progress callback,
and the post-approval menu.
"""

from __future__ import annotations

from arc.runtime.key_matching import format_pct_off
from arc.runtime.key_matching import registry_keys_match as _registry_keys_match


def pct_off(
    outputs: dict,
    target: dict,
    schema_registry: dict | None = None,
) -> str:
    """Format a one-line "vs target" diagnostic.

    For each target key that resolves to a numeric output, emit
    ``output_name=value (X.X% off)``. Returns ``"(no target key match)"``
    when no target key resolves, or ``""`` when no target is set.
    """
    return format_pct_off(outputs, target, schema_registry)


def registry_keys_match(tk: str, ok: str, registry: dict) -> bool:
    """True when ``tk`` and ``ok`` map to the same schema-registry entry."""
    return _registry_keys_match(tk, ok, registry)
