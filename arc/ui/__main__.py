"""Run the standalone ARC browser UI."""

from __future__ import annotations

import argparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


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
    parser = argparse.ArgumentParser(prog="python -m arc.ui")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
