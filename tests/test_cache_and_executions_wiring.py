"""Cache-service + catalog-execution-registry wiring.

Two issues this test suite pins:

  1. ``Sim2LRuntimeAdapter._get_repo`` must call ``sim2l.configure``
     with the cache / catalog / results service URLs + session ids so
     sim2l's executor reaches the cache service. Without this the
     cache stays empty even when the chat hits identical inputs twice.

  2. After every successful run, the adapter must also POST to the
     catalog's ``/executions`` endpoint. The dashboard's "Total
     Executions" counter reads from ``execution_registry`` in the
     catalog DB — if the chat never tells the catalog about the run,
     the counter stays at 0 even when ``execution_results`` has rows.
"""

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.chat


# ── _get_repo passes cache config to sim2l.configure ─────────────────────


def _sim2l_accepts(key: str) -> bool:
    """Mirror the adapter's config-attribute probe."""
    import sim2l
    try:
        cfg = sim2l.config.get_config()
    except Exception:
        return False
    return hasattr(cfg, key)


def test_get_repo_forwards_cache_url_to_sim2l_configure(monkeypatch):
    """The adapter must wire SIM2L_CACHE_URL into sim2l.configure so
    the executor can route lookups through the cache service."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import sim2l

    monkeypatch.setenv("SIM2L_CACHE_URL", "http://localhost:8001")

    captured = {}
    def fake_configure(**kwargs):
        captured.update(kwargs)

    # sim2l.configure must accept cache_service_url for this test to
    # be meaningful — assert that first.
    if not _sim2l_accepts("cache_service_url"):
        pytest.skip("installed sim2l doesn't expose cache_service_url")

    monkeypatch.setattr(sim2l, "configure", fake_configure)
    monkeypatch.setattr(
        "arc.runtime.sim2l_adapter.Sim2LRuntimeAdapter._check_sim2l",
        lambda self: True,
    )

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    # Stub the repository import — we only care that configure was called.
    import sys
    sys.modules.setdefault("sim2l.repository", SimpleNamespace(
        SimulationRepository=lambda: None,
    ))

    # Trigger _get_repo
    try:
        adapter._get_repo()
    except Exception:
        pass  # SimulationRepository may need other state; configure was called

    assert captured.get("cache_service_url") == "http://localhost:8001"


def test_get_repo_forwards_cache_session_id(monkeypatch):
    """When the chat has authenticated, the cache session id flows to sim2l."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import sim2l

    if not _sim2l_accepts("cache_session_id"):
        pytest.skip("installed sim2l doesn't expose cache_session_id")

    captured = {}
    monkeypatch.setattr(sim2l, "configure", lambda **kw: captured.update(kw))
    monkeypatch.setattr(
        "arc.runtime.sim2l_adapter.Sim2LRuntimeAdapter._check_sim2l",
        lambda self: True,
    )

    adapter = Sim2LRuntimeAdapter(
        db_path="/tmp/x.db",
        cache_session_id="cache-sid-from-login",
    )
    try:
        adapter._get_repo()
    except Exception:
        pass
    assert captured.get("cache_session_id") == "cache-sid-from-login"


def test_get_repo_skips_unaccepted_kwargs_gracefully(monkeypatch):
    """If sim2l's config object doesn't expose cache_service_url (older
    build), the adapter must NOT pass it — otherwise sim2l raises and
    the adapter is unusable. The adapter probes the config object's
    attributes to decide what's safe to forward."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import sim2l

    # Stub a minimal config object exposing ONLY db_path. The adapter's
    # _accepted_configure_keys() walks the real config — replace it with
    # a tiny stand-in that mimics an older build.
    class _OldConfig:
        db_path = None  # the only attribute an old sim2l had
    monkeypatch.setattr(sim2l.config, "get_config", lambda: _OldConfig())

    captured = {}
    def restricted_configure(**kwargs):
        # Mimic real sim2l.configure: reject unknown keys.
        for k in kwargs:
            if not hasattr(_OldConfig, k):
                raise ValueError(f"Unknown configuration option: {k}")
        captured.update(kwargs)
    monkeypatch.setattr(sim2l, "configure", restricted_configure)
    monkeypatch.setattr(
        "arc.runtime.sim2l_adapter.Sim2LRuntimeAdapter._check_sim2l",
        lambda self: True,
    )

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db",
                                   cache_session_id="cs", catalog_session_id="cat")
    # Should NOT raise — the adapter probes the config attrs.
    try:
        adapter._get_repo()
    except (ValueError, TypeError) as exc:
        pytest.fail(f"adapter passed unaccepted kwarg: {exc}")
    except Exception:
        # SimulationRepository may complain about other things — outside scope.
        pass
    # And only db_path was forwarded.
    assert set(captured.keys()) <= {"db_path"}


# ── _push_to_catalog_execution_registry ──────────────────────────────────


def _stub_sim2l_result(execution_id="exec-1"):
    return SimpleNamespace(
        execution_id=execution_id,
        squid_id="sq-1",
        status="completed",
        duration_seconds=0.5,
        executor_type="isolated",
    )


def test_push_to_catalog_execution_registry_happy_path(monkeypatch):
    """When the catalog has the simulation, /executions receives the row."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import requests

    posts = []

    class _LookupResp:
        status_code = 200
        def json(self):
            return {"id": 42, "name": "n", "version": "0.1.0"}
        def raise_for_status(self):
            pass

    class _PostResp:
        status_code = 200
        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return _LookupResp()

    def fake_post(url, *, json=None, headers=None, timeout=None):
        posts.append({"url": url, "json": json, "headers": dict(headers or {})})
        return _PostResp()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)

    adapter = Sim2LRuntimeAdapter(
        db_path="/tmp/x.db",
        catalog_session_id="cat-sid",
    )
    ok = adapter._push_to_catalog_execution_registry(
        _stub_sim2l_result(),
        "n", "0.1.0",
        {"thickness": 5.0},
        {"bandgap_ev": 1.1},
    )
    assert ok is True
    assert len(posts) == 1
    post = posts[0]
    assert post["url"].endswith("/executions")
    assert post["headers"].get("X-Session-ID") == "cat-sid"
    body = post["json"]
    assert body["simulation_id"] == 42
    assert body["execution_id"] == "exec-1"
    assert body["status"] == "completed"
    assert body["output_count"] == 1


def test_push_to_catalog_execution_silently_skips_on_404(monkeypatch):
    """When the catalog doesn't know the simulation, the registry push
    is a no-op and the run continues."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import requests

    class _NotFound:
        status_code = 404
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404", response=self)
        def json(self):
            return {}

    monkeypatch.setattr(requests, "get", lambda url, **kw: _NotFound())
    # POST should never be called — assert by leaving the mock missing.
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail(
        "POST /executions should not run when the GET returned 404"
    ))

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    ok = adapter._push_to_catalog_execution_registry(
        _stub_sim2l_result(), "missing-sim", "0.1.0", {}, {},
    )
    assert ok is False
    # No errors accumulated either — this is a "best effort skip", not a failure.
    assert all("404" not in m for _, m in adapter.last_push_errors)


def test_push_to_catalog_execution_records_401(monkeypatch):
    """Lookup returns 200, but POST /executions returns 401 → recorded."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import requests

    class _LookupOk:
        status_code = 200
        def json(self):
            return {"id": 1}
        def raise_for_status(self):
            pass

    class _Post401:
        status_code = 401
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("401", response=self)

    monkeypatch.setattr(requests, "get", lambda url, **kw: _LookupOk())
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Post401())

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    ok = adapter._push_to_catalog_execution_registry(
        _stub_sim2l_result(), "n", "0.1.0", {}, {},
    )
    assert ok is False
    assert any("catalog/executions" in label for label, _ in adapter.last_push_errors)


def test_push_to_catalog_execution_computes_input_hash(monkeypatch):
    """Identical inputs produce identical hashes (stability check)."""
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter
    import requests

    posts = []

    class _Ok:
        status_code = 200
        def json(self):
            return {"id": 1}
        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda url, **kw: _Ok())
    monkeypatch.setattr(
        requests, "post",
        lambda url, *, json=None, headers=None, timeout=None: posts.append(json) or _Ok(),
    )

    adapter = Sim2LRuntimeAdapter(db_path="/tmp/x.db")
    inputs_a = {"thickness": 5.0, "temperature": 300.0}
    inputs_b = {"temperature": 300.0, "thickness": 5.0}  # different order, same content
    adapter._push_to_catalog_execution_registry(
        _stub_sim2l_result("e1"), "n", "0.1", inputs_a, {},
    )
    adapter._push_to_catalog_execution_registry(
        _stub_sim2l_result("e2"), "n", "0.1", inputs_b, {},
    )
    # Hash is content-determined; key order shouldn't change it.
    assert posts[0]["input_hash"] == posts[1]["input_hash"]


# ── Cache session id flows from auth → adapter ───────────────────────────


def test_authenticate_attaches_cache_session_to_adapter(monkeypatch):
    """The chat's _authenticate_and_prompt must set _cache_session_id on
    the adapter when the cache login succeeded."""
    import asyncio
    from arc.chat.loop import _authenticate_and_prompt
    from arc.chat.sim2l_auth import ServiceAuth

    fake_auth = ServiceAuth(
        username="admin",
        catalog_session="cat-sid",
        results_session="res-sid",
        cache_session="cache-sid",
    )
    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services", lambda: fake_auth,
    )

    workflow = SimpleNamespace(adapter=SimpleNamespace(
        _catalog_session_id=None,
        _results_session_id=None,
        _cache_session_id=None,
    ))
    asyncio.run(_authenticate_and_prompt(workflow))
    assert workflow.adapter._cache_session_id == "cache-sid"


def test_authenticate_handles_adapter_without_cache_session_id_attr(monkeypatch):
    """Older adapter classes (or LocalRuntimeAdapter) don't have the new
    ``_cache_session_id`` slot — must not crash."""
    import asyncio
    from arc.chat.loop import _authenticate_and_prompt
    from arc.chat.sim2l_auth import ServiceAuth

    monkeypatch.setattr(
        "arc.chat.sim2l_auth.login_to_services",
        lambda: ServiceAuth(
            username="admin",
            catalog_session="c", results_session="r", cache_session="cs",
        ),
    )

    class _SlottedAdapter:
        __slots__ = ()
    workflow = SimpleNamespace(adapter=_SlottedAdapter())
    # No AttributeError raised
    result = asyncio.run(_authenticate_and_prompt(workflow))
    assert result is True
