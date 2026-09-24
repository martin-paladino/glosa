"""AdminEvents: a minimal in-process broadcaster for the admin panel.

``create_app`` keeps one on ``app.state.admin_events``. The admin API
publishes to it (a talk edited, an agenda imported, a room's mode changed, a
talk started or ended) and Task 12's admin SSE endpoint subscribes to it.

    hub.publish("talk_updated", {"talk": {...}})   # never blocks, never raises
    with hub.subscribe() as sub:                    # a bounded queue of its own
        async for event in sub:                     # AdminEvent(id, kind, data, ts)
            ...

Each subscriber has its own bounded queue (``maxsize`` events). A subscriber
that falls behind loses its *oldest* events (``Subscription.dropped`` counts
them), so a stuck browser tab can never block the publisher or grow memory.
Event ids grow by one per publish across all kinds, so a consumer can tell it
missed some.

In-process only: nothing is persisted (the durable record is the ``events``
table, glosa/db.py ``log_event``), and a second instance of the server has
its own hub.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

DEFAULT_MAXSIZE = 256


@dataclass(frozen=True)
class AdminEvent:
    id: int
    kind: str
    data: dict[str, Any]
    ts: float  # wall clock, epoch seconds

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "data": self.data, "ts": self.ts}


class Subscription:
    """One subscriber's queue. An async iterator and a context manager
    (leaving it, or ``close()``, unsubscribes)."""

    def __init__(self, hub: AdminEvents, maxsize: int) -> None:
        self._hub = hub
        self._queue: asyncio.Queue[AdminEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def _offer(self, event: AdminEvent) -> None:
        if self._queue.full():
            self._queue.get_nowait()  # drop the oldest
            self.dropped += 1
        self._queue.put_nowait(event)

    async def get(self) -> AdminEvent:
        return await self._queue.get()

    def get_nowait(self) -> AdminEvent:
        """The oldest queued event; ``asyncio.QueueEmpty`` if there is none."""
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def close(self) -> None:
        self._hub._subs.discard(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __aiter__(self) -> AsyncIterator[AdminEvent]:
        return self

    async def __anext__(self) -> AdminEvent:
        return await self.get()


class AdminEvents:
    def __init__(self, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subs: set[Subscription] = set()
        self._next_id = 1

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, kind: str, data: dict[str, Any]) -> AdminEvent:
        event = AdminEvent(id=self._next_id, kind=kind, data=data, ts=time.time())
        self._next_id += 1
        for sub in list(self._subs):
            sub._offer(event)
        return event

    def subscribe(self) -> Subscription:
        """Start receiving every event published from now on."""
        sub = Subscription(self, self._maxsize)
        self._subs.add(sub)
        return sub
