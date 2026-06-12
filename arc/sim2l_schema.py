from __future__ import annotations

from pathlib import Path
from typing import Any


def load_sim2l_schema(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ARC-normalized input/output schema maps from a sim2l.yaml file.

    Field entries are passed through nearly verbatim — ``units`` / ``min``
    / ``max`` / ``choices`` and any other declared keys are preserved so
    consumers (catalog indexing, input reconciliation, optimizers) see the
    schema the author wrote. Only the type name is normalized, and a
    ``description`` is defaulted to the field name.

    A field that declares no ``default`` gets none — fabricating one (the
    old behaviour injected ``1.0``) silently runs simulations at parameter
    values the author never chose, and is nonsense for non-numeric types.
    """
    yaml_path = Path(path)
    if yaml_path.is_dir():
        yaml_path = yaml_path / "sim2l.yaml"
    if not yaml_path.exists():
        return {}, {}

    import yaml

    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return (
        _normalize_fields(spec.get("inputs", {}), include_default=True),
        _normalize_fields(spec.get("outputs", {}), include_default=False),
    )


def _normalize_fields(fields: dict[str, Any], include_default: bool) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, raw in (fields or {}).items():
        if isinstance(raw, dict):
            entry = dict(raw)  # preserve units / min / max / choices / …
            entry["type"] = _normalize_type(raw.get("type", "Number"))
            entry.setdefault("description", name)
            if not include_default:
                entry.pop("default", None)
        else:
            # Shorthand ``name: value`` — the value is the default.
            entry = {"type": _value_type(raw), "description": name}
            if include_default and raw is not None:
                entry["default"] = raw
        normalized[name] = entry
    return normalized


# sim2l's registered field types (sim2l/schema/registry.py). Shared by this
# loader and the sim2l adapter so the same artifact gets identical catalog
# schema types regardless of which register path pushed it.
CANONICAL_FIELD_TYPES: frozenset[str] = frozenset({
    "Integer", "Number", "Text", "Array", "Image", "Element",
    "Boolean", "List", "Dict",
})
FIELD_TYPE_ALIASES: dict[str, str] = {
    "number": "Number", "float": "Number", "double": "Number",
    "integer": "Integer", "int": "Integer",
    "text": "Text", "string": "Text", "str": "Text",
    "boolean": "Boolean", "bool": "Boolean",
    "array": "Array", "list": "List",
    "dict": "Dict", "object": "Dict", "map": "Dict",
    "image": "Image", "element": "Element",
}


def normalize_field_type(value: Any) -> str:
    """Map a declared type name to a canonical sim2l field type.

    Canonical names pass through; common spellings ("float", "string",
    "bool") map to their canonical type; unknown names fall back to Text —
    never silently to Number.
    """
    name = str(value or "Number").strip()
    if name in CANONICAL_FIELD_TYPES:
        return name
    return FIELD_TYPE_ALIASES.get(name.lower(), "Text")


def _normalize_type(value: Any) -> str:
    return normalize_field_type(value)


def _value_type(value: Any) -> str:
    """Infer a field type from a shorthand default value."""
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, str):
        return "Text"
    if isinstance(value, (list, tuple)):
        return "List"
    if isinstance(value, dict):
        return "Dict"
    return "Number"
