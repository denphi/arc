"""Structured chat events.

Every user-visible message in the chat is also a structured event. Two
sinks consume events:

  * ``AnsiSink``  — reproduces the current terminal output (default).
  * ``JsonlSink`` — appends JSON-line records to a session file for
                    later analysis / web-UI consumption.

The existing ``ui`` helpers (``ok``, ``warn``, ``err``, ``step``,
``header``, ``hr``) call through ``emit()`` so adding a new sink is
zero-churn.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Optional


EventKind = Literal[
    "info",         # plain text line
    "step",         # label + value pair (e.g. "Status: completed")
    "header",       # section header
    "hr",           # horizontal rule
    "ok",           # success indicator
    "warn",         # warning indicator
    "err",          # error indicator
    "phase_start",  # research pipeline phase begins
    "phase_end",    # research pipeline phase ends
    "agent_call",   # llm / agent invocation
    "tool_call",    # tool dispatch
    "user_input",   # user typed something
    "llm_reply",    # llm responded
]


# Truncation cap for the ``text`` field in JSONL sinks. Keeps a runaway
# stack trace from filling the disk.
_MAX_TEXT_LEN = 4096


@dataclass
class ChatEvent:
    kind: EventKind
    text: str = ""
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_jsonable(self) -> dict[str, Any]:
        """Return a dict suitable for ``json.dumps``. Truncates text."""
        d = asdict(self)
        if isinstance(d.get("text"), str) and len(d["text"]) > _MAX_TEXT_LEN:
            d["text"] = d["text"][:_MAX_TEXT_LEN] + "…[truncated]"
        return d


# ── Sinks ─────────────────────────────────────────────────────────────────

class Sink:
    """Base sink. Subclasses implement ``handle``."""

    def handle(self, event: ChatEvent) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Optional teardown — flush, close files, etc."""


class AnsiSink(Sink):
    """Reproduces the current terminal output via ``arc.chat.ui`` helpers.

    Kept thin: it does no formatting itself. The actual ANSI rendering
    lives in ``ui.py`` where it always has.

    Phase/agent/tool events are silent by default — they belong in the
    JSONL stream, not the user-facing terminal. Construct with
    ``AnsiSink(verbose=True)`` (or the ``--events-debug`` flag) to
    surface them as dimmed informational lines.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def handle(self, event: ChatEvent) -> None:
        # Late import to avoid a cycle with ui.py (ui.py uses emit() too).
        from arc.chat import ui
        kind = event.kind
        text = event.text
        if kind == "info":
            print(text)
        elif kind == "header":
            ui._render_header(text)
        elif kind == "step":
            label = event.meta.get("label", "")
            ui._render_step(label, text)
        elif kind == "ok":
            ui._render_ok(text)
        elif kind == "warn":
            ui._render_warn(text)
        elif kind == "err":
            ui._render_err(text)
        elif kind == "hr":
            ui._render_hr()
        elif self.verbose:
            # Verbose mode: surface phase/agent/tool/user_input/llm_reply
            # as DIM informational lines so terminal users can see them
            # without breaking the visual rhythm.
            print(ui.c(f"  ⓘ {kind}: {text}", ui.DIM))


class JsonlSink(Sink):
    """Append-only JSON-lines event stream.

    Used by ``arc chat --events stdout-json`` and (Phase 4) by web-UI /
    CI agents that need to consume the chat output programmatically.

    File rotation: rotates to ``<path>.1`` when the active file passes
    the size cap (10 MiB by default). Older rotations are NOT kept —
    one rotation is enough to bound disk usage; the JSONL is meant for
    short-term inspection, not durable history.
    """

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._fh = None  # opened lazily

    def _open(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        return self._fh

    def _maybe_rotate(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        rotated = self.path.with_suffix(self.path.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            self.path.rename(rotated)
        except OSError:
            # If rotation fails (e.g. read-only fs) we just keep writing —
            # the cap is a soft limit.
            pass

    def handle(self, event: ChatEvent) -> None:
        with self._lock:
            self._maybe_rotate()
            line = json.dumps(event.to_jsonable(), default=str)
            fh = self._open()
            fh.write(line + "\n")
            fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


class StdoutJsonSink(Sink):
    """Emit JSON lines straight to stdout. For piping to ``jq``."""

    def __init__(self):
        self._lock = Lock()

    def handle(self, event: ChatEvent) -> None:
        line = json.dumps(event.to_jsonable(), default=str)
        with self._lock:
            print(line, flush=True)


class MultiSink(Sink):
    """Fan out one event to several sinks. A buggy sink can't crash the
    pipeline — exceptions are caught and logged."""

    def __init__(self, *sinks: Sink):
        self.sinks = list(sinks)

    def handle(self, event: ChatEvent) -> None:
        for sink in self.sinks:
            try:
                sink.handle(event)
            except Exception:  # noqa: BLE001
                # Sink failures must never break the chat. Swallow silently
                # (we don't have a logger configured at this layer; the
                # event itself will appear in any other sink that works).
                pass

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                pass


# ── Global emit() ─────────────────────────────────────────────────────────

_current_sink: Optional[Sink] = None


SinkKind = Literal["ansi", "jsonl", "stdout-json", "multi"]


@dataclass
class SinkConfig:
    """Deferred sink configuration.

    The CLI knows what the user asked for (``--events jsonl``) before the
    REPL has a session id. We stash the config here; ``chat_loop`` reads
    it and instantiates the actual sink once the session dir is known,
    so the per-session default for ``--events-path`` can resolve to
    ``<session_dir>/events.jsonl`` instead of a single global file.
    """
    kind: SinkKind = "ansi"
    path: Optional[Path] = None      # explicit override; otherwise per-session

    def __post_init__(self) -> None:
        # Dataclasses don't enforce ``Literal`` at runtime. Validate here
        # so a stray value from a future caller raises immediately
        # rather than silently selecting the default branch.
        valid = {"ansi", "jsonl", "stdout-json", "multi"}
        if self.kind not in valid:
            raise ValueError(
                f"SinkConfig.kind must be one of {sorted(valid)}, got {self.kind!r}"
            )


_pending_sink_config: Optional[SinkConfig] = None


def set_sink_config(config: Optional[SinkConfig]) -> Optional[SinkConfig]:
    """Stash the sink config for ``chat_loop`` to materialise later."""
    global _pending_sink_config
    prev = _pending_sink_config
    _pending_sink_config = config
    return prev


def get_sink_config() -> Optional[SinkConfig]:
    return _pending_sink_config


def set_sink(sink: Optional[Sink]) -> Optional[Sink]:
    """Install a sink. Returns the previous one so tests can restore."""
    global _current_sink
    previous = _current_sink
    _current_sink = sink
    return previous


def current_sink() -> Optional[Sink]:
    return _current_sink


def emit(kind: EventKind, text: str = "", **meta) -> None:
    """Emit a structured event to the active sink.

    Safe no-op when no sink is configured (tests that don't use events).
    """
    sink = _current_sink
    if sink is None:
        return
    sink.handle(ChatEvent(kind=kind, text=text, meta=meta))
