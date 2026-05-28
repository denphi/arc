"""Startup helpers — service health checks, signal handling, banner.

Extracted from ``arc/chat/loop.py`` in Phase 1 with no behaviour change.
"""

from __future__ import annotations

import asyncio
import os
import signal

from arc.chat.ui import (
    c, BOLD, CYAN, DIM, GREEN, RED, YELLOW,
)


# Service registry. Each entry: (env var override, default URL).
_SIM2L_SERVICES: dict[str, tuple[str, str]] = {
    "catalog": ("SIM2L_CATALOG_URL", "http://localhost:8002"),
    "results": ("SIM2L_RESULTS_URL", "http://localhost:8003"),
    "cache":   ("SIM2L_CACHE_URL",   "http://localhost:8001"),
}


def check_sim2l_services() -> dict[str, bool]:
    """Ping each sim2l service ``/health`` endpoint.

    Returns ``{name: reachable}`` with a 1-second timeout per service so a
    fully-down cluster still returns in <3s.
    """
    import requests as _req
    status: dict[str, bool] = {}
    for name, (env_key, default_url) in _SIM2L_SERVICES.items():
        url = os.environ.get(env_key, default_url)
        try:
            r = _req.get(f"{url}/health", timeout=1)
            status[name] = r.status_code == 200
        except Exception:
            status[name] = False
    return status


def install_sigint_handler() -> None:
    """Make Ctrl+C cancel the current asyncio task instead of killing the process.

    Tolerant of being called in contexts where there's no running loop yet
    (the loop is fetched at handler-fire time, not install time).
    """
    def _handler(sig, frame):  # pragma: no cover — signal-handler bodies are hard to unit-test
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        for task in asyncio.all_tasks(loop):
            if not task.done() and task != asyncio.current_task():
                task.cancel()

    signal.signal(signal.SIGINT, _handler)


def print_banner(
    provider: str | None,
    model: str | None,
    base_url: str | None,
    session_id: str,
    coder_backend: str = "builder",
    sim2l_status: dict | None = None,
) -> None:
    """Render the chat-startup banner with colour-coded service status."""
    if sim2l_status is None:
        svc_line = c("  (not checked)", DIM)
    else:
        parts = []
        for name, up in sim2l_status.items():
            parts.append(f"{c(name, GREEN if up else RED)} {'✓' if up else '✗'}")
        svc_line = "  " + "  ".join(parts)

    print(f"""
{c('ARC', BOLD, CYAN)} {c('Autonomous Research Coder', DIM)}
{c('━' * 60, CYAN)}
  {c('Session', BOLD)}   {c(session_id, BOLD + CYAN)}
  {c('Data', DIM)}      {c(f'~/.sim2l/code/{session_id}/', DIM)}
{c('─' * 60, DIM)}
  Provider : {c(provider or 'stub (no LLM)', YELLOW)}
  Model    : {c(model or 'auto', YELLOW)}
  Endpoint : {c(base_url or 'n/a', DIM)}
  Coder    : {c(coder_backend, YELLOW)}
{c('─' * 60, DIM)}
  Sim2L    :{svc_line}
{c('─' * 60, DIM)}
Type your research goal — subsequent inputs refine it (add constraints, boundaries, etc.)
Use /run <new goal> to start a completely fresh goal.
Input supports history with Up/Down and Emacs editing keys such as Ctrl+A, Ctrl+E, Ctrl+K.
""")
