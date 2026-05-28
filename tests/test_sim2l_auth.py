"""Tests for the sim2l-auth + push-failure-visibility flow.

Three concerns covered here:

  1. ``arc.chat.sim2l_auth.login_to_services`` correctly attempts
     login on each service URL and reports per-service outcomes.
  2. ``arc.runtime.sim2l_adapter`` accepts catalog/results session ids
     and forwards them via the X-Session-ID header.
  3. The chat-loop's ``_authenticate_and_prompt`` surfaces failures
     and lets the user choose continue / abort.
"""

from types import SimpleNamespace

import pytest

import arc.chat.sim2l_auth as auth_mod
from arc.chat.sim2l_auth import (
    ServiceAuth,
    login_to_services,
    resolve_credentials,
)


pytestmark = pytest.mark.chat


# ── ServiceAuth dataclass ────────────────────────────────────────────────


def test_service_auth_default_is_unauthenticated():
    a = ServiceAuth()
    assert a.authenticated is False
    assert a.partial is False


def test_service_auth_requires_catalog_and_results_to_be_authenticated():
    """Cache session alone is not enough — the push targets are catalog
    and results."""
    a = ServiceAuth(catalog_session="x", cache_session="y")
    assert a.authenticated is False
    assert a.partial is True


def test_service_auth_authenticated_when_both_push_targets_set():
    a = ServiceAuth(catalog_session="x", results_session="y")
    assert a.authenticated is True
    assert a.partial is False


def test_service_auth_partial_when_some_succeed():
    a = ServiceAuth(catalog_session="x")
    assert a.partial is True
    assert a.authenticated is False


# ── resolve_credentials ─────────────────────────────────────────────────


def test_resolve_credentials_prefers_explicit_args(monkeypatch):
    monkeypatch.setenv("SIM2L_USERNAME", "from-env")
    monkeypatch.setenv("SIM2L_PASSWORD", "env-pass")
    u, p = resolve_credentials("explicit-user", "explicit-pass")
    assert u == "explicit-user"
    assert p == "explicit-pass"


def test_resolve_credentials_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SIM2L_USERNAME", "env-u")
    monkeypatch.setenv("SIM2L_PASSWORD", "env-p")
    u, p = resolve_credentials()
    assert u == "env-u"
    assert p == "env-p"


def test_resolve_credentials_documented_default(monkeypatch):
    monkeypatch.delenv("SIM2L_USERNAME", raising=False)
    monkeypatch.delenv("SIM2L_PASSWORD", raising=False)
    u, p = resolve_credentials()
    assert u == "admin"
    assert p == "admin"


# ── login_to_services ────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _make_fake_post(responses_by_url: dict):
    """responses_by_url is ``base_url → _FakeResponse | exception``."""
    def fake_post(url, *, json=None, timeout=None):
        for base, resp in responses_by_url.items():
            if url.startswith(base):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected url: {url}")
    return fake_post


def test_login_all_services_succeed(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", _make_fake_post({
        "http://localhost:8001": _FakeResponse(200, {"session_id": "cache-sid"}),
        "http://localhost:8002": _FakeResponse(200, {"session_id": "cat-sid"}),
        "http://localhost:8003": _FakeResponse(200, {"session_id": "res-sid"}),
    }))
    auth = login_to_services("u", "p")
    assert auth.authenticated is True
    assert auth.catalog_session == "cat-sid"
    assert auth.results_session == "res-sid"
    assert auth.cache_session == "cache-sid"
    assert auth.errors == []


def test_login_catalog_401_surfaces_error(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", _make_fake_post({
        "http://localhost:8001": _FakeResponse(200, {"session_id": "cache-sid"}),
        "http://localhost:8002": _FakeResponse(401),
        "http://localhost:8003": _FakeResponse(200, {"session_id": "res-sid"}),
    }))
    auth = login_to_services("u", "wrong-pass")
    assert auth.authenticated is False  # catalog missing → not authenticated
    assert auth.partial is True
    assert any("catalog" in e and "401" in e for e in auth.errors)


def test_login_connection_refused_records_error(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", _make_fake_post({
        "http://localhost:8001": requests.exceptions.ConnectionError("refused"),
        "http://localhost:8002": requests.exceptions.ConnectionError("refused"),
        "http://localhost:8003": requests.exceptions.ConnectionError("refused"),
    }))
    auth = login_to_services("u", "p")
    assert auth.authenticated is False
    assert auth.partial is False
    assert len(auth.errors) == 3
    assert all("not reachable" in e for e in auth.errors)


def test_login_429_distinguished_from_401(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", _make_fake_post({
        "http://localhost:8001": _FakeResponse(429),
        "http://localhost:8002": _FakeResponse(429),
        "http://localhost:8003": _FakeResponse(429),
    }))
    auth = login_to_services("u", "p")
    assert any("rate-limited" in e for e in auth.errors)


def test_login_missing_session_id_in_response_treated_as_failure(monkeypatch):
    """200 with no ``session_id`` field is a malformed response."""
    import requests
    monkeypatch.setattr(requests, "post", _make_fake_post({
        "http://localhost:8001": _FakeResponse(200, {}),
        "http://localhost:8002": _FakeResponse(200, {}),
        "http://localhost:8003": _FakeResponse(200, {}),
    }))
    auth = login_to_services("u", "p")
    assert auth.authenticated is False
    assert all("session_id" in e for e in auth.errors)


def test_login_respects_url_overrides(monkeypatch):
    import requests
    captured_urls = []
    def fake_post(url, **kw):
        captured_urls.append(url)
        return _FakeResponse(200, {"session_id": "x"})
    monkeypatch.setattr(requests, "post", fake_post)
    login_to_services(
        "u", "p",
        catalog_url="http://override-catalog:9999",
        results_url="http://override-results:8888",
        cache_url="http://override-cache:7777",
    )
    assert any("override-catalog:9999" in u for u in captured_urls)
    assert any("override-results:8888" in u for u in captured_urls)
    assert any("override-cache:7777" in u for u in captured_urls)


def test_login_uses_env_url_overrides(monkeypatch):
    import requests
    monkeypatch.setenv("SIM2L_CATALOG_URL", "http://env-cat:1234")
    captured = []
    def fake_post(url, **kw):
        captured.append(url)
        return _FakeResponse(200, {"session_id": "x"})
    monkeypatch.setattr(requests, "post", fake_post)
    login_to_services("u", "p")
    assert any("env-cat:1234" in u for u in captured)


# ── sim2l_adapter passes session ids on the header ───────────────────────


def test_results_push_includes_session_header(monkeypatch):
    """The /register_direct POST must carry X-Session-ID when one is set."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    captured = {}

    class _RespOk:
        def raise_for_status(self): pass

    import requests
    def fake_post(url, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _RespOk()
    monkeypatch.setattr(requests, "post", fake_post)

    adapter = Sim2LRuntimeAdapter(
        db_path="/tmp/x.db",
        session_id="legacy-sid",
        results_session_id="results-sid-from-login",
    )
    sim2l_result = SimpleNamespace(
        execution_id="exec-1",
        squid_id="sq",
        status="completed",
        duration_seconds=0.5,
    )
    ok = adapter._push_to_results(
        sim2l_result, "sim_name", "0.1.0",
        {"a": 1}, {"out": 2.0},
    )
    assert ok is True
    assert captured["headers"].get("X-Session-ID") == "results-sid-from-login"


def test_results_push_falls_back_to_legacy_session_when_no_login(monkeypatch):
    """When results_session_id is None, the legacy session_id is used."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    captured = {}
    class _Ok:
        def raise_for_status(self): pass
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: (
        captured.update(kw) or _Ok()
    ))

    adapter = Sim2LRuntimeAdapter(
        db_path="/tmp/x.db",
        session_id="legacy-fallback",
    )
    sim2l_result = SimpleNamespace(
        execution_id="exec-1", squid_id="sq",
        status="completed", duration_seconds=0.5,
    )
    adapter._push_to_results(sim2l_result, "n", "0.1", {"a": 1}, {"b": 2})
    assert captured["headers"].get("X-Session-ID") == "legacy-fallback"


def test_catalog_push_includes_workflow_bundle(monkeypatch, tmp_path):
    """ARC catalog registration must include executable workflow files."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    from sim2l.schema import InputSchema, OutputSchema
    import sim2l

    captured = {}

    class FakeCatalogClient:
        def __init__(self, *args, **kwargs):
            pass

        def register_simulation(self, **kwargs):
            captured.update(kwargs)
            return True

    monkeypatch.setattr(
        "sim2l.database.catalog_client.CatalogClient",
        FakeCatalogClient,
    )

    def simulate(x=1.0):
        return {"y": x + 1}

    sim_def = sim2l.SimulationDefinition.from_function(
        func=simulate,
        name="tool",
        version="0.1.0",
        inputs=InputSchema.from_dict({"x": {"type": "Number", "default": 1.0}}),
        outputs=OutputSchema.from_dict({"y": {"type": "Number"}}),
    )
    adapter = Sim2LRuntimeAdapter(db_path=str(tmp_path / "simulations.db"))
    source = "def simulate(x=1.0):\n    return {'y': x + 1}\n"

    assert adapter._push_to_catalog(sim_def, "tool", "0.1.0", source) is True
    bundle = captured["workflow_bundle"]
    assert bundle["workflow_type"] == "function"
    assert bundle["entrypoint"] == "workflow.py"
    assert bundle["files"][0]["path"] == "workflow.py"
    assert bundle["files"][0]["content_base64"]


def test_results_push_records_failure_in_last_push_errors(monkeypatch):
    """A 401 push doesn't raise — it gets recorded for the chat to surface."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import requests

    class _Resp401:
        status_code = 401
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("401 Unauthorized", response=self)
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp401())

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    sim2l_result = SimpleNamespace(
        execution_id="exec-1", squid_id="sq",
        status="completed", duration_seconds=0.1,
    )
    ok = adapter._push_to_results(sim2l_result, "n", "0.1", {}, {})
    assert ok is False
    # The failure is recorded so the chat layer can surface it
    assert any("results" in label for label, _ in adapter.last_push_errors)


def test_push_classifier_logs_401_as_warning(caplog, monkeypatch):
    """401 → logger.warning, not logger.debug."""
    import requests
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    class _Resp:
        status_code = 401
    err = requests.exceptions.HTTPError("401", response=_Resp())

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    with caplog.at_level("WARNING", logger="arc.runtime.sim2l_adapter"):
        adapter._classify_push_error(err, "catalog")
    assert any("401" in r.message for r in caplog.records)


def test_push_classifier_logs_connection_refused_as_debug(caplog, monkeypatch):
    """Connection-refused → debug (the chat already warned at startup)."""
    import requests
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    err = requests.exceptions.ConnectionError("nope")
    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    with caplog.at_level("WARNING", logger="arc.runtime.sim2l_adapter"):
        adapter._classify_push_error(err, "catalog")
    # No warning was emitted for the connection-refused case
    assert not any(r.levelname == "WARNING" for r in caplog.records)


# ── _authenticate_and_prompt flow ───────────────────────────────────────


@pytest.mark.asyncio
async def test_authenticate_returns_true_on_full_success(monkeypatch, capsys):
    from arc.chat.loop import _authenticate_and_prompt

    fake_auth = ServiceAuth(
        username="admin",
        catalog_session="cat-sid",
        results_session="res-sid",
    )
    monkeypatch.setattr(auth_mod, "login_to_services", lambda: fake_auth)
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: fake_auth,
    )

    workflow = SimpleNamespace(adapter=SimpleNamespace(
        _catalog_session_id=None,
        _results_session_id=None,
    ))
    result = await _authenticate_and_prompt(workflow)
    assert result is True
    # Session ids attached to the adapter
    assert workflow.adapter._catalog_session_id == "cat-sid"
    assert workflow.adapter._results_session_id == "res-sid"
    # And the user got a sign-in confirmation
    assert "Signed in" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_authenticate_prompts_continue_on_failure(monkeypatch, capsys):
    """When login fails, the user is asked whether to continue."""
    from arc.chat.loop import _authenticate_and_prompt

    fake_auth = ServiceAuth(
        errors=["catalog rejected credentials (401)",
                "results rejected credentials (401)"],
    )
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: fake_auth,
    )
    # Simulate user typing "y" at the prompt
    async def _fake_input(prompt):
        return "y"
    monkeypatch.setattr("arc.chat.loop.chat_input_async", _fake_input)

    workflow = SimpleNamespace(adapter=SimpleNamespace(
        _catalog_session_id=None,
        _results_session_id=None,
    ))
    result = await _authenticate_and_prompt(workflow)
    assert result is True
    out = capsys.readouterr().out
    assert "Could not sign in" in out
    assert "401" in out  # the errors are echoed


@pytest.mark.asyncio
async def test_authenticate_returns_false_when_user_declines(monkeypatch):
    """User types 'n' → chat_loop bails out cleanly."""
    from arc.chat.loop import _authenticate_and_prompt

    fake_auth = ServiceAuth(errors=["catalog rejected credentials (401)"])
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: fake_auth,
    )
    async def _fake_input(prompt):
        return "n"
    monkeypatch.setattr("arc.chat.loop.chat_input_async", _fake_input)

    workflow = SimpleNamespace(adapter=SimpleNamespace(
        _catalog_session_id=None,
        _results_session_id=None,
    ))
    result = await _authenticate_and_prompt(workflow)
    assert result is False


@pytest.mark.asyncio
async def test_authenticate_returns_false_on_eof(monkeypatch):
    """Ctrl-D at the prompt is the same as 'no'."""
    from arc.chat.loop import _authenticate_and_prompt

    fake_auth = ServiceAuth(errors=["catalog 401"])
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: fake_auth,
    )
    async def _fake_input(prompt):
        raise EOFError
    monkeypatch.setattr("arc.chat.loop.chat_input_async", _fake_input)

    workflow = SimpleNamespace(adapter=SimpleNamespace(
        _catalog_session_id=None,
        _results_session_id=None,
    ))
    assert await _authenticate_and_prompt(workflow) is False


@pytest.mark.asyncio
async def test_authenticate_handles_adapter_without_session_id_attrs(monkeypatch):
    """Local adapters don't have the *_session_id attrs — must not crash."""
    from arc.chat.loop import _authenticate_and_prompt

    fake_auth = ServiceAuth(
        username="admin",
        catalog_session="cat", results_session="res",
    )
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: fake_auth,
    )

    class _LocalAdapter:
        __slots__ = ()  # No attrs allowed
    workflow = SimpleNamespace(adapter=_LocalAdapter())
    result = await _authenticate_and_prompt(workflow)
    assert result is True
