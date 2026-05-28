"""``arc.chat.io_utils`` — banner renderer + health-probe tests.

The ``install_sigint_handler`` function intentionally has no test
(signal handlers are unsafe to exercise in pytest), but ``print_banner``
and ``check_sim2l_services`` have observable behaviour we can pin.
"""

import pytest

from arc.chat.io_utils import (
    _SIM2L_SERVICES,
    check_sim2l_services,
    print_banner,
)


pytestmark = pytest.mark.chat


# ── _SIM2L_SERVICES registry ─────────────────────────────────────────────


def test_sim2l_services_registry_has_three_known_services():
    assert set(_SIM2L_SERVICES.keys()) == {"cache", "catalog", "results"}


def test_sim2l_services_registry_default_ports():
    """Per-service default URLs use the documented localhost ports."""
    assert _SIM2L_SERVICES["cache"][1].endswith(":8001")
    assert _SIM2L_SERVICES["catalog"][1].endswith(":8002")
    assert _SIM2L_SERVICES["results"][1].endswith(":8003")


# ── check_sim2l_services ─────────────────────────────────────────────────


def test_check_sim2l_services_all_unreachable(monkeypatch):
    """No services running → every probe times out → all False."""
    import requests
    def raise_timeout(url, **kwargs):
        raise requests.exceptions.ConnectTimeout("no route")
    monkeypatch.setattr(requests, "get", raise_timeout)
    status = check_sim2l_services()
    assert status == {"cache": False, "catalog": False, "results": False}


def test_check_sim2l_services_all_running(monkeypatch):
    """All services 200 OK → every value is True."""
    import requests
    class _Resp:
        status_code = 200
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp())
    status = check_sim2l_services()
    assert all(status.values())


def test_check_sim2l_services_mixed(monkeypatch):
    """One service down, two up → exactly one False in the dict."""
    import requests
    class _OK:
        status_code = 200
    class _Bad:
        status_code = 503

    def fake_get(url, **kwargs):
        # Catalog returns 503; others 200
        if ":8002" in url:
            return _Bad()
        return _OK()
    monkeypatch.setattr(requests, "get", fake_get)
    status = check_sim2l_services()
    assert status["catalog"] is False
    assert status["cache"] is True
    assert status["results"] is True


def test_check_sim2l_services_uses_env_override(monkeypatch):
    """SIM2L_CATALOG_URL env var redirects the probe."""
    import requests
    captured_urls = []
    class _Resp:
        status_code = 200
    def fake_get(url, **kwargs):
        captured_urls.append(url)
        return _Resp()
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("SIM2L_CATALOG_URL", "http://override.example:9999")
    check_sim2l_services()
    # The catalog probe used the override
    catalog_url = next(u for u in captured_urls if "catalog" in u or "9999" in u)
    assert "9999" in catalog_url


def test_check_sim2l_services_one_second_timeout(monkeypatch):
    """Each request gets a 1s timeout. We assert via the call args."""
    import requests
    captured = []
    class _Resp:
        status_code = 404
    def fake_get(url, **kwargs):
        captured.append(kwargs.get("timeout"))
        return _Resp()
    monkeypatch.setattr(requests, "get", fake_get)
    check_sim2l_services()
    assert all(t == 1 for t in captured)


# ── print_banner ─────────────────────────────────────────────────────────


def test_banner_renders_with_provider_and_model(capsys):
    print_banner(
        provider="openwebui", model="gpt-oss:120b",
        base_url="https://example.invalid/api",
        session_id="my-session",
    )
    out = capsys.readouterr().out
    assert "openwebui" in out
    assert "gpt-oss:120b" in out
    assert "my-session" in out
    assert "https://example.invalid/api" in out


def test_banner_renders_with_stub_provider(capsys):
    """provider=None → 'stub (no LLM)' fallback string."""
    print_banner(provider=None, model=None, base_url=None, session_id="sess")
    out = capsys.readouterr().out
    assert "stub" in out
    assert "auto" in out
    assert "n/a" in out


def test_banner_shows_sim2l_status_line(capsys):
    print_banner(
        provider="openwebui", model="x", base_url="u", session_id="s",
        sim2l_status={"cache": True, "catalog": False, "results": True},
    )
    out = capsys.readouterr().out
    # All three names appear; the failing one is the catalog
    assert "cache" in out
    assert "catalog" in out
    assert "results" in out


def test_banner_with_no_status_shows_not_checked(capsys):
    """When sim2l_status=None, a "(not checked)" label appears."""
    print_banner(
        provider="openwebui", model="x", base_url="u",
        session_id="s",  # sim2l_status defaults to None
    )
    out = capsys.readouterr().out
    assert "not checked" in out


def test_banner_includes_coder_backend(capsys):
    print_banner(
        provider="openwebui", model="x", base_url="u", session_id="s",
        coder_backend="arc-codex:coder",
    )
    out = capsys.readouterr().out
    assert "arc-codex:coder" in out
