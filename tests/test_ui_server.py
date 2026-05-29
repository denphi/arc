"""Smoke tests for the standalone ARC browser UI."""

import ast
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from arc.session import save_session_meta, session_paths
from arc.ui import server as ui_server


def test_ui_health_and_static_index_load():
    client = TestClient(ui_server.create_app())

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ui"] == "arc.ui"

    index = client.get("/")
    assert index.status_code == 200
    assert "ARC UI" in index.text
    assert "Autonomous Research Coder" in index.text
    assert 'id="configModal"' in index.text
    assert 'id="toolsPopover"' in index.text
    assert 'id="sidebarResizer"' in index.text
    assert 'id="inspectorResizer"' in index.text

    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "runResearch" in script.text
    assert "startResize" in script.text
    assert "saveConfig" in script.text
    assert "Raw JSON" in script.text
    assert "message-data-pills" in script.text


def test_ui_config_round_trip_preserves_env_file(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# keep this\nUNCHANGED=value\nARC_PROVIDER=stub\n", encoding="utf-8")
    monkeypatch.setenv("ARC_UI_ENV_PATH", str(env_path))
    monkeypatch.delenv("ARC_API_TOKEN", raising=False)
    _reload_security()
    client = TestClient(ui_server.create_app())

    initial = client.get("/api/config")
    assert initial.status_code == 200
    assert initial.json()["path"] == str(env_path)
    assert initial.json()["values"]["ARC_PROVIDER"] == "stub"
    assert initial.json()["extra"] == {"UNCHANGED": "value"}

    try:
        updated = client.put(
            "/api/config",
            json={
                "values": {
                    "ARC_PROVIDER": "openai",
                    "ARC_MODEL": "gpt-test",
                    "OPENWEBUI_URL": "https://genai.rcac.purdue.edu",
                }
            },
        )

        assert updated.status_code == 200
        assert updated.json()["values"]["ARC_PROVIDER"] == "openai"
        contents = env_path.read_text(encoding="utf-8")
        assert "# keep this" in contents
        assert "UNCHANGED=value" in contents
        assert "ARC_PROVIDER=openai" in contents
        assert "ARC_MODEL=gpt-test" in contents
    finally:
        for key in ("ARC_PROVIDER", "ARC_MODEL", "OPENWEBUI_URL"):
            monkeypatch.delenv(key, raising=False)
        _reload_security()


def test_ui_create_and_list_session():
    client = TestClient(ui_server.create_app())

    created = client.post("/api/sessions", json={"prefix": "ui", "goal": "test goal"})
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert any(s["session_id"] == session_id for s in listed.json()["sessions"])

    detail = client.get(f"/api/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["meta"]["goal"] == "test goal"
    assert "thread" in detail.json()


def test_ui_delete_session():
    client = TestClient(ui_server.create_app())

    created = client.post("/api/sessions", json={"prefix": "ui", "goal": "delete me"})
    session_id = created.json()["session_id"]

    deleted = client.delete(f"/api/sessions/{session_id}")

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not any(s["session_id"] == session_id for s in deleted.json()["sessions"])


def test_ui_commands_manifest_and_target_command():
    client = TestClient(ui_server.create_app())
    created = client.post("/api/sessions", json={"prefix": "ui", "goal": "target test"})
    session_id = created.json()["session_id"]

    manifest = client.get("/api/commands")
    assert manifest.status_code == 200
    assert any(cmd["name"] == "strategy" for cmd in manifest.json()["commands"])

    response = client.post(
        "/api/commands/run",
        json={"session_id": session_id, "command": "/target result=5"},
    )

    assert response.status_code == 200
    assert response.json()["payload"]["target"] == {"result": 5}
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["meta"]["target"] == {"result": 5}
    assert [msg["role"] for msg in detail["thread"][-2:]] == ["user", "assistant"]


def test_ui_research_loop_actions_router_is_exposed():
    client = TestClient(ui_server.create_app())
    created = client.post("/api/sessions", json={"prefix": "ui"})
    session_id = created.json()["session_id"]

    response = client.get(f"/api/actions/strategies?session_id={session_id}")

    assert response.status_code == 200
    assert "roles" in response.json()


def test_ui_message_route_records_thread(monkeypatch):
    class FakeWorkflow:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self._context = SimpleNamespace(memory={}, iteration=1)

        async def run_once(self, goal):
            return {
                "status": "completed",
                "artifact": {"name": "demo"},
                "execution": {"status": "completed"},
                "review": {"summary": f"reviewed {goal.goal}", "approved": False},
            }

    monkeypatch.setattr(ui_server, "ResearchWorkflow", FakeWorkflow)
    client = TestClient(ui_server.create_app())

    response = client.post("/api/messages", json={"content": "make a model"})

    assert response.status_code == 200
    thread = response.json()["session"]["thread"]
    assert [msg["role"] for msg in thread] == ["user", "assistant"]
    assert "reviewed make a model" in thread[-1]["content"]


def test_ui_session_detail_derives_thread_from_saved_run_history():
    client = TestClient(ui_server.create_app())
    created = client.post("/api/sessions", json={"prefix": "ui", "goal": "saved run"})
    session_id = created.json()["session_id"]
    save_session_meta(
        session_id=session_id,
        goal="saved run",
        iteration=1,
        current_artifact_id=None,
        current_artifact_name=None,
        run_history=[
            {
                "inputs": {"x": 1.0},
                "outputs": {"y": 2.0},
                "status": "completed",
            }
        ],
        target={},
        next_parameters={},
    )

    detail = client.get(f"/api/sessions/{session_id}")

    assert detail.status_code == 200
    thread = detail.json()["thread"]
    assert [msg["role"] for msg in thread] == ["user", "assistant"]
    assert thread[0]["content"] == "saved run"
    assert "Inputs:" not in thread[1]["content"]
    assert "Outputs:" not in thread[1]["content"]
    assert thread[1]["payload"]["status"] == "completed"
    assert thread[1]["payload"]["inputs"] == {"x": 1.0}
    assert thread[1]["payload"]["outputs"] == {"y": 2.0}


def test_ui_session_detail_is_read_only_for_missing_stores():
    client = TestClient(ui_server.create_app())

    created = client.post("/api/sessions", json={"prefix": "ui"})
    session_id = created.json()["session_id"]
    paths = session_paths(session_id)

    assert not Path(paths["artifacts"]).exists()
    assert not Path(paths["runs"]).exists()

    detail = client.get(f"/api/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["artifacts"] == []
    assert detail.json()["results"] == []
    assert not Path(paths["artifacts"]).exists()
    assert not Path(paths["runs"]).exists()


def test_ui_rejects_bad_session_id():
    client = TestClient(ui_server.create_app())

    response = client.get("/api/sessions/..%5Cescape")

    assert response.status_code == 400


def test_ui_server_does_not_import_chat():
    tree = ast.parse(Path(ui_server.__file__).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert all(not name.startswith("arc.chat") for name in imports)


# ── security: bearer-token gate + SSRF validation (same as arc.api) ────────


def _reload_security():
    """Re-import the security module so it re-reads ARC_API_TOKEN."""
    import importlib
    import arc.api.security as sec
    importlib.reload(sec)


def test_ui_data_routes_open_when_no_token(monkeypatch):
    monkeypatch.delenv("ARC_API_TOKEN", raising=False)
    _reload_security()
    client = TestClient(ui_server.create_app())
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 200


def test_ui_data_routes_require_token_when_configured(monkeypatch):
    monkeypatch.setenv("ARC_API_TOKEN", "secret123")
    _reload_security()
    try:
        client = TestClient(ui_server.create_app())
        # health stays open so the page can probe before auth
        assert client.get("/api/health").status_code == 200
        # data routes are gated
        assert client.get("/api/sessions").status_code == 401
        assert client.get(
            "/api/sessions", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert client.get(
            "/api/sessions", headers={"Authorization": "Bearer secret123"}
        ).status_code == 200
    finally:
        monkeypatch.delenv("ARC_API_TOKEN", raising=False)
        _reload_security()


def test_ui_research_run_rejects_private_base_url(monkeypatch):
    """A provider base_url pointing at a link-local/metadata host is
    rejected (SSRF guard) with 400 before reaching the provider."""
    monkeypatch.delenv("ARC_API_TOKEN", raising=False)
    monkeypatch.delenv("ARC_PROVIDER_ALLOWLIST", raising=False)
    _reload_security()
    client = TestClient(ui_server.create_app())
    resp = client.post("/api/research/run", json={
        "goal": "x",
        "provider": "openwebui",
        "base_url": "http://169.254.169.254/latest/meta-data",
    })
    assert resp.status_code == 400
