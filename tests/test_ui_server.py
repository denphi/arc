"""Smoke tests for the standalone ARC browser UI."""

import ast
from pathlib import Path

from fastapi.testclient import TestClient

from arc.session import session_paths
from arc.ui import server as ui_server


def test_ui_health_and_static_index_load():
    client = TestClient(ui_server.create_app())

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ui"] == "arc.ui"

    index = client.get("/")
    assert index.status_code == 200
    assert "ARC UI" in index.text

    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "runResearch" in script.text


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
