"""
Stdio MCP permission-relay server for arc-claude-code.

Architecture:
  Claude Code  <--stdio MCP-->  this server  <--Unix socket-->  CoderAgent
                                                                     |
                                                              permission_callback
                                                              (arc chat UI prompt)

The server owns two channels:
  1. stdio  — MCP JSON-RPC with Claude Code (permission_request in, verdict out)
  2. socket — simple newline-delimited JSON with the CoderAgent in the same process
               tree (request forwarded out, verdict received in)

Socket path is passed as argv[1].  The CoderAgent connects as a client,
reads forwarded requests, calls the user-facing callback, and writes verdicts.
"""

from __future__ import annotations

import asyncio
import json
import sys


_INIT_RESPONSE = {
    "jsonrpc": "2.0",
    "result": {
        "protocolVersion": "2024-11-05",
        "serverInfo": {"name": "arc-permission-relay", "version": "0.1.0"},
        "capabilities": {
            "experimental": {
                "claude/channel": {},
                "claude/channel/permission": {},
            }
        },
    },
}


async def _stdio_send(transport: asyncio.WriteTransport, obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    transport.write(line.encode())


async def run(socket_path: str) -> None:
    """
    Start the MCP server.  Reads MCP messages from stdin, forwards permission
    requests over the Unix socket, and sends verdicts back to Claude Code.
    """
    loop = asyncio.get_running_loop()

    # ── stdio (Claude Code side) ───────────────────────────────────────────
    reader = asyncio.StreamReader()
    proto = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: proto, sys.stdin.buffer)
    write_transport, _ = await loop.connect_write_pipe(asyncio.BaseProtocol, sys.stdout.buffer)

    def mcp_send(obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        write_transport.write(line.encode())

    # ── Unix socket (CoderAgent side) ─────────────────────────────────────
    # We start as the server; the CoderAgent connects as a client.
    sock_reader: asyncio.StreamReader | None = None
    sock_writer: asyncio.StreamWriter | None = None
    client_connected = asyncio.Event()

    async def _handle_client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal sock_reader, sock_writer
        sock_reader, sock_writer = r, w
        client_connected.set()

    server = await asyncio.start_unix_server(_handle_client, path=socket_path)

    async def _process_mcp() -> None:
        while True:
            try:
                raw = await reader.readline()
            except Exception:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            msg_id = msg.get("id")

            if method == "initialize":
                resp = dict(_INIT_RESPONSE)
                resp["id"] = msg_id
                mcp_send(resp)

            elif method == "notifications/initialized":
                pass

            elif method == "notifications/claude/channel/permission_request":
                params = msg.get("params", {})
                # Forward to CoderAgent via socket (best-effort — if no client, deny).
                verdict = "deny"
                if client_connected.is_set() and sock_writer and sock_reader:
                    try:
                        sock_writer.write((json.dumps(params) + "\n").encode())
                        await sock_writer.drain()
                        resp_raw = await asyncio.wait_for(sock_reader.readline(), timeout=120)
                        resp_obj = json.loads(resp_raw)
                        verdict = "allow" if resp_obj.get("allow") else "deny"
                    except Exception:
                        verdict = "deny"

                mcp_send({
                    "jsonrpc": "2.0",
                    "method": "notifications/claude/channel/permission",
                    "params": {
                        "request_id": params.get("request_id", ""),
                        "behavior": verdict,
                    },
                })

            elif msg_id is not None:
                mcp_send({"jsonrpc": "2.0", "id": msg_id, "result": {}})

    try:
        await _process_mcp()
    finally:
        server.close()
        await server.wait_closed()
