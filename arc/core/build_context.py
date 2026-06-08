"""Helpers for build-context workflow outputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


def render_build_contexts(contexts: list[Any] | tuple[Any, ...] | None, *, limit: int = 6000) -> str:
    """Render build contexts for prompt-only builders.

    The structured models remain the source of truth. This renderer is only a
    stable text view for builder backends that ultimately receive one prompt.
    """
    if not contexts:
        return "none"
    payload = [_dump(item) for item in contexts]
    return json.dumps(payload, indent=2, sort_keys=True, default=str)[:limit]


def build_context_cache_key(
    *,
    workflow_name: str,
    package_name: str | None,
    inputs: dict[str, Any],
    goal: Any,
    plan: Any,
) -> str:
    """Stable cache key for a build-context workflow result."""
    payload = {
        "workflow": workflow_name,
        "package_name": package_name,
        "inputs": _dump(inputs),
        "goal": _dump(goal),
        "plan": _dump(plan),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
