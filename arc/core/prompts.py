"""Prompt-template registry — let domain packages override per-role prompts.

The ideator (and any future LLM-driven agent) calls :func:`find_prompt`
with its role name and the active goal's domain. We scan every package's
``prompts/`` directory for a markdown file whose first ``# H1`` matches
``<domain>_<role>`` (preferred) or just ``<role>`` (fallback). The file
body becomes the prompt template, which the caller fills with
``str.format(**fields)``.

Lookup precedence (highest first):

  1. ``memory["prompt_overrides"][role]`` — raw template string,
     usually set by a future ``/prompt`` slash command. Highest so a
     user can experiment without editing files.
  2. Package prompts matching ``<domain>_<role>``. Example: domain
     ``materials`` + role ``hypothesis_generation`` matches the
     ``materials_hypothesis_generation`` H1 shipped in arc-materials.
  3. Package prompts matching just ``<role>`` (any domain) — useful
     for cross-domain helpers like ``review_results``.
  4. None — caller falls back to its built-in default template.

Templates are parsed lazily and cached by file path + mtime so editing
a prompt during a chat session picks up immediately without restart.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Match the first ``# Heading`` line in a markdown file.
_H1_RE = re.compile(r"^#\s+(\S[^\n]*)\s*$", re.MULTILINE)


# ── Discovery ──────────────────────────────────────────────────────────


def _bundled_packages_root() -> Path:
    return Path(__file__).resolve().parent.parent / "packages"


def _candidate_prompt_files() -> list[Path]:
    """Every ``*.md`` under ``arc/packages/*/prompts/``."""
    root = _bundled_packages_root()
    if not root.exists():
        return []
    return sorted(root.glob("*/prompts/*.md"))


def _parse_prompt_file(path: Path) -> tuple[str, str] | None:
    """Return ``(prompt_name, body)`` for a single markdown file.

    ``prompt_name`` is the first H1's text. ``body`` is everything after
    that line, with any leading whitespace stripped. Files missing an
    H1 are skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _H1_RE.search(text)
    if not match:
        return None
    name = match.group(1).strip()
    body = text[match.end():].lstrip("\n")
    return name, body


@lru_cache(maxsize=64)
def _cached_index(mtime_bucket: float) -> dict[str, str]:  # noqa: ARG001 — cache key
    """Build {prompt_name → body} from every package prompts dir.

    ``mtime_bucket`` is the floor of the max mtime across discovered
    files in seconds — bumping that invalidates the cache so edits
    during a chat session are picked up without restart.
    """
    index: dict[str, str] = {}
    for path in _candidate_prompt_files():
        parsed = _parse_prompt_file(path)
        if parsed is None:
            continue
        name, body = parsed
        # First package to declare a given name wins. That's stable —
        # arc-materials' ``materials_hypothesis_generation`` doesn't
        # collide with anything else because it's domain-prefixed.
        index.setdefault(name, body)
    return index


def _current_mtime_bucket() -> float:
    """Floor of the latest mtime across prompt files.

    Bucketed to whole seconds so cache invalidates exactly when a file
    is edited, without re-reading on every call.
    """
    latest = 0.0
    for path in _candidate_prompt_files():
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return float(int(latest))


def list_prompts() -> list[str]:
    """Names of every prompt the registry can resolve."""
    return sorted(_cached_index(_current_mtime_bucket()).keys())


def get_prompt(name: str) -> str | None:
    """Return the prompt body for ``name``, or ``None`` if not found."""
    return _cached_index(_current_mtime_bucket()).get(name)


# ── Resolution ─────────────────────────────────────────────────────────


def find_prompt(
    role: str,
    domain: str | None = None,
    *,
    overrides: dict[str, str] | None = None,
) -> str | None:
    """Pick the best prompt template for ``role`` + ``domain``.

    Returns the *unformatted* template body (or ``None`` if nothing
    matches). The caller fills placeholders with ``str.format`` — that
    keeps the discovery layer free of agent-specific field names.

    Order:

    1. Runtime override on memory.
    2. ``<domain>_<role>`` package prompt.
    3. ``<role>`` package prompt (any domain).
    4. ``None``.
    """
    if overrides and role in overrides and overrides[role]:
        return overrides[role]

    if domain:
        domain_key = f"{domain.lower()}_{role.lower()}"
        body = get_prompt(domain_key)
        if body is not None:
            return body

    role_key = role.lower()
    body = get_prompt(role_key)
    if body is not None:
        return body

    return None


# ── Formatting helper ──────────────────────────────────────────────────


def safe_format(template: str, **fields: Any) -> str:
    """Format ``template`` with ``fields``, ignoring unknown placeholders.

    Standard ``str.format`` raises ``KeyError`` for missing placeholders;
    we'd rather render the literal ``{foo}`` than break the chat. Useful
    because prompt files written by domain authors may reference fields
    the caller doesn't supply.
    """
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(_SafeDict(fields))
