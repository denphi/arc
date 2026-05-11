"""
Entry point: spawned by Claude Code as an MCP server subprocess.
argv[1] = Unix socket path used to talk back to the CoderAgent.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("arc_cc_perm_server", _HERE / "permission_server.py")
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)

if __name__ == "__main__":
    socket_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/arc-permission.sock"
    asyncio.run(_mod.run(socket_path))
