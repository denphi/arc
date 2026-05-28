"""``/services`` — manage sim2l services from the chat.

Sub-commands::

    /services                  (alias for /services status)
    /services status
    /services start  [name]
    /services stop   [name]
    /services restart [name]
    /services logs   [name]
"""

from __future__ import annotations

import asyncio

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, DIM, GREEN, RED, c, err, ok, warn


_VALID_SUBS = {"status", "start", "stop", "restart", "logs"}


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.services import (
        sim2l_available, status_all,
        start as svc_start, stop as svc_stop,
        _SERVICES, _log_path,
    )
    from arc.chat.io_utils import check_sim2l_services

    if not sim2l_available():
        err("sim2l is not installed. Install it with: pip install sim2l")
        return

    sub = (argv[0].lower() if argv else "status")
    target = (argv[1].lower() if len(argv) > 1 else None)

    if sub not in _VALID_SUBS:
        err(f"Unknown /services sub-command '{sub}'.")
        print(c("  Usage: /services [status|start|stop|restart|logs] [service]", DIM))
        print(c("  Services: cache  catalog  results  mcp  (omit to apply to core services)", DIM))
        return

    if sub == "status":
        st = status_all()
        print(c("  Sim2L services:", BOLD))
        for name, info in st.items():
            up = info["running"]
            pid_str = c(f"  PID {info['pid']}", DIM) if up else ""
            print(f"    {c(name, GREEN if up else RED)} {'✓' if up else '✗'}"
                  f"  {c(info['url'], DIM)}{pid_str}")
        return

    # All other paths take a list of names. Omitting the target applies to
    # core HTTP services only; MCP is optional and started explicitly.
    names = [target] if target else [
        name for name, cfg in _SERVICES.items() if not cfg.get("optional")
    ]
    for name in names:
        if name not in _SERVICES:
            err(f"Unknown service '{name}'. Choose from: {', '.join(_SERVICES)}")
            return

    loop = asyncio.get_event_loop()

    if sub == "start":
        for name in names:
            success, msg = await loop.run_in_executor(None, svc_start, name)
            (ok if success else err)(msg)
        state.sim2l_status = await loop.run_in_executor(None, check_sim2l_services)
        return

    if sub == "stop":
        for name in names:
            success, msg = await loop.run_in_executor(None, svc_stop, name)
            (ok if success else warn)(msg)
        state.sim2l_status = await loop.run_in_executor(None, check_sim2l_services)
        return

    if sub == "restart":
        for name in names:
            await loop.run_in_executor(None, svc_stop, name)
            success, msg = await loop.run_in_executor(None, svc_start, name)
            (ok if success else err)(msg)
        state.sim2l_status = await loop.run_in_executor(None, check_sim2l_services)
        return

    if sub == "logs":
        log_target = target or "catalog"
        log = _log_path(log_target)
        if not log.exists():
            warn(f"No log file for {log_target} at {log}")
            return
        try:
            lines = log.read_text().splitlines()[-40:]
            print(c(f"  Last {len(lines)} lines of {log}:", DIM))
            for line in lines:
                print(f"  {c(line, DIM)}")
        except Exception as exc:
            err(f"Could not read log: {exc}")


COMMANDS = [
    SlashCommand(
        name="services",
        summary="Manage sim2l services (catalog / results / cache / mcp)",
        handler=run,
        args_help="[status|start|stop|restart|logs] [name]",
    ),
]
