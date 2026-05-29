"""Target-distance helpers.

Pure functions for measuring how far a simulation's outputs are from
the user's requested target. Used by the chat post-execution diagnostic,
the genetic-optimizer progress callback, and the post-approval menu.

These helpers have no dependency on chat state and were previously
duplicated across two call sites in ``loop.py`` and
``commands/optimize.py``.
"""

from __future__ import annotations


def pct_off(
    outputs: dict,
    target: dict,
    schema_registry: dict | None = None,
) -> str:
    """Format a one-line "vs target" diagnostic.

    For each target key that matches a numeric output key (either by
    fuzzy match or via the schema registry's aliases), emit
    ``output_name=value (X.X% off)``. Returns ``"(no target key match)"``
    when target keys can't be resolved, or ``""`` when no target is set.
    """
    from arc.packages import load_reviewer
    keys_match = load_reviewer()._keys_match
    parts: list[str] = []
    for tk, tv in target.items():
        for ok_name, ov in outputs.items():
            matched = keys_match(tk, ok_name) or (
                schema_registry and registry_keys_match(tk, ok_name, schema_registry)
            )
            if matched:
                # Exclude bool (an int subclass) and guard against a
                # non-numeric target value the user may have typed.
                if (
                    isinstance(ov, (int, float)) and not isinstance(ov, bool)
                    and isinstance(tv, (int, float)) and not isinstance(tv, bool)
                ):
                    pct = abs(ov - tv) / max(abs(tv), 1e-12) * 100
                    parts.append(f"{ok_name}={ov:.4g} ({pct:.1f}% off)")
                break
    if parts:
        return "  ".join(parts)
    return "(no target key match)" if target else ""


def registry_keys_match(tk: str, ok: str, registry: dict) -> bool:
    """Return True when target key ``tk`` and output key ``ok`` map to
    the same canonical schema entry via the schema registry's aliases.

    Both keys are normalised by lower-casing and stripping underscores so
    ``bandgap_ev`` and ``BandgapEv`` resolve the same way.
    """
    tk_flat = tk.replace("_", "").lower()
    ok_flat = ok.replace("_", "").lower()
    for canon, entry in registry.items():
        canon_flat = canon.replace("_", "").lower()
        aliases_flat = [a.replace("_", "").lower() for a in entry.get("aliases", [])]
        all_forms = [canon_flat] + aliases_flat
        if tk_flat in all_forms and ok_flat in all_forms:
            return True
    return False
