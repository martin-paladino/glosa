"""CaptionBus: pub/sub fan-out of CaptionMsg per (room_id, lang), with a
bounded circular replay buffer so late subscribers (or ones resuming with
Last-Event-ID) can catch up before switching to the live stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import AsyncIterator

from glosa.clock import Clock, RealClock
from glosa.models import CaptionMsg

log = logging.getLogger(__name__)

# Bound on each SSE subscriber's per-message queue. A subscriber that can't
# keep up (e.g. a phone on bad wifi) must never make the publisher block or
# grow memory without limit; once full we drop the oldest queued message to
# make room for the newest one. The client already recovers any gap via
# Last-Event-ID replay from the bus's circular buffer on reconnect, so
# losing an already-queued-but-undelivered live message here is safe.
SUBSCRIBER_QUEUE_MAX = 500


class _TrackState:
    """Per-(room_id, lang) bookkeeping: id counter, replay buffer, subscribers."""

    def __init__(self, buffer_size: int) -> None:
        self.next_id = 1
        self.buffer: deque[tuple[CaptionMsg, str | None]] = deque(maxlen=buffer_size)
        self.subscribers: set[asyncio.Queue[CaptionMsg]] = set()
        self.current_talk_id: str | None = None
        # Subscriber queues we've already logged as lagging, so we warn at
        # most once per subscriber rather than once per dropped message.
        self.lagging_logged: set[asyncio.Queue[CaptionMsg]] = set()


class CaptionBus:
    """In-memory pub/sub bus for caption messages.

    One CaptionBus is shared across all rooms/languages for the process;
    state is partitioned internally by (room_id, lang). clock is injectable
    (defaults to RealClock()) so tests can control the wall-clock ts stamped
    on each published CaptionMsg.
    """

    def __init__(self, buffer_size: int = 2000, *, clock: Clock | None = None) -> None:
        self._buffer_size = buffer_size
        self._clock: Clock = clock if clock is not None else RealClock()
        self._tracks: dict[tuple[str, str], _TrackState] = {}

    def _track(self, room_id: str, lang: str) -> _TrackState:
        key = (room_id, lang)
        track = self._tracks.get(key)
        if track is None:
            track = _TrackState(self._buffer_size)
            self._tracks[key] = track
        return track

    def publish(self, room_id: str, lang: str, type: str, **payload) -> CaptionMsg:
        """Create, buffer, and fan out a CaptionMsg for (room_id, lang)."""
        track = self._track(room_id, lang)
        ts = self._clock.wall().timestamp()
        msg = CaptionMsg(id=track.next_id, type=type, ts=ts, **payload)
        track.next_id += 1

        if type == "talk":
            data = payload.get("data") or {}
            track.current_talk_id = data.get("talk_id")

        track.buffer.append((msg, track.current_talk_id))
        for queue in track.subscribers:
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Never block the publisher and never raise: drop the
                # oldest queued message to make room for this one.
                queue.get_nowait()
                queue.put_nowait(msg)
                if queue not in track.lagging_logged:
                    track.lagging_logged.add(queue)
                    log.warning(
                        "room %s lang %s: subscriber queue full (max %d), "
                        "dropping oldest queued message(s)",
                        room_id,
                        lang,
                        SUBSCRIBER_QUEUE_MAX,
                    )
        return msg

    async def subscribe(
        self, room_id: str, lang: str, last_event_id: int | None
    ) -> AsyncIterator[CaptionMsg]:
        """Replay buffered messages with id > last_event_id, then live ones.

        Registration happens before the buffer snapshot is read, and both
        steps run without an `await` in between, so no publish() (itself
        synchronous) can land in the gap and be missed or duplicated.

        A last_event_id greater than the track's newest id is treated the
        same as None (full replay), not as "already caught up": ids restart
        at 1 on every process restart, so a browser reconnecting with a
        Last-Event-ID from before a restart can hold a value higher than
        anything the fresh track has published, which would otherwise
        silently filter out every message that should have been replayed.
        """
        track = self._track(room_id, lang)
        queue: asyncio.Queue[CaptionMsg] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        track.subscribers.add(queue)
        try:
            newest_id = track.next_id - 1
            stale = last_event_id is not None and last_event_id > newest_id
            threshold = 0 if (last_event_id is None or stale) else last_event_id
            backlog = [msg for msg, _talk_id in track.buffer if msg.id > threshold]
            for msg in backlog:
                yield msg
            while True:
                msg = await queue.get()
                yield msg
        finally:
            track.subscribers.discard(queue)
            track.lagging_logged.discard(queue)

    def history(self, room_id: str, lang: str, talk_id: str) -> list[CaptionMsg]:
        """Buffered messages published while talk_id was current, for latecomers."""
        track = self._track(room_id, lang)
        return [msg for msg, tid in track.buffer if tid == talk_id]
