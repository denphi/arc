"""Network-facing defaults: config secrecy, Host guard, bind guard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arc.api import security
from arc.api.security import InsecureBindError, check_bind_allowed, is_loopback_host
from arc.ui import server as ui_server


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("ARC_API_TOKEN", raising=False)
    monkeypatch.delenv("ARC_ALLOW_UNAUTHENTICATED_BIND", raising=False)
    security.load_security_config.cache_clear()
    yield
    security.load_security_config.cache_clear()


def _ui_client(monkeypatch, tmp_path, env_text=""):
    env_path = tmp_path / ".env"
    env_path.write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("ARC_UI_ENV_PATH", str(env_path))
    return TestClient(ui_server.create_app()), env_path


def test_config_get_never_returns_secret_values(monkeypatch, tmp_path):
    client, _ = _ui_client(
        monkeypatch, tmp_path,
        "OPENWEBUI_KEY=sk-secret-123\nSIM2L_PASSWORD=hunter2\nPLAIN=visible\n",
    )
    body = client.get("/api/config").json()
    assert body["values"]["OPENWEBUI_KEY"] == ui_server.SECRET_MASK
    assert body["extra"]["SIM2L_PASSWORD"] == ui_server.SECRET_MASK
    assert body["extra"]["PLAIN"] == "visible"
    assert "sk-secret-123" not in str(body) and "hunter2" not in str(body)


def test_config_put_keeps_secret_when_mask_is_sent_back(monkeypatch, tmp_path):
    client, env_path = _ui_client(monkeypatch, tmp_path, "OPENWEBUI_KEY=sk-secret-123\n")
    try:
        resp = client.put("/api/config", json={"values": {
            "OPENWEBUI_KEY": ui_server.SECRET_MASK, "ARC_MODEL": "m1",
        }})
        assert resp.status_code == 200
        text = env_path.read_text()
        assert "OPENWEBUI_KEY=sk-secret-123" in text
        assert "ARC_MODEL=m1" in text
    finally:
        monkeypatch.delenv("ARC_MODEL", raising=False)


@pytest.mark.parametrize("key", ["PYTHONPATH", "LD_PRELOAD", "ARC_CODEX_COMMAND"])
def test_config_put_rejects_keys_outside_the_ui_settings(monkeypatch, tmp_path, key):
    client, env_path = _ui_client(monkeypatch, tmp_path)
    resp = client.put("/api/config", json={"values": {key: "/tmp/evil"}})
    assert resp.status_code == 400
    assert key not in env_path.read_text()


def test_unauthenticated_server_rejects_foreign_host_header(monkeypatch, tmp_path):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1."""
    client, _ = _ui_client(monkeypatch, tmp_path)
    assert client.get("/api/sessions", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/api/sessions", headers={"host": "127.0.0.1:8888"}).status_code == 200
    assert client.get("/api/sessions", headers={"host": "localhost"}).status_code == 200


def test_host_guard_steps_aside_when_a_token_is_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ARC_API_TOKEN", "t0ken")
    security.load_security_config.cache_clear()
    client, _ = _ui_client(monkeypatch, tmp_path)
    resp = client.get("/api/sessions", headers={
        "host": "arc.internal.example", "authorization": "Bearer t0ken",
    })
    assert resp.status_code == 200
    assert client.get("/api/sessions", headers={"authorization": "Bearer nope"}).status_code == 401


def test_core_api_has_the_same_host_guard():
    from arc.api.server import create_app

    client = TestClient(create_app())
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/health").status_code == 200


def test_bind_guard(monkeypatch):
    check_bind_allowed("127.0.0.1")
    check_bind_allowed("::1")
    with pytest.raises(InsecureBindError):
        check_bind_allowed("0.0.0.0")
    monkeypatch.setenv("ARC_API_TOKEN", "t0ken")
    security.load_security_config.cache_clear()
    check_bind_allowed("0.0.0.0")


def test_bind_guard_escape_hatch(monkeypatch):
    monkeypatch.setenv("ARC_ALLOW_UNAUTHENTICATED_BIND", "1")
    check_bind_allowed("0.0.0.0")


def test_loopback_detection():
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.5")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")


def test_token_comparison_is_constant_time_helper():
    assert security.token_matches("abc", "abc")
    assert not security.token_matches("abd", "abc")
    assert not security.token_matches(None, "abc")
