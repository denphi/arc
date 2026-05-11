"""Fuzzy matching for physics-quantity key names.

Three ARC agents (reviewer, optimizer, curator) historically each had their
own slightly-different ``_keys_match`` implementation. They've now been
consolidated here so a change in one place propagates everywhere.

The matcher operates in three tiers, in order of decreasing reliability:

  Tier 0 — explicit schema registry. If both keys resolve to the same
           canonical name (or one is registered as an alias of the other),
           they match.
  Tier 1 — flat exact match (case-insensitive, underscores ignored).
  Tier 2 — substring match, but only when both flattened forms are ≥ 5
           characters long. Stops 'ev' inside 'sensitivity_ev_per_nm' from
           matching the target key 'ev'.
  Tier 3 — physics-synonym groups (bandgap/band_gap, strain/eps, …).
"""

from __future__ import annotations

import re

# Each entry is a frozenset of tokens that all name the *same physical
# quantity*. A match requires BOTH keys to share tokens from the SAME group.
# Units (eV, nm, etc.) are intentionally excluded — they appear in many
# unrelated quantities and would cause false matches.
PHYSICS_SYNONYMS: list[frozenset] = [
    frozenset({"bandgap", "band_gap", "bg", "gap"}),  # "eg" excluded — too short
    frozenset({"strain", "eps", "epsilon", "deformation"}),
    frozenset({"temperature", "temp", "kelvin"}),
    frozenset({"effective_mass", "effectivemass", "ema"}),
    frozenset({"doping", "dopant", "carrier", "concentration"}),
    frozenset({"pressure", "stress"}),
    frozenset({"mobility", "mu"}),
    frozenset({"wavelength", "lambda"}),
    frozenset({"frequency", "freq"}),
]

# Short tokens to ignore during matching (units, articles, common suffixes).
STOP_TOKENS: frozenset = frozenset({
    "ev", "mev", "gpa", "mpa", "nm", "pm", "cm", "k", "hz", "ghz", "thz",
    "per", "total", "avg", "mean", "min", "max", "value", "val",
    "the", "a", "an", "of", "in",
})

_MIN_SUBSTRING_LENGTH = 5


def _flatten(key: str) -> str:
    return key.replace("_", "").lower()


def _key_tokens(key: str) -> frozenset:
    """Normalised, stop-word-free token set for a key."""
    raw = re.sub(r"[0-9]", "", key.lower())
    parts = frozenset(re.split(r"[_\-\s]+", raw)) - {""} - STOP_TOKENS
    return parts


def registry_keys_match(tk: str, ok: str, registry: dict | None) -> bool:
    """Tier 0: tk and ok both resolve to the same canonical entry."""
    if not registry:
        return False
    tk_flat = _flatten(tk)
    ok_flat = _flatten(ok)
    for canon, entry in registry.items():
        canon_flat = _flatten(canon)
        aliases_flat = [_flatten(a) for a in entry.get("aliases", [])]
        all_forms = [canon_flat] + aliases_flat
        if tk_flat in all_forms and ok_flat in all_forms:
            return True
    return False


def fuzzy_keys_match(tk: str, ok: str) -> bool:
    """Tiers 1–3: flat / substring / physics-synonym match.

    Use ``keys_match`` when a registry is available — it tries Tier 0 first.
    """
    tk_flat = _flatten(tk)
    ok_flat = _flatten(ok)

    # Tier 1 — flat exact match.
    if tk_flat == ok_flat:
        return True

    # Tier 2 — substring match (require meaningful length).
    if len(tk_flat) >= _MIN_SUBSTRING_LENGTH and len(ok_flat) >= _MIN_SUBSTRING_LENGTH:
        if tk_flat in ok_flat or ok_flat in tk_flat:
            return True

    # Tier 3 — physics-synonym groups.
    tk_toks = _key_tokens(tk)
    ok_toks = _key_tokens(ok)
    if tk_toks and ok_toks:
        for group in PHYSICS_SYNONYMS:
            if tk_toks & group and ok_toks & group:
                return True

    return False


def keys_match(tk: str, ok: str, registry: dict | None = None) -> bool:
    """Combined matcher: Tier 0 registry → Tier 1-3 fuzzy."""
    if registry_keys_match(tk, ok, registry):
        return True
    return fuzzy_keys_match(tk, ok)
