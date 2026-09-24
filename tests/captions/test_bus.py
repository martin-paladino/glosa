"""Tests for CaptionBus: per-(room, lang) pub/sub fan-out with a bounded
replay buffer (Last-Event-ID resume) and per-talk history for latecomers.
"""

from __future__ import annotations

import asyncio

import pytest

from glosa.captions.bus import CaptionBus
from glosa.models import CaptionMsg


async def _take(aiter, n: int) -> list[CaptionMsg]:
    """Collect exactly n items from an async iterator, then close it."""
    out: list[CaptionMsg] = []
    try:
        async for item in aiter:
            out.append(item)
            if len(out) == n:
                break
    finally:
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            await aclose()
    return out


def test_publish_assigns_incrementing_ids_per_room_and_lang() -> None:
    bus = CaptionBus()

    first = bus.publish("room1", "es", "append", seg=0, text="Hola")
    second = bus.publish("room1", "es", "append", seg=0, text=" mundo")
    other_lang = bus.publish("room1", "en", "append", seg=0, text="Hello")

    assert first.id == 1
    assert second.id == 2
    assert other_lang.id == 1  # independent counter per (room, lang)


async def test_two_subscribers_receive_the_same_live_messages() -> None:
    bus = CaptionBus()
    sub_a = bus.subscribe("room1", "es", last_event_id=None)
    sub_b = bus.subscribe("room1", "es", last_event_id=None)
    # Prime both generators so they're registered before we publish.
    task_a = asyncio.ensure_future(_take(sub_a, 2))
    task_b = asyncio.ensure_future(_take(sub_b, 2))
    await asyncio.sleep(0)  # let both subscribe() bodies run up to their await

    bus.publish("room1", "es", "append", seg=0, text="Hola")
    bus.publish("room1", "es", "close", seg=0)

    received_a = await task_a
    received_b = await task_b

    assert [m.id for m in received_a] == [1, 2]
    assert [m.id for m in received_b] == [1, 2]
    assert received_a == received_b


async def test_subscribe_with_last_event_id_replays_only_newer_ids() -> None:
    bus = CaptionBus()
    for i in range(1, 8):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    replayed = await _take(bus.subscribe("room1", "es", last_event_id=5), n=2)

    assert [m.id for m in replayed] == [6, 7]


async def test_subscribe_without_last_event_id_replays_whole_buffer() -> None:
    bus = CaptionBus()
    bus.publish("room1", "es", "append", seg=0, text="Hola")
    bus.publish("room1", "es", "close", seg=0)

    replayed = await _take(bus.subscribe("room1", "es", last_event_id=None), n=2)

    assert [m.id for m in replayed] == [1, 2]


async def test_circular_buffer_discards_oldest_messages() -> None:
    bus = CaptionBus(buffer_size=3)
    for i in range(1, 6):  # ids 1..5, only 3,4,5 should remain buffered
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    replayed = await _take(bus.subscribe("room1", "es", last_event_id=None), n=3)

    assert [m.id for m in replayed] == [3, 4, 5]


async def test_history_returns_messages_for_a_given_talk() -> None:
    bus = CaptionBus()
    bus.publish("room1", "es", "talk", data={"talk_id": "talk-a"})
    bus.publish("room1", "es", "append", seg=0, text="from talk a")
    bus.publish("room1", "es", "talk", data={"talk_id": "talk-b"})
    bus.publish("room1", "es", "append", seg=0, text="from talk b")

    history_a = bus.history("room1", "es", "talk-a")
    history_b = bus.history("room1", "es", "talk-b")

    assert [m.text for m in history_a if m.type == "append"] == ["from talk a"]
    assert [m.text for m in history_b if m.type == "append"] == ["from talk b"]
