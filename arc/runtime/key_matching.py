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


# ── Target resolution ───────────────────────────────────────────────────
#
# Every "how close are the outputs to the target?" computation (reviewer
# approval, optimizer fitness, reflective/comparative reviewers, chat
# diagnostics) goes through ``match_targets`` / ``target_errors`` so they
# agree on which output a target key refers to. Matching is done across
# *all* outputs and the most reliable tier wins — a fuzzy hit on an earlier
# key (``band_gap_units``) must never shadow an exact one (``band_gap``).


def is_number(value: object) -> bool:
    """True for int/float values, excluding ``bool`` (an int subclass)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _match_tier(tk: str, ok: str, registry: dict | None) -> int | None:
    """Rank how reliably output key ``ok`` names target key ``tk``.

    Lower is better; ``None`` means no match.
    """
    if tk == ok:
        return 0
    if _flatten(tk) == _flatten(ok):
        return 1
    if registry_keys_match(tk, ok, registry):
        return 2
    tk_flat, ok_flat = _flatten(tk), _flatten(ok)
    if len(tk_flat) >= _MIN_SUBSTRING_LENGTH and len(ok_flat) >= _MIN_SUBSTRING_LENGTH:
        if tk_flat in ok_flat or ok_flat in tk_flat:
            return 3
    tk_toks, ok_toks = _key_tokens(tk), _key_tokens(ok)
    if tk_toks and ok_toks:
        for group in PHYSICS_SYNONYMS:
            if tk_toks & group and ok_toks & group:
                return 4
    return None


def best_output_key(
    target_key: str,
    outputs: dict,
    registry: dict | None = None,
    *,
    numeric_only: bool = True,
) -> str | None:
    """The output key that best matches ``target_key``, or ``None``.

    With ``numeric_only`` (the default) non-numeric outputs are never
    candidates, so a label such as ``band_gap_units="eV"`` can't be picked
    in place of the numeric ``band_gap``. Ties keep output order.
    """
    best: tuple[int, str] | None = None
    for ok, ov in outputs.items():
        if numeric_only and not is_number(ov):
            continue
        tier = _match_tier(target_key, str(ok), registry)
        if tier is not None and (best is None or tier < best[0]):
            best = (tier, ok)
            if tier == 0:
                break
    return best[1] if best else None


def match_targets(
    outputs: dict,
    target: dict,
    registry: dict | None = None,
    *,
    numeric_only: bool = True,
) -> dict[str, str | None]:
    """Map each target key to its best-matching output key (or ``None``)."""
    return {
        tk: best_output_key(tk, outputs, registry, numeric_only=numeric_only)
        for tk in target
    }


def target_errors(
    outputs: dict,
    target: dict,
    registry: dict | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Relative error per matched output key, plus unresolved target keys.

    Returns ``({output_key: |out - tgt| / |tgt|}, [unmatched_target_keys])``.
    A target key is unmatched when no numeric output matches it or when the
    target value itself is not a number. Callers deciding approval must
    treat any unmatched key as a failure — an unchecked target is not a met
    target.
    """
    errors: dict[str, float] = {}
    unmatched: list[str] = []
    for tk, tv in target.items():
        ok = best_output_key(tk, outputs, registry) if is_number(tv) else None
        if ok is None:
            unmatched.append(tk)
            continue
        errors[ok] = abs(outputs[ok] - tv) / max(abs(tv), 1e-12)
    return errors, unmatched


def format_pct_off(
    outputs: dict,
    target: dict,
    registry: dict | None = None,
) -> str:
    """One-line ``key=value (X.X% off)`` diagnostic for chat/optimizer output.

    Returns ``"(no target key match)"`` when nothing resolves, ``""`` when
    there is no target.
    """
    if not target:
        return ""
    errors, _ = target_errors(outputs, target, registry)
    parts = [f"{ok}={outputs[ok]:.4g} ({err * 100:.1f}% off)" for ok, err in errors.items()]
    return "  ".join(parts) if parts else "(no target key match)"
