"""arc-mcp extension: registers MCP tools as ARC skills (Item 2).

Mocks the MCP SDK entirely — no live server. Verifies:
  * multi-app (ext-apps) config registers namespaced ``mcp::<app>::<tool>``
    skills via the registry;
  * a registered skill's ``execute`` forwards to the session's
    ``call_tool`` and returns plain content;
  * the extension is idle (no crash) without config;
  * one failing app doesn't prevent the others from loading.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from arc.core.loader import _import_class
from arc.core.registry import ComponentRegistry

pytestmark = pytest.mark.chat


_McpExt = _import_class("arc.packages.arc-mcp.extension:McpExtension")
_mod = sys.modules[_McpExt.__module__]


# ── fakes for the MCP SDK ───────────────────────────────────────────────


class _FakeSession:
    """Stands in for mcp.ClientSession (an async context manager)."""

    def __init__(self, tools, *, calls):
        self._tools = tools
        self._calls = calls  # shared list to record call_tool invocations

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        tools = [SimpleNamespace(name=n, description=f"{n} desc") for n in self._tools]
        return SimpleNamespace(tools=tools)

    async def call_tool(self, name, arguments=None):
        self._calls.append((name, arguments))
        return SimpleNamespace(isError=False, structuredContent={"echo": arguments},
                               content=[])


@asynccontextmanager
async def _fake_http(url, headers=None):
    yield ("read", "write", lambda: None)


def _install_fake_sdk(monkeypatch, tools_by_app, calls):
    """Patch the SDK symbols the extension imports lazily."""
    # ClientSession(read, write) -> _FakeSession; pick tools by call order.
    made = {"i": 0}
    app_tool_lists = list(tools_by_app.values())

    def _session_factory(read, write):
        idx = made["i"]
        made["i"] += 1
        return _FakeSession(app_tool_lists[idx], calls=calls)

    import mcp
    monkeypatch.setattr(mcp, "ClientSession", _session_factory, raising=False)
    import mcp.client.streamable_http as http_mod
    monkeypatch.setattr(http_mod, "streamablehttp_client", _fake_http, raising=False)


# ── tests ───────────────────────────────────────────────────────────────


def test_idle_without_config():
    ext = _McpExt()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({}, reg))
    assert reg.list_skills() == []
    asyncio.run(ext.shutdown())


def test_registers_namespaced_skills_for_multiple_apps(monkeypatch):
    calls: list = []
    tools_by_app = {"files": ["read_file", "write_file"], "search": ["query"]}
    _install_fake_sdk(monkeypatch, tools_by_app, calls)

    ext = _McpExt()
    reg = ComponentRegistry()
    config = {"apps": [
        {"name": "files", "transport": "http", "url": "http://a/mcp"},
        {"name": "search", "transport": "http", "url": "http://b/mcp"},
    ]}
    asyncio.run(ext.initialize(config, reg))

    names = set(reg.list_skills())
    assert names == {
        "mcp::files::read_file", "mcp::files::write_file", "mcp::search::query",
    }
    asyncio.run(ext.shutdown())


def test_skill_execute_calls_tool(monkeypatch):
    calls: list = []
    _install_fake_sdk(monkeypatch, {"files": ["read_file"]}, calls)

    ext = _McpExt()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize(
        {"apps": [{"name": "files", "transport": "http", "url": "http://a/mcp"}]}, reg,
    ))
    skill = reg.get_skill("mcp::files::read_file")
    result = asyncio.run(skill.execute({"path": "/tmp/x"}, context=None))

    assert calls == [("read_file", {"path": "/tmp/x"})]
    assert result["ok"] is True
    assert result["content"] == {"echo": {"path": "/tmp/x"}}
    asyncio.run(ext.shutdown())


def test_one_bad_app_does_not_block_others(monkeypatch):
    calls: list = []
    # 'bad' app uses an unsupported transport → raises in _connect_app;
    # 'good' app still registers.
    _install_fake_sdk(monkeypatch, {"good": ["t"]}, calls)
    ext = _McpExt()
    reg = ComponentRegistry()
    config = {"apps": [
        {"name": "bad", "transport": "carrier-pigeon", "url": "http://x"},
        {"name": "good", "transport": "http", "url": "http://a/mcp"},
    ]}
    asyncio.run(ext.initialize(config, reg))
    assert reg.list_skills() == ["mcp::good::t"]
    asyncio.run(ext.shutdown())


def test_server_url_shortcut_is_one_app(monkeypatch):
    calls: list = []
    _install_fake_sdk(monkeypatch, {"default": ["ping"]}, calls)
    ext = _McpExt()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({"server_url": "http://a/mcp"}, reg))
    assert reg.list_skills() == ["mcp::default::ping"]
    asyncio.run(ext.shutdown())
