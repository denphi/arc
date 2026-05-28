"""Coverage tests for ``/services`` start / stop / restart / logs.

The chat command delegates to ``arc.services`` for the actual
daemon-manager calls; we monkeypatch the service-manager primitives
so we can drive every sub-command without spawning real processes.
"""

import pytest

from arc.chat.commands import build_registry
from arc.chat.state import ChatState
from tests.fakes import make_workflow


pytestmark = pytest.mark.chat


def _patch_services(monkeypatch, *, sim2l_available=True,
                     start_returns=None, stop_returns=None):
    """Stub the arc.services API. ``start_returns`` / ``stop_returns``
    are dicts ``name → (success, message)``."""
    from arc import services as svc

    monkeypatch.setattr(svc, "sim2l_available", lambda: sim2l_available)

    start_returns = start_returns or {}
    stop_returns = stop_returns or {}
    started = []
    stopped = []

    def fake_start(name):
        started.append(name)
        return start_returns.get(name, (True, f"{name} started (PID 42)"))

    def fake_stop(name):
        stopped.append(name)
        return stop_returns.get(name, (True, f"{name} stopped"))

    monkeypatch.setattr(svc, "start", fake_start)
    monkeypatch.setattr(svc, "stop", fake_stop)

    # Status — assume nothing running unless caller overrides
    monkeypatch.setattr(
        svc, "status_all",
        lambda: {n: {"running": False, "pid": None, "url": f"http://localhost:{p}"}
                 for n, p in [("cache", 8001), ("catalog", 8002), ("results", 8003)]},
    )

    # Health check used after start/stop to refresh state.sim2l_status
    monkeypatch.setattr(
        "arc.chat.io_utils.check_sim2l_services",
        lambda: {"cache": False, "catalog": False, "results": False},
    )

    return started, stopped


@pytest.mark.asyncio
async def test_services_status_lists_all_three(capsys, monkeypatch):
    _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["status"])
    out = capsys.readouterr().out
    for name in ("cache", "catalog", "results"):
        assert name in out


@pytest.mark.asyncio
async def test_services_start_all_invokes_each(monkeypatch):
    started, _ = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["start"])
    # Started in registry order: cache, catalog, results (alphabetic / config order)
    assert set(started) == {"cache", "catalog", "results"}


@pytest.mark.asyncio
async def test_services_start_specific_targets_one(monkeypatch):
    started, _ = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["start", "catalog"])
    assert started == ["catalog"]


@pytest.mark.asyncio
async def test_services_start_mcp_targets_optional_service(monkeypatch):
    started, _ = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["start", "mcp"])
    assert started == ["mcp"]


@pytest.mark.asyncio
async def test_services_stop_all(monkeypatch):
    _, stopped = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["stop"])
    assert set(stopped) == {"cache", "catalog", "results"}


@pytest.mark.asyncio
async def test_services_stop_specific(monkeypatch):
    _, stopped = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["stop", "results"])
    assert stopped == ["results"]


@pytest.mark.asyncio
async def test_services_restart_stops_then_starts(monkeypatch):
    started, stopped = _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["restart", "cache"])
    assert stopped == ["cache"]
    assert started == ["cache"]


@pytest.mark.asyncio
async def test_services_start_failure_is_shown_as_error(capsys, monkeypatch):
    """Service-manager returned (False, msg) → err() is called."""
    _patch_services(monkeypatch, start_returns={"cache": (False, "port in use")})
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["start", "cache"])
    out = capsys.readouterr().out
    assert "port in use" in out


@pytest.mark.asyncio
async def test_services_unknown_name_rejected(capsys, monkeypatch):
    _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["start", "no-such-service"])
    assert "Unknown service" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_services_logs_when_log_missing(capsys, monkeypatch, tmp_path):
    """``/services logs catalog`` with no log file → warning."""
    _patch_services(monkeypatch)
    from arc import services as svc
    monkeypatch.setattr(svc, "_log_path", lambda name: tmp_path / "missing.log")
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["logs"])
    assert "No log file" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_services_logs_tails_existing_file(capsys, monkeypatch, tmp_path):
    """When a log file exists, its last 40 lines are echoed."""
    _patch_services(monkeypatch)
    log_path = tmp_path / "catalog.log"
    log_path.write_text("\n".join(f"line-{i}" for i in range(50)))
    from arc import services as svc
    monkeypatch.setattr(svc, "_log_path", lambda name: log_path)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["logs", "catalog"])
    out = capsys.readouterr().out
    assert "Last 40 lines" in out
    # The 40 most-recent lines (10..49) are present; older lines (0..9) are not
    assert "line-49" in out
    assert "line-9" not in out


@pytest.mark.asyncio
async def test_services_logs_default_target_is_catalog(monkeypatch, tmp_path):
    """``/services logs`` with no arg defaults to ``catalog``."""
    _patch_services(monkeypatch)
    captured = {}
    from arc import services as svc

    def fake_log_path(name):
        captured["name"] = name
        return tmp_path / "x.log"  # missing file

    monkeypatch.setattr(svc, "_log_path", fake_log_path)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["logs"])
    assert captured["name"] == "catalog"


@pytest.mark.asyncio
async def test_services_logs_unknown_service_rejected(capsys, monkeypatch):
    _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["logs", "no-such"])
    assert "Unknown service" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_services_default_sub_is_status(capsys, monkeypatch):
    """``/services`` with no argv defaults to ``status``."""
    _patch_services(monkeypatch)
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, [])
    out = capsys.readouterr().out
    # The status row contains the URLs; "http://localhost:8002" is enough
    assert "8002" in out
