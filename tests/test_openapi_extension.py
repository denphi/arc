"""arc-openapi extension: OpenAPI operations as ARC skills (Item 4a).

Mocks ``requests`` — no live HTTP. Verifies spec fetch → skill
registration (``openapi::<spec>::<op>``), operation invocation (path-param
substitution, query vs body, auth header), the SSRF host guard, and
multi-spec namespacing.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from arc.core.loader import _import_class
from arc.core.registry import ComponentRegistry

pytestmark = pytest.mark.chat

_Ext = _import_class("arc.packages.arc-openapi.extension:OpenApiExtension")
_mod = sys.modules[_Ext.__module__]


_SPEC = {
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/items/{item_id}": {
            "get": {"operationId": "get_item", "summary": "Get an item"},
        },
        "/items": {
            "post": {"operationId": "create_item"},
        },
    },
}


class _FakeResp:
    def __init__(self, *, json_data=None, text="", status=200, ok=True):
        self._json = json_data
        self.text = text
        self.status_code = status
        self.ok = ok

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _allow_public_hosts(monkeypatch):
    """Treat the test's example.com hosts as public.

    ``_host_is_private`` resolves hosts via DNS and treats *unresolvable*
    names as private (an SSRF-safe default). Test domains don't resolve, so
    stub the check to flag only literal loopback/private hosts — keeping the
    127.0.0.1 rejection test meaningful.
    """
    import arc.api.security as sec

    def fake_private(host: str) -> bool:
        return host in ("127.0.0.1", "localhost", "::1") or host.startswith("10.") \
            or host.startswith("169.254.") or host.startswith("192.168.")
    monkeypatch.setattr(sec, "_host_is_private", fake_private)


def _install_requests(monkeypatch, *, spec=_SPEC, recorder=None):
    import requests
    _allow_public_hosts(monkeypatch)

    def fake_get(url, timeout=None, **kw):
        return _FakeResp(json_data=spec)

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if recorder is not None:
            recorder.append({"method": method, "url": url, "params": params,
                             "json": json, "headers": headers or {}})
        return _FakeResp(json_data={"ok": True, "url": url}, status=200, ok=True)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "request", fake_request)


def test_idle_without_config():
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({}, reg))
    assert reg.list_skills() == []


def test_registers_operations_as_skills(monkeypatch):
    _install_requests(monkeypatch)
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({"spec_url": "https://api.example.com/openapi.json"}, reg))
    assert set(reg.list_skills()) == {
        "openapi::default::get_item", "openapi::default::create_item",
    }


def test_get_operation_substitutes_path_and_sends_query(monkeypatch):
    calls: list = []
    _install_requests(monkeypatch, recorder=calls)
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({"spec_url": "https://api.example.com/openapi.json"}, reg))

    skill = reg.get_skill("openapi::default::get_item")
    result = asyncio.run(skill.execute({"item_id": 42, "verbose": True}))

    assert result["ok"] is True
    call = calls[-1]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.example.com/items/42"   # path param substituted
    assert call["params"] == {"verbose": True}                  # leftover → query
    assert call["json"] is None


def test_post_operation_sends_body_with_auth(monkeypatch):
    calls: list = []
    _install_requests(monkeypatch, recorder=calls)
    monkeypatch.setenv("MY_API_TOKEN", "sekret")
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize(
        {"spec_url": "https://api.example.com/openapi.json", "auth_env": "MY_API_TOKEN"}, reg,
    ))
    skill = reg.get_skill("openapi::default::create_item")
    asyncio.run(skill.execute({"name": "widget"}))

    call = calls[-1]
    assert call["method"] == "POST"
    assert call["json"] == {"name": "widget"}          # write method → body
    assert call["headers"]["Authorization"] == "Bearer sekret"


def test_private_host_spec_is_rejected(monkeypatch):
    # A spec URL on a loopback host must be refused (SSRF guard), so no
    # skills register.
    _install_requests(monkeypatch)
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({"spec_url": "http://127.0.0.1/openapi.json"}, reg))
    assert reg.list_skills() == []


def test_allow_private_hosts_opt_in(monkeypatch):
    _install_requests(monkeypatch)
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({
        "specs": [{"name": "local", "url": "http://127.0.0.1/openapi.json",
                   "allow_private_hosts": True}],
    }, reg))
    assert set(reg.list_skills()) == {
        "openapi::local::get_item", "openapi::local::create_item",
    }


def test_multi_spec_namespacing(monkeypatch):
    _install_requests(monkeypatch)
    ext = _Ext()
    reg = ComponentRegistry()
    asyncio.run(ext.initialize({
        "specs": [
            {"name": "a", "url": "https://a.example.com/openapi.json"},
            {"name": "b", "url": "https://b.example.com/openapi.json"},
        ],
    }, reg))
    names = set(reg.list_skills())
    assert "openapi::a::get_item" in names
    assert "openapi::b::get_item" in names
