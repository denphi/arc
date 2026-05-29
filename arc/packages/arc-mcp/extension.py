"""MCP extension — exposes Model Context Protocol tools as ARC skills.

Core defines ``ExtensionContract``; this *implementation* lives in a
package (the package-first principle). When enabled, the extension
connects to one or more MCP servers ("ext-apps"), lists each server's
tools, and registers every tool as an ARC skill named
``mcp::<app>::<tool>``. An agent/skill caller then invokes the tool
through the normal skill path with no per-tool code.

**Multi-app (ext-apps):** the extension supports *several* named MCP
servers at once. Each app is configured independently (transport + URL or
stdio command); tools are namespaced by app so two servers can expose a
tool of the same name without collision. A single ``server_url`` is the
degenerate one-app case.

**Connection lifetime:** MCP's client transports are async context
managers, but tools are called long after ``initialize`` returns. We hold
the contexts open with an ``AsyncExitStack`` entered during
``initialize`` and closed in ``shutdown`` — so a session stays live for
the extension's lifetime.

THREAT MODEL: an MCP server is remote code the user opted into by
configuring it. Tool calls run on that server. Treat configured servers
as trusted endpoints; the extension does not sandbox them.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from arc.contracts.extension import ExtensionContract

logger = logging.getLogger(__name__)


def _parse_apps(config: dict) -> list[dict]:
    """Normalise the extension config into a list of app dicts.

    Accepts three shapes, in order of preference:
      * ``apps: [{name, transport, url|command, headers, args, env}, …]``
        — the multi-app (ext-apps) form;
      * a single ``server_url`` (+ optional ``headers``) — one HTTP app
        named ``default``;
      * a single ``command`` (+ optional ``args``) — one stdio app.
    Returns ``[]`` when nothing is configured (extension loads but idle).
    """
    apps = config.get("apps")
    if isinstance(apps, list) and apps:
        out = []
        for i, app in enumerate(apps):
            if not isinstance(app, dict):
                continue
            app = dict(app)
            app.setdefault("name", app.get("name") or f"app{i}")
            out.append(app)
        return out

    if config.get("server_url"):
        return [{
            "name": config.get("name", "default"),
            "transport": "http",
            "url": config["server_url"],
            "headers": config.get("headers") or {},
        }]
    if config.get("command"):
        return [{
            "name": config.get("name", "default"),
            "transport": "stdio",
            "command": config["command"],
            "args": config.get("args") or [],
            "env": config.get("env") or {},
        }]
    return []


class _McpToolSkill:
    """ARC skill wrapping one MCP tool on one app.

    Satisfies the skill contract (``async execute(inputs, context)``).
    Forwards ``inputs`` to the MCP ``tools/call`` and returns a plain dict.
    """

    def __init__(self, app_name: str, tool_name: str, session: Any, description: str = ""):
        self.name = f"mcp::{app_name}::{tool_name}"
        self.description = description or f"MCP tool {tool_name} on {app_name}"
        self._app = app_name
        self._tool = tool_name
        self._session = session

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        try:
            result = await self._session.call_tool(self._tool, arguments=inputs or {})
        except Exception as exc:  # noqa: BLE001 — surface as a result, don't crash the loop
            logger.debug("mcp tool %s failed: %s", self.name, exc)
            return {"skill": self.name, "ok": False, "error": str(exc)}
        return {"skill": self.name, "ok": not getattr(result, "isError", False),
                "content": _content_to_plain(result)}


def _content_to_plain(result: Any) -> Any:
    """Best-effort: reduce an MCP CallToolResult to JSON-friendly data."""
    # Prefer structured content when the server provides it.
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else getattr(block, "model_dump", lambda: str(block))())
    return parts if len(parts) != 1 else parts[0]


class McpExtension(ExtensionContract):
    name = "mcp"

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, Any] = {}
        self._skill_names: list[str] = []

    async def initialize(self, config: dict, registry: Any = None) -> None:
        apps = _parse_apps(config or {})
        if not apps:
            logger.info("mcp extension enabled but no apps/server_url configured — idle.")
            return
        if registry is None:
            logger.warning("mcp extension has no registry to register skills into — idle.")
            return

        # Import the SDK lazily so the package only needs `mcp` installed
        # when the extension is actually enabled.
        try:
            from mcp import ClientSession
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp extension enabled but the `mcp` SDK isn't importable (%s).", exc)
            return

        self._stack = AsyncExitStack()
        for app in apps:
            try:
                await self._connect_app(app, ClientSession, registry)
            except Exception as exc:  # noqa: BLE001 — one bad app shouldn't kill the rest
                logger.warning("mcp app %r failed to connect: %s", app.get("name"), exc)

        logger.info(
            "mcp extension ready: %d app(s), %d tool skill(s) registered.",
            len(self._sessions), len(self._skill_names),
        )

    async def _connect_app(self, app: dict, ClientSession, registry) -> None:
        app_name = app["name"]
        transport = (app.get("transport") or "http").lower()

        if transport in ("http", "streamable-http", "streamable_http"):
            from mcp.client.streamable_http import streamablehttp_client
            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(app["url"], headers=app.get("headers") or None)
            )
        elif transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client
            params = StdioServerParameters(
                command=app["command"], args=app.get("args") or [], env=app.get("env") or None,
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        else:
            raise ValueError(f"unsupported mcp transport: {transport!r}")

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[app_name] = session

        tools = await session.list_tools()
        for tool in getattr(tools, "tools", []) or []:
            skill = _McpToolSkill(
                app_name, tool.name, session, getattr(tool, "description", "") or "",
            )
            registry.register_skill(skill.name, skill)
            self._skill_names.append(skill.name)

    async def shutdown(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("mcp extension shutdown error: %s", exc)
            self._stack = None
        self._sessions.clear()
        self._skill_names.clear()
