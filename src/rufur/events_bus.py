"""In-process pub/sub that fans service events out to SSE subscribers.

Publishers may run on any thread (REST worker threads, MCP tool threads, the
extraction worker); each subscriber is an asyncio queue bound to its loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(loop, s) for loop, s in self._subs if s is not q]

    def publish(self, kind: str, **data: Any) -> None:
        event = {"type": kind, **data}
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except (RuntimeError, asyncio.QueueFull):
                pass  # loop closed or slow consumer; SSE is best-effort
