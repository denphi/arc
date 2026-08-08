"""UI event model + a tiny in-process pub/sub for Server-Sent Events.

The terminal chat has its own structured events in ``arc.chat.events``, but
the UI must not depend on chat — a deliberate boundary. This is a
small, chat-independent event surface: a workflow/job records phase starts,
phase ends, warnings, generated artifacts, execution results, and review
summaries; the SSE route drains them to the browser, which renders
*structured* events (never ANSI text).

Each job owns one :class:`EventStream`. Producers ``publish`` events; the SSE
handler ``subscribe``\\s to get an ``asyncio.Queue`` of events plus everything
already buffered (so a late subscriber still sees the start of the job). When
the job ends, ``close`` is called and subscribers get a sentinel ``None``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIEvent:
    kind: str                       # phase_start | phase_end | warning | artifact | result | review | status
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "meta": self.meta, "ts": self.ts}


class EventStream:
    """A buffered fan-out of :class:`UIEvent` for one job."""

    def __init__(self, *, max_buffer: int = 1000) -> None:
        self._buffer: list[UIEvent] = []
        self._subscribers: list[asyncio.Queue] = []
        self._closed = False
        self._max_buffer = max_buffer

    def publish(self, event: UIEvent) -> None:
        if self._closed:
            return
        self._buffer.append(event)
        if len(self._buffer) > self._max_buffer:
            self._buffer = self._buffer[-self._max_buffer:]
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def emit(self, kind: str, text: str = "", **meta: Any) -> None:
        """Convenience: build + publish a :class:`UIEvent`."""
        self.publish(UIEvent(kind=kind, text=text, meta=meta))

    def subscribe(self) -> tuple[asyncio.Queue, list[UIEvent]]:
        """Return a live queue + a snapshot of already-buffered events.

        The caller should render the buffered events first, then drain the
        queue, so a subscriber that joins mid-job still sees the full history.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        if self._closed:
            queue.put_nowait(None)
        return queue, list(self._buffer)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)   # sentinel: stream finished

    @property
    def closed(self) -> bool:
        return self._closed

    def buffered(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._buffer]
