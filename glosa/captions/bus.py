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
# grow memory without limit. Once its queue is full the subscriber is marked
# overflowed: nothing more is queued for it, it is handed what it already
# has, and then its stream ENDS. The browser's EventSource reconnects on its
# own with its Last-Event-ID and subscribe() replays the gap from the
# track's buffer -- no silent hole in the middle of a caption (dropping
# single "append"s would corrupt the text on screen with no way to notice).
SUBSCRIBER_QUEUE_MAX = 500


class _TrackState:
    """Per-(room_id, lang) bookkeeping: id counter, replay buffer, subscribers."""

    def __init__(self, buffer_size: int) -> None:
        self.next_id = 1
        self.buffer: deque[tuple[CaptionMsg, str | None]] = deque(maxlen=buffer_size)
        self.subscribers: set[asyncio.Queue[CaptionMsg]] = set()
        self.current_talk_id: str | None = None
        # Subscriber queues that filled up: they get nothing more and their
        # stream ends once drained (see SUBSCRIBER_QUEUE_MAX).
        self.overflowed: set[asyncio.Queue[CaptionMsg]] = set()


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
            if queue in track.overflowed:
                continue
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Never block the publisher and never raise: end this
                # subscriber's stream once drained; it reconnects and replays.
                track.overflowed.add(queue)
                log.warning(
                    "room %s lang %s: a subscriber fell %d messages behind: ending its stream "
                    "(the client reconnects and replays from Last-Event-ID)",
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
                if queue in track.overflowed and queue.empty():
                    return  # fell behind: the client reconnects and replays the rest
                msg = await queue.get()
                yield msg
        finally:
            track.subscribers.discard(queue)
            track.overflowed.discard(queue)

    def history(self, room_id: str, lang: str, talk_id: str) -> list[CaptionMsg]:
        """Buffered messages published while talk_id was current, for latecomers."""
        track = self._track(room_id, lang)
        return [msg for msg, tid in track.buffer if tid == talk_id]
