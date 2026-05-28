"""Phase 0 baseline: ``arc/services.py`` service manager.

Tests the PID-file daemon manager without actually spawning daemons.
All filesystem writes go to ``tmp_path`` via monkeypatched ``Path.home``.
"""

import pytest

from arc import services as svc


pytestmark = pytest.mark.chat


def test_sim2l_available_truthy_when_sim2l_importable():
    # sim2l is installed in this repo, so this should be True.
    assert svc.sim2l_available() is True


def test_service_url_uses_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("SIM2L_CATALOG_URL", raising=False)
    assert svc.service_url("catalog") == "http://localhost:8002"


def test_service_url_respects_env_override(monkeypatch):
    monkeypatch.setenv("SIM2L_CATALOG_URL", "http://example.invalid:9999/")
    # trailing slash stripped
    assert svc.service_url("catalog") == "http://example.invalid:9999"


def test_service_env_maps_admin_password_from_chat_credentials(monkeypatch):
    monkeypatch.setenv("SIM2L_USERNAME", "admin")
    monkeypatch.setenv("SIM2L_PASSWORD", "local-secret")
    monkeypatch.delenv("SIM2L_ADMIN_PASSWORD", raising=False)

    env = svc._service_env()

    assert env["SIM2L_ADMIN_PASSWORD"] == "local-secret"


def test_service_env_does_not_map_non_admin_password(monkeypatch):
    monkeypatch.setenv("SIM2L_USERNAME", "sim2l")
    monkeypatch.setenv("SIM2L_PASSWORD", "db-password")
    monkeypatch.delenv("SIM2L_ADMIN_PASSWORD", raising=False)

    env = svc._service_env()

    assert "SIM2L_ADMIN_PASSWORD" not in env


def test_service_env_preserves_explicit_admin_password(monkeypatch):
    monkeypatch.setenv("SIM2L_USERNAME", "admin")
    monkeypatch.setenv("SIM2L_PASSWORD", "chat-secret")
    monkeypatch.setenv("SIM2L_ADMIN_PASSWORD", "service-secret")

    env = svc._service_env()

    assert env["SIM2L_ADMIN_PASSWORD"] == "service-secret"


def test_service_env_sets_service_url_aliases(monkeypatch):
    monkeypatch.setenv("SIM2L_RESULTS_URL", "http://results.example")
    monkeypatch.delenv("SIM2L_RESULTS_SERVICE_URL", raising=False)

    env = svc._service_env()

    assert env["SIM2L_CACHE_SERVICE_URL"] == "http://localhost:8001"
    assert env["SIM2L_CATALOG_SERVICE_URL"] == "http://localhost:8002"
    assert env["SIM2L_RESULTS_SERVICE_URL"] == "http://results.example"


def test_status_all_returns_managed_services(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(svc, "_LOG_DIR", tmp_path / "logs")
    status = svc.status_all()
    assert set(status.keys()) == {"cache", "catalog", "results", "mcp"}
    # No PID files present → none running
    for name, info in status.items():
        assert info["running"] is False
        assert info["pid"] is None


def test_status_all_cleans_stale_pid_files(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(svc, "_LOG_DIR", tmp_path / "logs")
    (tmp_path / "pids").mkdir()
    # Stale PID — process 1 exists but doesn't have this name; we use a
    # very-unlikely PID that should not be alive.
    stale_pid = 999_999_999
    (tmp_path / "pids" / svc._SERVICES["catalog"]["pid_file"]).write_text(str(stale_pid))

    status = svc.status_all()
    # Status reports not-running and cleans up the file
    assert status["catalog"]["running"] is False
    assert not (tmp_path / "pids" / svc._SERVICES["catalog"]["pid_file"]).exists()


def test_stop_returns_failure_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_PID_DIR", tmp_path / "pids")
    success, msg = svc.stop("cache")
    assert success is False
    assert "not running" in msg.lower()


def test_start_refuses_when_sim2l_unavailable(monkeypatch):
    monkeypatch.setattr(svc, "sim2l_available", lambda: False)
    success, msg = svc.start("cache")
    assert success is False
    assert "not installed" in msg.lower()


def test_start_mcp_uses_optional_server_command(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(svc, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(svc, "sim2l_available", lambda: True)
    monkeypatch.setattr(svc.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("SIM2L_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("SIM2L_CATALOG_URL", "http://catalog.example")

    captured = {}

    class Proc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)

    success, msg = svc.start("mcp")

    assert success is True
    assert "mcp started" in msg
    assert captured["cmd"][:3] == [svc.sys.executable, "-m", "sim2l.mcp.server"]
    assert "--transport" in captured["cmd"]
    assert "streamable-http" in captured["cmd"]
    assert "--catalog-url" in captured["cmd"]
    assert "--db-path" not in captured["cmd"]


def test_pid_alive_returns_false_for_unlikely_pid():
    # Pick a PID very unlikely to exist (above typical max)
    assert svc._pid_alive(999_999_999) is False


def test_services_registry_has_safe_ports():
    """No service registered to a privileged port (<1024).
    Belt-and-braces against accidentally requiring root.
    """
    for name, cfg in svc._SERVICES.items():
        assert cfg["port"] >= 1024, f"{name} port {cfg['port']} is privileged"
