"""Tiny env-flag parsing helper.

Three callers (``ARC_CHAT_V2``, ``ARC_TRUST_PROJECT_SKILLS``,
``ARC_TRUST_PROJECT_AGENTS``) were each open-coding the same
``os.environ.get(name, "").strip().lower() in {...}`` check. This
module is the single source of truth.
"""

from __future__ import annotations

import os


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str) -> bool:
    """Return True when the env var ``name`` is set to a truthy value.

    Truthy: ``1``, ``true``, ``yes``, ``on`` (case-insensitive, with
    surrounding whitespace tolerated). Anything else, or unset, is False.
    """
    value = os.environ.get(name, "").strip().lower()
    return value in _TRUTHY
