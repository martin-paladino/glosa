"""Tests for CaptionBus: per-(room, lang) pub/sub fan-out with a bounded
replay buffer (Last-Event-ID resume) and per-talk history for latecomers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from glosa.captions.bus import SUBSCRIBER_QUEUE_MAX, CaptionBus
from glosa.clock import FakeClock
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


def test_publish_sets_ts_from_injected_clock() -> None:
    clock = FakeClock(start=10.0)
    bus = CaptionBus(clock=clock)

    msg = bus.publish("room1", "es", "append", seg=0, text="Hola")

    assert msg.ts == clock.wall().timestamp()


def test_publish_sets_ts_to_wall_clock_epoch_seconds_by_default() -> None:
    bus = CaptionBus()

    before = time.time()
    msg = bus.publish("room1", "es", "append", seg=0, text="Hola")
    after = time.time()

    assert before <= msg.ts <= after


async def test_subscribe_with_stale_last_event_id_replays_whole_buffer() -> None:
    # Regression for controller Ruling 26 / Task 6 integration bug: after a
    # server restart, ids for a (room, lang) track start over at 1. A
    # browser reconnecting with a Last-Event-ID from before the restart can
    # send an id higher than anything this (fresh) track has ever
    # published. That must not be treated as "already caught up" (which
    # would silently filter out every message) — it must fall back to a
    # full replay of the buffer, same as last_event_id=None.
    bus = CaptionBus()
    for i in range(1, 6):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    replayed = await asyncio.wait_for(
        _take(bus.subscribe("room1", "es", last_event_id=999), n=5), timeout=1.0
    )

    assert [m.id for m in replayed] == [1, 2, 3, 4, 5]


async def _prime_and_abandon(gen):
    """Register a subscriber (so its queue exists) without ever consuming
    from it, simulating a subscriber stuck behind bad wifi. Returns the
    pending __anext__ task so the caller can clean it up afterward.
    """
    task = asyncio.ensure_future(anext(gen))
    await asyncio.sleep(0)  # let subscribe() register and suspend on queue.get()
    return task


async def _abandon_cleanup(gen, task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await gen.aclose()


async def test_publish_never_blocks_or_raises_when_subscriber_queue_is_full() -> None:
    bus = CaptionBus()
    sub = bus.subscribe("room1", "es", last_event_id=None)
    task = await _prime_and_abandon(sub)

    # Flood well past the bound; a naive unbounded/blocking queue would
    # either grow forever or raise asyncio.QueueFull here.
    for i in range(SUBSCRIBER_QUEUE_MAX + 50):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    await _abandon_cleanup(sub, task)


async def test_a_subscriber_that_falls_behind_gets_its_queue_then_its_stream_ends_and_replay_fills_the_gap() -> None:
    bus = CaptionBus()
    sub = bus.subscribe("room1", "es", last_event_id=None)
    bus.publish("room1", "es", "append", seg=0, text="word0")
    first = await anext(sub)  # registered and reading; now it stops reading while the room keeps publishing
    assert first.id == 1

    total = SUBSCRIBER_QUEUE_MAX + 50
    for i in range(1, total):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    received = [msg async for msg in sub]  # drains what was queued, then the stream ends
    assert [m.id for m in received] == list(range(2, SUBSCRIBER_QUEUE_MAX + 2))  # contiguous, no hole

    # What EventSource does next: reconnect with the last id it saw.
    replay = bus.subscribe("room1", "es", last_event_id=received[-1].id)
    rest = await _take(replay, total - SUBSCRIBER_QUEUE_MAX - 1)
    assert [m.id for m in rest] == list(range(SUBSCRIBER_QUEUE_MAX + 2, total + 1))


async def test_full_queue_does_not_affect_other_subscribers() -> None:
    bus = CaptionBus()
    slow = bus.subscribe("room1", "es", last_event_id=None)
    normal = bus.subscribe("room1", "es", last_event_id=None)
    slow_task = await _prime_and_abandon(slow)
    normal_task = asyncio.ensure_future(anext(normal))
    await asyncio.sleep(0)

    total = SUBSCRIBER_QUEUE_MAX + 20
    normal_received: list[CaptionMsg] = []
    for i in range(total):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")
        # Actively drain the "normal" subscriber as we go, unlike "slow".
        msg = await normal_task
        normal_received.append(msg)
        normal_task = asyncio.ensure_future(anext(normal))

    normal_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await normal_task
    await normal.aclose()
    await _abandon_cleanup(slow, slow_task)

    assert [m.id for m in normal_received] == list(range(1, total + 1))


async def test_logs_lagging_subscriber_at_most_once(caplog: pytest.LogCaptureFixture) -> None:
    bus = CaptionBus()
    sub = bus.subscribe("room1", "es", last_event_id=None)
    task = await _prime_and_abandon(sub)

    with caplog.at_level(logging.WARNING, logger="glosa.captions.bus"):
        for i in range(SUBSCRIBER_QUEUE_MAX + 50):
            bus.publish("room1", "es", "append", seg=0, text=f"word{i}")

    await _abandon_cleanup(sub, task)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


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
