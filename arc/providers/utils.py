"""Shared utilities for ARC provider implementations.

Most providers wrap the response in a markdown fence (```json … ```) when
asked for structured output. This module centralises the stripping logic so
the three providers don't each open-code the same five-line trim.
"""

from __future__ import annotations


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
