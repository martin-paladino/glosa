"""CaptionBus: pub/sub fan-out of CaptionMsg per (room_id, lang), with a
bounded circular replay buffer so late subscribers (or ones resuming with
Last-Event-ID) can catch up before switching to the live stream.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator

from glosa.models import CaptionMsg


class _TrackState:
    """Per-(room_id, lang) bookkeeping: id counter, replay buffer, subscribers."""

    def __init__(self, buffer_size: int) -> None:
        self.next_id = 1
        self.buffer: deque[tuple[CaptionMsg, str | None]] = deque(maxlen=buffer_size)
        self.subscribers: set[asyncio.Queue[CaptionMsg]] = set()
        self.current_talk_id: str | None = None


class CaptionBus:
    """In-memory pub/sub bus for caption messages.

    One CaptionBus is shared across all rooms/languages for the process;
    state is partitioned internally by (room_id, lang).
    """

    def __init__(self, buffer_size: int = 2000) -> None:
        self._buffer_size = buffer_size
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
        msg = CaptionMsg(id=track.next_id, type=type, **payload)
        track.next_id += 1

        if type == "talk":
            data = payload.get("data") or {}
            track.current_talk_id = data.get("talk_id")

        track.buffer.append((msg, track.current_talk_id))
        for queue in track.subscribers:
            queue.put_nowait(msg)
        return msg

    async def subscribe(
        self, room_id: str, lang: str, last_event_id: int | None
    ) -> AsyncIterator[CaptionMsg]:
        """Replay buffered messages with id > last_event_id, then live ones.

        Registration happens before the buffer snapshot is read, and both
        steps run without an `await` in between, so no publish() (itself
        synchronous) can land in the gap and be missed or duplicated.
        """
        track = self._track(room_id, lang)
        queue: asyncio.Queue[CaptionMsg] = asyncio.Queue()
        track.subscribers.add(queue)
        try:
            threshold = last_event_id if last_event_id is not None else 0
            backlog = [msg for msg, _talk_id in track.buffer if msg.id > threshold]
            for msg in backlog:
                yield msg
            while True:
                msg = await queue.get()
                yield msg
        finally:
            track.subscribers.discard(queue)

    def history(self, room_id: str, lang: str, talk_id: str) -> list[CaptionMsg]:
        """Buffered messages published while talk_id was current, for latecomers."""
        track = self._track(room_id, lang)
        return [msg for msg, tid in track.buffer if tid == talk_id]
