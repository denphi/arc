"""Render a ``CheckReport`` for the terminal or JSON output.

Kept separate from ``check.py`` so the report can be consumed by other
front-ends (web UI, CI scripts) without dragging ANSI codes through them.
"""

from __future__ import annotations

import json
from typing import Literal

from arc.chat.check import CheckReport, CheckItem
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, c


_MARK = {"ok": "✓", "warning": "⚠", "blocked": "✗"}
_COLOR = {"ok": GREEN, "warning": YELLOW, "blocked": RED}


def render_ansi(report: CheckReport) -> str:
    """ANSI table for a TTY."""
    lines: list[str] = []
    lines.append(c("ARC chat — config check", BOLD, CYAN))
    lines.append(c("─" * 60, DIM))
    name_w = max((len(it.name) for it in report.items), default=10)
    for it in report.items:
        mark = c(_MARK[it.verdict], _COLOR[it.verdict], BOLD)
        name = it.name.ljust(name_w)
        if it.detail:
            lines.append(f"  {mark} {name}  {c(it.detail, DIM)}")
        else:
            lines.append(f"  {mark} {name}")
    lines.append(c("─" * 60, DIM))
    overall = report.overall
    overall_label = {
        "ok":      c("ready", GREEN, BOLD),
        "warning": c("warning", YELLOW, BOLD),
        "blocked": c("blocked", RED, BOLD),
    }[overall]
    lines.append(f"  Verdict: {overall_label}")
    return "\n".join(lines)


def render_json(report: CheckReport) -> str:
    """JSON output for CI / piping. Stable schema."""
    payload = {
        "overall": report.overall,
        "exit_code": report.exit_code,
        "items": [
            {
                "name": it.name,
                "verdict": it.verdict,
                "detail": it.detail,
                "info": it.info,
            }
            for it in report.items
        ],
    }
    return json.dumps(payload, indent=2)


def render(report: CheckReport, fmt: Literal["ansi", "json"] = "ansi") -> str:
    if fmt == "json":
        return render_json(report)
    return render_ansi(report)
