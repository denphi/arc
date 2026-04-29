import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class EventBus:
    """Simple async event bus for intra-kernel communication."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable[..., Coroutine]) -> None:
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Callable) -> None:
        self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def emit(self, event: str, payload: Any = None) -> None:
        handlers = self._handlers.get(event, [])
        if not handlers:
            return
        await asyncio.gather(
            *(h(payload) for h in handlers),
            return_exceptions=True,
        )
        logger.debug("Event '%s' dispatched to %d handler(s)", event, len(handlers))
