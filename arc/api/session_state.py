"""HTTP-side session state persistence.

The chat layer persists a fixed schema (``session.json``: goal,
iteration, run_history, target, …) via :mod:`arc.chat.session_io`.
The strategy/recipe surface this PR exposes is a separate concern —
it lives in its own file, ``session_state.json``, alongside the legacy
one. That keeps the chat-side I/O backwards compatible while letting
the HTTP API mutate the new keys per-request without coupling to the
chat-side ``ChatState.persist()`` machinery.

Schema::

    {
      "strategy_overrides":   {role: impl, …},
      "recipe_applied":       {role: impl, …},
      "active_recipe":        str | null,
      "recipe_suggested":     [recipe_name, …],
    }

Missing file → empty state. Writes are atomic-ish: we write the whole
file each time because it's tiny (<1 KB). A future refactor can move
the chat layer to also read/write this file so the two surfaces share
configuration; this module is the on-ramp for that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arc.session import _session_dir  # type: ignore[attr-defined]


_FILE_NAME = "session_state.json"


# Keys we persist. Anything not in this set is dropped on load — gives
# us a safe rename path for the future.
_PERSISTED_KEYS = frozenset({
    "strategy_overrides",
    "recipe_applied",
    "active_recipe",
    "recipe_suggested",
})


def _state_path(session_id: str) -> Path:
    """Resolve the on-disk path. Reuses the chat layer's session-dir
    helper so the two layers always agree on where the session lives.
    """
    return _session_dir(session_id) / _FILE_NAME


def load_state(session_id: str) -> dict[str, Any]:
    """Return the persisted state for ``session_id`` (or an empty dict).

    Tolerant of missing files, malformed JSON, and unknown keys —
    every failure path returns an empty dict so callers always see a
    well-formed structure.
    """
    path = _state_path(session_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in _PERSISTED_KEYS}


def save_state(session_id: str, state: dict[str, Any]) -> Path:
    """Write the persisted-keys subset of ``state`` to disk.

    Returns the path written. Empty values (``None``, empty dict,
    empty list) are dropped to keep the file small and unambiguous —
    "key absent" and "key explicitly empty" mean the same thing.
    """
    path = _state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    for key in _PERSISTED_KEYS:
        if key not in state:
            continue
        value = state[key]
        if not value:
            # Skip falsy: empty dict / empty list / None.
            continue
        payload[key] = value
    if not payload:
        # No interesting state; remove the file if it exists so we
        # don't leak empty-state files into the user's sessions.
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return path
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def clear_state(session_id: str) -> None:
    """Remove the state file for ``session_id`` (no error if absent)."""
    path = _state_path(session_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
