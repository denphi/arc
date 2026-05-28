"""Sim2L service manager for the ARC chat.

Starts, stops, and reports status on the sim2l microservices as background
daemons, using PID files under ~/.sim2l/pids/. The MCP server is optional and
can be started explicitly with ``/services start mcp``.

Only available when the ``sim2l`` package is installed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_SIM2L_HOME = Path(os.environ.get("SIM2L_HOME", Path.home() / ".sim2l" / "code"))

_SERVICES: dict[str, dict] = {
    "cache": {
        "module":   "sim2l.services.cache_service",
        "port_env": "SIM2L_CACHE_URL",
        "port":     8001,
        "db_flag":  "--db-path",
        "db_file":  "cache.db",
        "pid_file": "cache_service.pid",
        "log_file": "cache_service.log",
        "auto_start": True,
    },
    "catalog": {
        "module":   "sim2l.services.catalog_service",
        "port_env": "SIM2L_CATALOG_URL",
        "port":     8002,
        "db_flag":  "--db-path",
        "db_file":  "catalog.db",
        "pid_file": "catalog_service.pid",
        "log_file": "catalog_service.log",
        "auto_start": True,
    },
    "results": {
        "module":   "sim2l.services.results_service",
        "port_env": "SIM2L_RESULTS_URL",
        "port":     8003,
        "db_flag":  "--db-path",
        "db_file":  "results.db",
        "pid_file": "results_service.pid",
        "log_file": "results_service.log",
        "auto_start": True,
    },
    "mcp": {
        "module":   "sim2l.mcp.server",
        "port_env": "SIM2L_MCP_URL",
        "port":     8010,
        "pid_file": "mcp_service.pid",
        "log_file": "mcp_service.log",
        "auto_start": False,
        "optional": True,
    },
}

_PID_DIR = Path.home() / ".sim2l" / "pids"
_LOG_DIR = Path.home() / ".sim2l" / "logs"


def sim2l_available() -> bool:
    try:
        import sim2l  # noqa: F401
        return True
    except ImportError:
        return False


def _pid_path(name: str) -> Path:
    return _PID_DIR / _SERVICES[name]["pid_file"]


def _log_path(name: str) -> Path:
    return _LOG_DIR / _SERVICES[name]["log_file"]


def _read_pid(name: str) -> int | None:
    p = _pid_path(name)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def is_running(name: str) -> bool:
    pid = _read_pid(name)
    return pid is not None and _pid_alive(pid)


def service_url(name: str) -> str:
    cfg = _SERVICES[name]
    base = os.environ.get(cfg["port_env"], f"http://localhost:{cfg['port']}")
    return base.rstrip("/")


def _service_env() -> dict[str, str]:
    """Environment for locally started sim2l service processes.

    ARC chat logs in with SIM2L_USERNAME / SIM2L_PASSWORD. The sim2l
    services themselves seed the built-in admin account from
    SIM2L_ADMIN_PASSWORD. Bridge those settings for the common local case so
    `SIM2L_USERNAME=admin` + `SIM2L_PASSWORD=...` is enough when ARC starts
    the services.
    """
    env = os.environ.copy()
    username = env.get("SIM2L_USERNAME", "admin").strip().lower()
    password = env.get("SIM2L_PASSWORD")
    if username == "admin" and password and not env.get("SIM2L_ADMIN_PASSWORD"):
        env["SIM2L_ADMIN_PASSWORD"] = password
    env.setdefault(
        "SIM2L_CACHE_SERVICE_URL",
        env.get("SIM2L_CACHE_URL", "http://localhost:8001"),
    )
    env.setdefault(
        "SIM2L_CATALOG_SERVICE_URL",
        env.get("SIM2L_CATALOG_URL", "http://localhost:8002"),
    )
    env.setdefault(
        "SIM2L_RESULTS_SERVICE_URL",
        env.get("SIM2L_RESULTS_URL", "http://localhost:8003"),
    )
    return env


def status_all() -> dict[str, dict]:
    """Return {name: {running, pid, port, url}} for every service."""
    result = {}
    for name, cfg in _SERVICES.items():
        pid = _read_pid(name)
        alive = pid is not None and _pid_alive(pid)
        if pid and not alive:
            _pid_path(name).unlink(missing_ok=True)
        result[name] = {
            "running": alive,
            "pid":     pid if alive else None,
            "port":    cfg["port"],
            "url":     service_url(name),
        }
    return result


def start(name: str, db_path: str | None = None) -> tuple[bool, str]:
    """Start a service as a background daemon.

    Returns (success, message).
    """
    if not sim2l_available():
        return False, "sim2l package is not installed"

    if is_running(name):
        return True, f"{name} is already running (PID {_read_pid(name)})"

    cfg = _SERVICES[name]
    _PID_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    if cfg.get("optional") and name == "mcp":
        cmd = [
            sys.executable, "-m", cfg["module"],
            "--transport", os.environ.get("SIM2L_MCP_TRANSPORT", "streamable-http"),
        ]
        for env_key, flag in (
            ("SIM2L_CATALOG_URL", "--catalog-url"),
            ("SIM2L_RESULTS_URL", "--results-url"),
            ("SIM2L_CACHE_URL", "--cache-url"),
        ):
            if os.environ.get(env_key):
                cmd.extend([flag, os.environ[env_key]])
    else:
        if db_path is None:
            db_path = str(Path.home() / ".sim2l" / cfg["db_file"])

        cmd = [
            sys.executable, "-m", cfg["module"],
            "--host", "127.0.0.1",
            "--port", str(cfg["port"]),
            "--backend", "sqlite",
            cfg["db_flag"], db_path,
        ]

    log_path = _log_path(name)
    try:
        with open(log_path, "a") as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=_service_env(),
                start_new_session=True,
            )
        _pid_path(name).write_text(str(proc.pid))

        # Give the service a moment to fail fast (port already in use, etc.)
        time.sleep(0.5)
        if proc.poll() is not None:
            _pid_path(name).unlink(missing_ok=True)
            return False, f"{name} exited immediately — check {log_path}"

        if cfg.get("optional"):
            return True, f"{name} started (PID {proc.pid})"
        return True, f"{name} started (PID {proc.pid}, port {cfg['port']})"
    except Exception as exc:
        return False, f"Failed to start {name}: {exc}"


def stop(name: str) -> tuple[bool, str]:
    """Stop a running service. Returns (success, message)."""
    pid = _read_pid(name)
    if pid is None or not _pid_alive(pid):
        _pid_path(name).unlink(missing_ok=True)
        return False, f"{name} is not running"

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 3s for clean exit
        for _ in range(30):
            time.sleep(0.1)
            if not _pid_alive(pid):
                break
        _pid_path(name).unlink(missing_ok=True)
        return True, f"{name} stopped (PID {pid})"
    except Exception as exc:
        return False, f"Error stopping {name}: {exc}"


def start_all(*, include_optional: bool = False) -> list[tuple[str, bool, str]]:
    results = []
    for name, cfg in _SERVICES.items():
        if cfg.get("optional") and not include_optional:
            continue
        ok, msg = start(name)
        results.append((name, ok, msg))
    return results


def stop_all() -> list[tuple[str, bool, str]]:
    results = []
    for name in _SERVICES:
        ok, msg = stop(name)
        results.append((name, ok, msg))
    return results
