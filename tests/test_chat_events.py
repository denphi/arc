"""Structured event sink tests (Phase 2)."""

import json

import pytest

from arc.chat import ui
from arc.chat.events import (
    AnsiSink,
    ChatEvent,
    JsonlSink,
    MultiSink,
    Sink,
    StdoutJsonSink,
    emit,
    set_sink,
)


pytestmark = pytest.mark.chat


@pytest.fixture(autouse=True)
def _restore_sink():
    """Make sure no test bleeds a sink into the next one."""
    prev = set_sink(None)
    yield
    set_sink(prev)


# ── ChatEvent serialization ───────────────────────────────────────────────

def test_chat_event_round_trips_as_json():
    ev = ChatEvent(kind="ok", text="all good", meta={"phase": "review"})
    parsed = json.loads(json.dumps(ev.to_jsonable()))
    assert parsed["kind"] == "ok"
    assert parsed["text"] == "all good"
    assert parsed["meta"]["phase"] == "review"
    assert "ts" in parsed


def test_chat_event_truncates_long_text():
    huge = "x" * 10_000
    ev = ChatEvent(kind="info", text=huge)
    out = ev.to_jsonable()
    assert len(out["text"]) < 5_000
    assert out["text"].endswith("…[truncated]")


def test_chat_event_short_text_unchanged():
    ev = ChatEvent(kind="info", text="short")
    assert ev.to_jsonable()["text"] == "short"


# ── emit() / set_sink() lifecycle ─────────────────────────────────────────

def test_emit_is_noop_without_sink():
    # No sink set; emit must not raise
    emit("ok", "hello")  # passes if no exception


def test_set_sink_returns_previous():
    s1 = AnsiSink()
    s2 = AnsiSink()
    assert set_sink(s1) is None  # no previous
    assert set_sink(s2) is s1
    assert set_sink(None) is s2


# ── Capturing sink (for assertions) ───────────────────────────────────────

class CapturingSink(Sink):
    def __init__(self):
        self.events: list[ChatEvent] = []

    def handle(self, ev: ChatEvent) -> None:
        self.events.append(ev)


def test_emit_routes_to_active_sink():
    sink = CapturingSink()
    set_sink(sink)
    emit("ok", "yay")
    emit("warn", "careful", phase="planning")
    assert [e.kind for e in sink.events] == ["ok", "warn"]
    assert sink.events[0].text == "yay"
    assert sink.events[1].meta == {"phase": "planning"}


# ── ui helpers go through emit() when a sink is set ───────────────────────

def test_ui_ok_emits_when_sink_active(capsys):
    sink = CapturingSink()
    set_sink(sink)
    ui.ok("hello")
    # With a sink active, no direct print
    captured = capsys.readouterr()
    assert "hello" not in captured.out
    # Event made it to the sink
    assert len(sink.events) == 1
    assert sink.events[0].kind == "ok"
    assert sink.events[0].text == "hello"


def test_ui_ok_renders_when_no_sink(capsys):
    set_sink(None)
    ui.ok("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_ui_step_event_carries_label_in_meta(capsys):
    sink = CapturingSink()
    set_sink(sink)
    ui.step("Status", "completed")
    assert capsys.readouterr().out == ""  # no direct print
    assert sink.events[0].kind == "step"
    assert sink.events[0].meta["label"] == "Status"
    assert sink.events[0].text == "completed"


@pytest.mark.parametrize("fn, kind", [
    (ui.ok,     "ok"),
    (ui.warn,   "warn"),
    (ui.err,    "err"),
    (ui.header, "header"),
    (ui.hr,     "hr"),
])
def test_ui_helpers_emit_correct_event_kind(fn, kind):
    sink = CapturingSink()
    set_sink(sink)
    if fn is ui.hr:
        fn()
    else:
        fn("test")
    assert sink.events[0].kind == kind


def test_ansi_sink_silent_on_meta_events_by_default(capsys):
    """Phase/agent events should NOT pollute the terminal in default mode."""
    sink = AnsiSink()
    sink.handle(ChatEvent(kind="phase_start", text="validation"))
    sink.handle(ChatEvent(kind="agent_call", text="reviewer"))
    sink.handle(ChatEvent(kind="tool_call", text="set_target"))
    captured = capsys.readouterr()
    assert captured.out == "", f"expected silence, got: {captured.out!r}"


def test_ansi_sink_verbose_surfaces_meta_events(capsys):
    """--events-debug-equivalent: verbose AnsiSink shows meta events."""
    sink = AnsiSink(verbose=True)
    sink.handle(ChatEvent(kind="phase_start", text="validation"))
    captured = capsys.readouterr()
    assert "phase_start" in captured.out
    assert "validation" in captured.out


# ── JsonlSink ─────────────────────────────────────────────────────────────

def test_jsonl_sink_writes_one_line_per_event(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    sink.handle(ChatEvent(kind="ok", text="a"))
    sink.handle(ChatEvent(kind="warn", text="b"))
    sink.close()
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "a"
    assert json.loads(lines[1])["text"] == "b"


def test_jsonl_sink_round_trip(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    for i in range(5):
        sink.handle(ChatEvent(kind="info", text=f"line {i}", meta={"i": i}))
    sink.close()
    # File must be one valid JSON object per line
    for line in path.read_text().splitlines():
        obj = json.loads(line)
        assert "kind" in obj and "text" in obj


def test_jsonl_sink_rotates_at_size_cap(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path, max_bytes=200)
    # Write enough to exceed the cap
    for i in range(20):
        sink.handle(ChatEvent(kind="info", text="x" * 50))
    sink.close()
    # Active file shouldn't be wildly bigger than the cap (next rotation
    # won't happen until next write)
    rotated = tmp_path / "events.jsonl.1"
    assert rotated.exists(), "rotation file should exist after exceeding cap"


def test_jsonl_sink_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "deep" / "events.jsonl"
    sink = JsonlSink(path)
    sink.handle(ChatEvent(kind="info", text="hi"))
    sink.close()
    assert path.exists()


def test_jsonl_sink_truncates_runaway_text(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    sink.handle(ChatEvent(kind="err", text="x" * 100_000))
    sink.close()
    parsed = json.loads(path.read_text().strip())
    assert len(parsed["text"]) < 10_000


# ── StdoutJsonSink ────────────────────────────────────────────────────────

def test_stdout_json_sink_writes_to_stdout(capsys):
    sink = StdoutJsonSink()
    sink.handle(ChatEvent(kind="ok", text="hi"))
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["kind"] == "ok"


# ── Deferred SinkConfig (Finding #4) ─────────────────────────────────────


def test_sink_config_round_trips():
    from arc.chat.events import SinkConfig, get_sink_config, set_sink_config
    cfg = SinkConfig(kind="jsonl", path=None)
    prev = set_sink_config(cfg)
    try:
        assert get_sink_config() is cfg
        assert prev is None
    finally:
        set_sink_config(None)


def test_sink_config_rejects_unknown_kind():
    """R3-5: unknown kind raises at construction."""
    from arc.chat.events import SinkConfig
    with pytest.raises(ValueError, match="must be one of"):
        SinkConfig(kind="banana")


def test_sink_config_accepts_all_documented_kinds():
    from arc.chat.events import SinkConfig
    for k in ("ansi", "jsonl", "stdout-json", "multi"):
        SinkConfig(kind=k)  # no raise


def test_materialise_pending_sink_uses_per_session_path(tmp_path, monkeypatch):
    """When --events jsonl is set without --events-path, the materialiser
    must put the file under <session_dir>/events.jsonl."""
    from types import SimpleNamespace
    from arc.chat.events import SinkConfig, set_sink_config, current_sink
    from arc.chat.loop import _materialise_pending_sink

    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    workflow = SimpleNamespace(session_id="my-session-abc")
    set_sink_config(SinkConfig(kind="jsonl", path=None))
    try:
        _materialise_pending_sink(workflow)
        expected = tmp_path / "home" / "my-session-abc" / "events.jsonl"
        assert expected.parent.exists()
        # Sink is installed and file path is reachable
        assert current_sink() is not None
    finally:
        set_sink_config(None)
        from arc.chat.events import set_sink
        set_sink(None)


def test_materialise_pending_sink_honours_explicit_path(tmp_path, monkeypatch):
    """--events-path override is preserved verbatim."""
    from types import SimpleNamespace
    from arc.chat.events import SinkConfig, set_sink_config, current_sink, ChatEvent
    from arc.chat.loop import _materialise_pending_sink

    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    explicit = tmp_path / "elsewhere" / "log.jsonl"
    workflow = SimpleNamespace(session_id="any")
    set_sink_config(SinkConfig(kind="jsonl", path=explicit))
    try:
        _materialise_pending_sink(workflow)
        # Now write an event and check the explicit path got the line
        sink = current_sink()
        sink.handle(ChatEvent(kind="info", text="hello"))
        sink.close()
        assert explicit.exists()
        assert "hello" in explicit.read_text()
    finally:
        set_sink_config(None)
        from arc.chat.events import set_sink
        set_sink(None)


def test_materialise_is_idempotent(monkeypatch, tmp_path):
    """Second call must not double-install."""
    from types import SimpleNamespace
    from arc.chat.events import SinkConfig, set_sink_config, get_sink_config, current_sink
    from arc.chat.loop import _materialise_pending_sink

    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    set_sink_config(SinkConfig(kind="jsonl"))
    try:
        _materialise_pending_sink(SimpleNamespace(session_id="x"))
        first = current_sink()
        # Second call is a no-op because config was consumed
        _materialise_pending_sink(SimpleNamespace(session_id="x"))
        assert current_sink() is first
        assert get_sink_config() is None
    finally:
        set_sink_config(None)
        from arc.chat.events import set_sink
        set_sink(None)


def test_materialise_rejects_unsafe_session_id(monkeypatch, tmp_path, caplog):
    """R3-2: a session id with path traversal must not produce a sink
    rooted outside SIM2L_HOME."""
    from types import SimpleNamespace
    from arc.chat.events import SinkConfig, set_sink_config, current_sink
    from arc.chat.loop import _materialise_pending_sink

    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    # Hostile session id: contains a path separator
    set_sink_config(SinkConfig(kind="jsonl"))
    try:
        _materialise_pending_sink(SimpleNamespace(session_id="../../etc"))
        # No sink installed
        assert current_sink() is None
        # Nothing written under SIM2L_HOME
        leaks = list((tmp_path / "home").rglob("events.jsonl")) if (tmp_path / "home").exists() else []
        assert leaks == []
        # And the malicious dir wasn't created upstream
        assert not (tmp_path / "etc").exists()
    finally:
        set_sink_config(None)
        from arc.chat.events import set_sink
        set_sink(None)


# ── MultiSink ─────────────────────────────────────────────────────────────

def test_multi_sink_fans_out(tmp_path):
    cap = CapturingSink()
    path = tmp_path / "events.jsonl"
    jsonl = JsonlSink(path)
    multi = MultiSink(cap, jsonl)
    multi.handle(ChatEvent(kind="ok", text="hi"))
    multi.close()
    assert cap.events[0].text == "hi"
    assert json.loads(path.read_text().strip())["text"] == "hi"


def test_multi_sink_swallows_failing_subsink(tmp_path):
    """One sink raising must not break the others."""
    class BoomSink(Sink):
        def handle(self, ev):
            raise RuntimeError("kaboom")
    cap = CapturingSink()
    multi = MultiSink(BoomSink(), cap)
    multi.handle(ChatEvent(kind="ok", text="hi"))
    assert cap.events[0].text == "hi"  # second sink still got it
