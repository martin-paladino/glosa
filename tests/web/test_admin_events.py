"""glosa.web.admin_events: the in-process broadcaster behind the admin panel
(Task 12 streams it over SSE). Bounded per-subscriber queues that drop the
oldest event, never block the publisher."""

from __future__ import annotations

import asyncio

from glosa.web.admin_events import AdminEvents


async def test_every_subscriber_gets_every_event_in_order() -> None:
    hub = AdminEvents()
    first, second = hub.subscribe(), hub.subscribe()

    a = hub.publish("talk_updated", {"talk_id": "t1"})
    b = hub.publish("room_mode", {"room_id": "r1", "mode": "manual"})

    assert b.id == a.id + 1
    for sub in (first, second):
        got = [await sub.get(), await sub.get()]
        assert [(e.kind, e.data) for e in got] == [
            ("talk_updated", {"talk_id": "t1"}),
            ("room_mode", {"room_id": "r1", "mode": "manual"}),
        ]


async def test_a_slow_subscriber_loses_the_oldest_events_not_the_newest() -> None:
    hub = AdminEvents(maxsize=3)
    sub = hub.subscribe()

    for i in range(5):
        hub.publish("tick", {"n": i})  # never blocks, never raises

    assert sub.dropped == 2
    assert [(await sub.get()).data["n"] for _ in range(3)] == [2, 3, 4]


async def test_a_closed_subscription_stops_receiving() -> None:
    hub = AdminEvents()
    with hub.subscribe() as sub:
        hub.publish("a", {})
        assert (await sub.get()).kind == "a"
    assert hub.subscribers == 0
    hub.publish("b", {})  # nobody listening: fine


async def test_async_iteration_waits_for_the_next_event() -> None:
    hub = AdminEvents()
    sub = hub.subscribe()

    async def first_two() -> list[str]:
        out = []
        async for event in sub:
            out.append(event.kind)
            if len(out) == 2:
                break
        return out

    reader = asyncio.create_task(first_two())
    await asyncio.sleep(0)
    hub.publish("one", {})
    hub.publish("two", {})

    assert await asyncio.wait_for(reader, 1) == ["one", "two"]
    sub.close()


async def test_events_carry_a_wall_clock_timestamp() -> None:
    hub = AdminEvents()
    event = hub.publish("x", {"k": 1})
    assert event.ts > 1_700_000_000
    assert event.as_dict() == {"id": event.id, "kind": "x", "data": {"k": 1}, "ts": event.ts}
