from __future__ import annotations

from pathlib import Path
from typing import Any


def load_sim2l_schema(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ARC-normalized input/output schema maps from a sim2l.yaml file."""
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
            entry = {
                "type": _normalize_type(raw.get("type", "Number")),
                "description": raw.get("description", name),
            }
            if include_default:
                entry["default"] = raw.get("default", 1.0)
        else:
            entry = {"type": "Number", "description": name}
            if include_default:
                entry["default"] = raw if raw is not None else 1.0
        normalized[name] = entry
    return normalized


def _normalize_type(value: Any) -> str:
    if str(value).lower() in {"number", "float", "integer", "int"}:
        return "Number"
    return str(value)
