"""Shared utilities for ARC provider implementations.

Most providers wrap the response in a markdown fence (```json … ```) when
asked for structured output. This module centralises the stripping logic so
the three providers don't each open-code the same five-line trim.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple


# Review item #T17: each provider instance previously kept its own
# ``_native_json_supported`` toggle. When ARC builds a fresh provider per
# workflow run (the default in ``ResearchWorkflow``), every new instance
# repeats the failing API call before flipping the toggle, defeating the
# point of the cache. Lifting the registry to module-level — keyed by
# (provider_name, model) — means a single capability probe per
# (provider, model) per process.
_capability_lock = threading.Lock()
_native_json_support: Dict[Tuple[str, str], bool] = {}


def get_native_json_support(provider: str, model: str) -> Optional[bool]:
    """Return cached native-JSON support for (provider, model), or None.

    None means "not yet probed" — the caller should attempt the native
    path. True/False is the last observed outcome.
    """
    with _capability_lock:
        return _native_json_support.get((provider, model))


def set_native_json_support(provider: str, model: str, supported: bool) -> None:
    """Record the outcome of a native-JSON probe. Idempotent."""
    with _capability_lock:
        _native_json_support[(provider, model)] = supported


def reset_native_json_support() -> None:
    """Drop the cache — used by tests."""
    with _capability_lock:
        _native_json_support.clear()


def strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence from `text`.

    Handles the common shapes that the major LLM providers produce when asked
    for structured output:

        ```json\n{...}\n```
        ```\n{...}\n```
        plain JSON without fences

    Returns the trimmed content. If `text` doesn't start with a fence it is
    returned unchanged (after a strip).
    """
    text = text.strip()
    if not text.startswith("```"):
        return text

    # Drop the opening fence and any language tag (json/yaml/python/etc.).
    # Then drop the trailing fence and any trailing whitespace.
    inner = text.split("```", 2)[1]
    # Skip a single leading language tag on the first line, e.g. "json\n{..."
    newline = inner.find("\n")
    if newline >= 0:
        first_line = inner[:newline].strip()
        if first_line and first_line.isalpha():
            inner = inner[newline + 1 :]
    inner = inner.rsplit("```", 1)[0]
    return inner.strip()
