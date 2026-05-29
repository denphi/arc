"""Run the standalone ARC browser UI."""

from __future__ import annotations

import argparse
import os


DEFAULT_HOST = "127.0.0.1"
# 8080 collides with common local containers (Docker-proxied Jupyter, etc.)
# which often bind *:8080 on IPv6 and shadow localhost; default to a free port.
DEFAULT_PORT = 8888


def _env_host() -> str:
    return os.environ.get("ARC_UI_HOST", DEFAULT_HOST)


def _env_port() -> int:
    raw = os.environ.get("ARC_UI_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def run_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    reload: bool = False,
) -> None:
    import uvicorn

    uvicorn.run(
        "arc.ui.server:app",
        host=host,
        port=port,
        reload=reload,
    )


def main() -> None:
    # CLI flags win over env (ARC_UI_HOST / ARC_UI_PORT) which win over the
    # built-in defaults. .env is loaded by create_app() before the app builds.
    parser = argparse.ArgumentParser(prog="python -m arc.ui")
    parser.add_argument("--host", default=_env_host())
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
