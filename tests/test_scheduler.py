"""Autopilot (glosa/scheduler.py): the agenda drives the rooms.

Most tests use FakeClock and FakeWorker, a stand-in with the part of
RoomWorker the autopilot uses (``room``, ``talk``, ``start(talk)``,
``stop()``, ``reconnect(reason)``) that, like the real one, ends the running
talk before it opens another and writes both to the database. The database
is the real SQLite one. The last test runs the real RoomWorker (engine and
audio faked, simulated time).

The event is in Buenos Aires (UTC-3); FakeClock's wall clock starts at
2026-01-01 00:00 UTC, and the talks are on 2026-01-02.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from glosa.captions.bus import CaptionBus
from glosa.clock import FakeClock, RealClock
from glosa.config import RoomCfg, Settings
from glosa.db import init_db
from glosa.models import AudioChunk, EngineEvent, Room, Talk
from glosa.room import RoomWorker
from glosa.scheduler import Autopilot, NoTalkToRestart
from glosa.web.admin_events import AdminEvents

TZ = "America/Argentina/Buenos_Aires"
ART = ZoneInfo(TZ)
DAY = date(2026, 1, 2)


def at(hm: str, day: date = DAY) -> datetime:
    """HH:MM[:SS] of ``day`` in Buenos Aires."""
    parts = [int(p) for p in hm.split(":")]
    return datetime(day.year, day.month, day.day, *parts, tzinfo=ART)


def move_to(clock: FakeClock, when: datetime) -> None:
    clock.advance((when - clock.wall()).total_seconds())


def _room(room_id: str, mode: str = "auto") -> Room:
    return Room(
        id=room_id, slug=room_id, name=f"Sala {room_id}", source_type="file", source_url=f"fake://{room_id}",
        mode=mode, public_token=f"tok-{room_id}", default_targets=["es"],
    )


def _talk(talk_id: str, start: str, end: str, room_id: str = "r1", day: date = DAY) -> Talk:
    return Talk(
        id=talk_id, room_id=room_id, title=f"Talk {talk_id}", speakers=["Ana"], language="en", targets=["es"],
        engine="fast", start=at(start, day), end=at(end, day), abstract="", tags=[], glossary=[],
        status="scheduled", actual_start=None, actual_end=None,
    )


class FakeWorker:
    def __init__(self, room: Room, db, clock: FakeClock, *, has_source: bool = True) -> None:
        self.room = room
        self.db = db
        self.clock = clock
        self.has_source = has_source
        self.talk: Talk | None = None
        self.calls: list[tuple] = []

    async def start(self, talk: Talk | None = None) -> None:
        if not self.has_source:
            raise ValueError(f"room {self.room.id!r} has no audio source")
        if self.talk is not None and (talk is None or talk.id != self.talk.id):
            await self._end()
        wall = self.clock.wall()
        talk = talk or self.talk or self.free_talk()
        if talk.actual_start is None:
            talk.actual_start = wall
        talk.status = "live"
        await self.db.insert_talks([talk])
        await self.db.update_talk(talk.id, status="live", actual_start=talk.actual_start)
        self.talk = talk
        self.calls.append(("start", talk.id, wall))

    async def stop(self) -> None:
        if self.talk is not None:
            await self._end()
        self.calls.append(("stop", self.clock.wall()))

    async def reconnect(self, reason: str) -> None:
        self.calls.append(("reconnect", reason))

    def free_talk(self) -> Talk:
        now = self.clock.wall().astimezone(ART)
        talk = _talk(f"free-{self.room.id}-{now:%Y%m%dT%H%M%S}", "00:00", "23:59", self.room.id, now.date())
        return replace(talk, title="Sesión libre")

    async def _end(self) -> None:
        talk, self.talk = self.talk, None
        assert talk is not None
        talk.status, talk.actual_end = "done", self.clock.wall()
        await self.db.update_talk(talk.id, status="done", actual_end=talk.actual_end)
        self.calls.append(("end", talk.id, talk.actual_end))

    def starts(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "start"]


@pytest.fixture
def db(tmp_path: Path):
    database = init_db(tmp_path / "glosa.db")
    yield database
    database.close()


async def _setup(db, *rooms: Room, talks: list[Talk] = (), at_time: datetime | None = None, tz: str = TZ):
    clock = FakeClock()
    if at_time is not None:
        move_to(clock, at_time)
    for room in rooms:
        await db.upsert_room(room)
    await db.insert_talks(list(talks))
    workers = {room.id: FakeWorker(replace(room), db, clock) for room in rooms}
    events = AdminEvents()
    pilot = Autopilot(db, workers, clock, lead_s=60, tz=tz, events=events)
    return clock, workers, pilot, events


# ------------------------------------------------------------------- auto (9.1)


async def test_a_talk_opens_lead_s_early_and_closes_at_its_end(db) -> None:  # 9.1
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")], at_time=at("13:00"))
    worker = workers["r1"]
    await worker.start(None)  # the free session the room started with at boot

    await pilot.tick()  # 13:00: the room has talks today and none is on: idle
    assert worker.talk is None and worker.calls[-1][0] == "stop"

    move_to(clock, at("13:58:59"))
    await pilot.tick()
    assert worker.talk is None
    move_to(clock, at("13:59"))
    await pilot.tick()
    assert worker.talk is not None and worker.talk.id == "A"
    assert (await db.get_talk("A")).actual_start == at("13:59")

    move_to(clock, at("14:59:55"))
    await pilot.tick()
    assert worker.talk.id == "A" and worker.starts().count("A") == 1

    move_to(clock, at("15:00"))
    await pilot.tick()
    assert worker.talk is None
    stored = await db.get_talk("A")
    assert (stored.status, stored.actual_end) == ("done", at("15:00"))

    move_to(clock, at("15:00:05"))
    await pilot.tick()  # still over: not opened again
    assert worker.talk is None and worker.starts().count("A") == 1


async def test_back_to_back_talks_close_the_first_before_opening_the_next(db) -> None:  # 9.1
    talks = [_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("13:59"))
    worker = workers["r1"]

    await pilot.tick()
    move_to(clock, at("14:59"))
    await pilot.tick()

    assert [c[:2] for c in worker.calls] == [("start", "A"), ("end", "A"), ("start", "B")]
    a, b = await db.get_talk("A"), await db.get_talk("B")
    assert a.status == "done" and a.actual_end == at("14:59")
    assert b.status == "live" and b.actual_start == at("14:59")
    assert a.actual_end <= b.actual_start

    move_to(clock, at("16:00"))
    await pilot.tick()
    assert worker.talk is None and (await db.get_talk("B")).actual_end == at("16:00")


async def test_a_talk_whose_source_ended_is_not_reopened_in_its_slot(db) -> None:
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")], at_time=at("13:59"))
    worker = workers["r1"]
    await pilot.tick()
    move_to(clock, at("14:30"))
    await worker.stop()  # e.g. the file ended by itself

    move_to(clock, at("14:31"))
    await pilot.tick()

    assert worker.talk is None and worker.starts() == ["A"]


# ------------------------------------------------------------ who's in charge (Ruling 33)


async def test_a_room_without_talks_today_keeps_its_free_session(db) -> None:
    tomorrow = _talk("T", "10:00", "11:00", day=DAY + timedelta(days=1))
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[tomorrow], at_time=at("12:00"))
    worker = workers["r1"]
    await worker.start(None)  # the free session, which the DB also lists for today
    free = worker.talk.id
    calls = list(worker.calls)

    await pilot.tick()

    assert worker.talk.id == free and worker.calls == calls
    assert not await pilot.in_charge("r1")


async def test_today_is_the_event_timezone(db) -> None:
    # 22:00 in Buenos Aires is 01:00 UTC of the next day.
    late = _talk("L", "22:00", "23:00")
    for tz, in_charge in ((TZ, True), ("UTC", False)):
        clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[late], at_time=at("20:30"), tz=tz)
        await workers["r1"].start(None)
        await pilot.tick()
        assert (workers["r1"].talk is None) is in_charge, tz
        assert await pilot.in_charge("r1") is in_charge


async def test_a_manual_room_is_never_touched(db) -> None:
    clock, workers, pilot, _ = await _setup(
        db, _room("r1", mode="manual"), talks=[_talk("A", "14:00", "15:00")], at_time=at("14:10")
    )

    await pilot.tick()

    assert workers["r1"].calls == []
    assert not await pilot.in_charge("r1")


async def test_one_room_that_cannot_start_does_not_stop_the_others(db, caplog: pytest.LogCaptureFixture) -> None:
    talks = [_talk("A", "14:00", "15:00", "r1"), _talk("B", "14:00", "15:00", "r2")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), _room("r2"), talks=talks, at_time=at("14:00"))
    workers["r1"].has_source = False

    await pilot.tick()
    await pilot.tick()

    assert workers["r2"].talk.id == "B"
    assert "no audio source" in caplog.text
    errors = [e for e in await db.recent_events(20) if e.type == "autopilot_error"]
    assert len(errors) == 1 and errors[0].room_id == "r1"  # logged once, not every tick


# ---------------------------------------------------------------- manual (9.2, 9.3)


async def test_manual_keeps_a_talk_past_its_end_and_start_talk_switches(db) -> None:  # 9.2
    talks = [_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00"), _talk("C", "16:00", "17:00")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("13:59"))
    worker = workers["r1"]
    await pilot.tick()
    move_to(clock, at("14:30"))
    await pilot.set_mode("r1", "manual")

    for hm in ("15:00", "15:05", "15:11"):
        move_to(clock, at(hm))
        await pilot.tick()
    assert worker.talk.id == "A"

    move_to(clock, at("15:12"))
    await pilot.start_talk("r1", "C")

    a = await db.get_talk("A")
    assert (a.status, a.actual_end) == ("done", at("15:12"))
    assert worker.talk.id == "C" and (await db.get_talk("C")).status == "live"
    assert pilot.mode("r1") == "manual"


async def test_back_to_auto_follows_the_clock_again(db) -> None:  # 9.3
    talks = [_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00"), _talk("C", "16:00", "17:00")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("15:12"))
    worker = workers["r1"]
    await pilot.start_talk("r1", "C")  # the operator jumps ahead: manual

    move_to(clock, at("15:20"))
    await pilot.set_mode("r1", "auto")
    await pilot.tick()
    assert worker.talk.id == "B"  # B's slot: C closes, B opens
    assert (await db.get_talk("C")).actual_end == at("15:20")

    move_to(clock, at("15:59"))
    await pilot.tick()
    assert worker.talk.id == "C"  # ended before its slot, so it opens in it
    assert (await db.get_talk("C")).actual_end is None

    move_to(clock, at("17:00"))
    await pilot.tick()
    assert worker.talk is None


async def test_back_to_auto_keeps_the_next_talk_the_operator_opened_early(db) -> None:  # Ruling 44
    talks = [_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00")]
    clock, workers, pilot, events = await _setup(db, _room("r1"), talks=talks, at_time=at("13:59"))
    worker = workers["r1"]
    await pilot.tick()  # A, by the autopilot
    move_to(clock, at("14:45"))
    await pilot.start_talk("r1", "B")  # the speaker is ready early
    sub = events.subscribe()

    move_to(clock, at("14:46"))
    await pilot.set_mode("r1", "auto")
    await pilot.tick()
    for hm in ("14:59", "15:00", "15:30"):
        move_to(clock, at(hm))
        await pilot.tick()

    assert worker.talk is not None and worker.talk.id == "B"
    assert worker.starts() == ["A", "B"]  # B started once, at 14:45
    assert (await db.get_talk("B")).actual_start == at("14:45")
    assert sub.get_nowait().kind == "room_mode"
    assert sub.empty()  # no second talk_started

    move_to(clock, at("16:00"))
    await pilot.tick()
    assert worker.talk is None


async def test_back_to_auto_stops_an_agenda_talk_that_is_not_the_next_one(db) -> None:  # Ruling 44
    talks = [_talk("B", "15:00", "16:00"), _talk("C", "16:00", "17:00"),
             _talk("T", "10:00", "11:00", day=DAY + timedelta(days=1))]
    for opened_early in ("C", "T"):  # a later talk today, tomorrow's rehearsal
        clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("14:30"))
        await pilot.start_talk("r1", opened_early)
        move_to(clock, at("14:31"))
        await pilot.set_mode("r1", "auto")
        await pilot.tick()
        assert workers["r1"].talk is None, opened_early
        await db.update_talk(opened_early, status="scheduled", actual_start=None, actual_end=None)


async def test_back_to_auto_stops_tomorrows_talk_even_when_it_is_the_next_one(db) -> None:  # Ruling 44, 6C
    """The "starts today" half of _early_next, left untested by the
    "not the next one" test above (there, C and T both fail the earlier
    "is upcoming[0]" check on their own, so the date check is never
    reached). Here, C's slot has already ended, so tomorrow's T really is
    the room's only upcoming talk (upcoming[0]) once opened -- and it must
    still be stopped, since it does not start today."""
    talks = [_talk("C", "16:00", "17:00"), _talk("T", "10:00", "11:00", day=DAY + timedelta(days=1))]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("16:00"))
    await pilot.tick()  # C, by the autopilot
    move_to(clock, at("17:00"))
    await pilot.tick()  # C's slot ends: idle
    assert workers["r1"].talk is None

    move_to(clock, at("17:30"))
    await pilot.start_talk("r1", "T")  # tomorrow's talk, rehearsed early
    move_to(clock, at("17:31"))
    await pilot.set_mode("r1", "auto")
    await pilot.tick()

    assert workers["r1"].talk is None


async def test_start_talk_on_the_running_talk_only_switches_to_manual(db) -> None:
    clock, workers, pilot, events = await _setup(
        db, _room("r1"), talks=[_talk("A", "14:00", "15:00")], at_time=at("13:59")
    )
    await pilot.tick()
    sub = events.subscribe()

    move_to(clock, at("14:10"))
    await pilot.start_talk("r1", "A")

    assert workers["r1"].starts() == ["A"]  # no restart of the engine
    assert pilot.mode("r1") == "manual"
    assert sub.get_nowait().kind == "room_mode" and sub.empty()


async def test_a_slot_that_crosses_midnight_in_the_event_timezone(db) -> None:
    late = _talk("L", "23:30", "23:59")
    late.end = at("00:30", DAY + timedelta(days=1))
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[late], at_time=at("23:29"))
    worker = workers["r1"]

    await pilot.tick()
    assert worker.talk is not None and worker.talk.id == "L"

    move_to(clock, at("00:10", DAY + timedelta(days=1)))  # a new day, with no talks of its own
    await pilot.tick()
    assert worker.talk is not None and worker.talk.id == "L" and worker.starts() == ["L"]

    move_to(clock, at("00:30", DAY + timedelta(days=1)))
    await pilot.tick()
    assert worker.talk is None
    assert (await db.get_talk("L")).actual_end == at("00:30", DAY + timedelta(days=1))


async def test_manual_actions_switch_the_room_to_manual_and_persist_it(db) -> None:  # Ruling 34
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")], at_time=at("14:10"))

    await pilot.start_talk("r1", "A")
    assert pilot.mode("r1") == "manual"
    assert (await db.get_rooms())[0].mode == "manual"
    assert workers["r1"].room.mode == "manual"

    await pilot.set_mode("r1", "auto")
    await pilot.end_talk("r1")
    assert pilot.mode("r1") == "manual"
    assert workers["r1"].talk is None
    assert (await db.get_talk("A")).actual_end == at("14:10")


async def test_start_talk_refuses_what_is_not_this_rooms_agenda(db) -> None:
    talks = [_talk("A", "14:00", "15:00", "r1"), _talk("B", "14:00", "15:00", "r2")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), _room("r2"), talks=talks, at_time=at("14:10"))
    await workers["r1"].start(None)
    free = workers["r1"].talk.id

    with pytest.raises(LookupError):
        await pilot.start_talk("r1", "missing")
    with pytest.raises(ValueError):
        await pilot.start_talk("r1", "B")  # another room's
    with pytest.raises(ValueError):
        await pilot.start_talk("r1", free)
    with pytest.raises(KeyError):
        await pilot.start_talk("nope", "A")
    with pytest.raises(ValueError):
        await pilot.set_mode("r1", "sideways")


# --------------------------------------------------------------------- reconnect (9.4)


async def test_reconnect_asks_the_worker_and_keeps_the_mode(db) -> None:  # 9.4
    clock, workers, pilot, _ = await _setup(db, _room("r1"), at_time=at("14:10"))

    await pilot.reconnect("r1")

    assert workers["r1"].calls == [("reconnect", "manual")]
    assert pilot.mode("r1") == "auto"


# ------------------------------------------------------------------ restart (Task 12)


async def test_restart_reopens_the_running_talk_and_keeps_the_mode(db) -> None:
    clock, workers, pilot, events = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")],
                                                 at_time=at("14:10"))
    await pilot.tick()
    worker = workers["r1"]
    assert worker.starts() == ["A"]
    sub = events.subscribe()

    talk = await pilot.restart("r1")

    assert talk.id == "A" and worker.starts() == ["A", "A"]  # the same talk again: no end, no free session
    assert not [c for c in worker.calls if c[0] == "end"]
    assert pilot.mode("r1") == "auto"
    event = sub.get_nowait()
    assert event.kind == "room_restart" and event.data == {"room_id": "r1", "talk_id": "A"}
    logged = [e for e in await db.recent_events(10) if e.type == "restart"]
    assert logged and logged[0].message.startswith("A:")


async def test_restart_reads_the_talk_under_the_room_lock(db) -> None:
    # A tick (or an operator action) that holds the room's lock changes the
    # talk; restart must see the talk as it is once it gets the lock.
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")],
                                            at_time=at("14:10"))
    await pilot.tick()
    worker = workers["r1"]
    lock = pilot._lock("r1")
    await lock.acquire()
    restarting = asyncio.create_task(pilot.restart("r1"))
    await asyncio.sleep(0)
    await worker.stop()  # the holder ends the talk meanwhile
    lock.release()

    with pytest.raises(NoTalkToRestart):
        await restarting
    assert worker.starts() == ["A"]


async def test_restart_needs_a_talk_and_a_known_room(db) -> None:
    clock, workers, pilot, _ = await _setup(db, _room("r1"), at_time=at("14:10"))

    with pytest.raises(NoTalkToRestart):
        await pilot.restart("r1")
    with pytest.raises(KeyError):
        await pilot.restart("nope")


async def test_a_failure_inside_the_restart_is_not_no_talk(db) -> None:
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=[_talk("A", "14:00", "15:00")],
                                            at_time=at("14:10"))
    await pilot.tick()

    async def broken(talk=None):
        raise KeyError("something inside start")

    workers["r1"].start = broken
    with pytest.raises(KeyError) as caught:
        await pilot.restart("r1")
    assert not isinstance(caught.value, NoTalkToRestart)


# ---------------------------------------------------------------- server restart (9.4b)


@pytest.mark.parametrize("left_as", ["live", "done"], ids=["crashed", "clean-shutdown"])
async def test_a_new_autopilot_on_the_same_db_resumes_the_talk_and_keeps_manual(db, left_as: str) -> None:  # 9.4b
    a, b = _talk("A", "14:00", "15:00", "r1"), _talk("B", "14:00", "15:00", "r2")
    for talk in (a, b):
        talk.status, talk.actual_start = left_as, at("13:59")
        talk.actual_end = at("14:30") if left_as == "done" else None
    await db.upsert_room(_room("r1", mode="auto"))
    await db.upsert_room(_room("r2", mode="manual"))
    await db.insert_talks([a, b])
    await db.update_talk("A", status=left_as, actual_start=a.actual_start, actual_end=a.actual_end)
    await db.update_talk("B", status=left_as, actual_start=b.actual_start, actual_end=b.actual_end)

    clock = FakeClock()
    move_to(clock, at("14:40"))
    # fresh workers, as after a restart; their Room still says "auto"
    workers = {rid: FakeWorker(_room(rid, mode="auto"), db, clock) for rid in ("r1", "r2")}
    pilot = Autopilot(db, workers, clock, lead_s=60, tz=TZ)

    await pilot.tick()

    assert workers["r1"].talk is not None and workers["r1"].talk.id == "A"
    resumed = await db.get_talk("A")
    assert (resumed.status, resumed.actual_start, resumed.actual_end) == ("live", at("13:59"), None)
    assert workers["r2"].calls == []
    assert pilot.mode("r2") == "manual" and workers["r2"].room.mode == "manual"


# ------------------------------------------------------------------ next talk, events


async def test_next_talk_skips_free_sessions_done_talks_and_the_current_one(db) -> None:
    talks = [_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00"), _talk("C", "16:00", "17:00")]
    clock, workers, pilot, _ = await _setup(db, _room("r1"), talks=talks, at_time=at("13:00"))
    assert (await pilot.next_talk("r1")).id == "A"

    move_to(clock, at("13:59"))
    await pilot.tick()
    assert (await pilot.next_talk("r1")).id == "B"

    await db.update_talk("B", status="done")
    assert (await pilot.next_talk("r1")).id == "C"

    move_to(clock, at("18:00"))
    assert await pilot.next_talk("r1") is None


async def test_mode_changes_and_talk_starts_are_published(db) -> None:
    clock, workers, pilot, events = await _setup(
        db, _room("r1"), talks=[_talk("A", "14:00", "15:00")], at_time=at("13:59")
    )
    sub = events.subscribe()

    await pilot.tick()
    await pilot.set_mode("r1", "manual")
    await pilot.reconnect("r1")

    got = [sub.get_nowait() for _ in range(3)]
    assert [(e.kind, e.data) for e in got] == [
        ("talk_started", {"room_id": "r1", "talk_id": "A", "title": "Talk A", "by": "autopilot"}),
        ("room_mode", {"room_id": "r1", "mode": "manual"}),
        ("room_reconnect", {"room_id": "r1"}),
    ]
    types = [e.type for e in await db.recent_events(10)]
    assert "mode" in types and "autopilot" in types


# ----------------------------------------------------------------------------- the loop


async def test_run_ticks_until_cancelled_and_survives_a_failing_tick(db, caplog: pytest.LogCaptureFixture) -> None:
    clock, workers, pilot, _ = await _setup(db, _room("r1"))
    ticks = 0

    async def flaky_tick(room_id: str | None = None) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("tick exploded")

    pilot.tick = flaky_tick  # type: ignore[method-assign]
    pilot._clock = RealClock()  # the loop really waits between ticks

    task = asyncio.create_task(pilot.run(interval_s=0.01), name="autopilot")
    for _ in range(200):
        if ticks >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ticks >= 3
    assert "tick exploded" in caplog.text


async def test_cancelling_the_loop_lets_a_tick_in_progress_finish(db) -> None:
    clock, workers, pilot, _ = await _setup(db, _room("r1"))
    entered, release = asyncio.Event(), asyncio.Event()
    finished = []

    async def slow_tick(room_id: str | None = None) -> None:
        entered.set()
        await release.wait()
        finished.append(True)

    pilot.tick = slow_tick  # type: ignore[method-assign]
    pilot._clock = RealClock()
    task = asyncio.create_task(pilot.run(interval_s=0.001))
    await asyncio.wait_for(entered.wait(), 1)

    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()  # waiting for the tick, not cutting it halfway
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert finished == [True]


# ------------------------------------------------- integration: the real RoomWorker


class DrivenClock(FakeClock):
    """FakeClock whose sleep() waits until the test advances time (as in
    tests/test_room.py)."""

    def __init__(self) -> None:
        super().__init__()
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def advance(self, s: float) -> None:
        super().advance(s)
        now = self.now()
        due = [f for t, f in self._sleepers if t <= now + 1e-9]
        self._sleepers = [(t, f) for t, f in self._sleepers if t > now + 1e-9]
        for fut in due:
            if not fut.done():
                fut.set_result(None)

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now() + s, fut))
        await fut


class SilentIngest:
    def __init__(self, source_type, source_url, realtime, clock) -> None:
        self._clock = clock
        self.restarts = 0
        self.last_error: str | None = None

    async def chunks(self):
        t = 0.0
        while True:
            await self._clock.sleep(0.1)
            yield AudioChunk(pcm=bytes(3200), t=round(t, 1))
            t += 0.1


class QuietEngine:
    """An engine that connects and stays quiet until it is closed."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def send_audio(self, chunk) -> None:
        return None

    async def end_utterance(self) -> None:
        return None

    async def close(self) -> None:
        self._closed.set()

    async def events(self):
        await self._closed.wait()
        yield EngineEvent(kind="closed")


async def _settle() -> None:
    for _ in range(3):
        for _ in range(50):
            await asyncio.sleep(0)
        await asyncio.sleep(0.002)


async def test_the_autopilot_drives_a_real_room_worker(db) -> None:  # 9.1, integration
    clock = DrivenClock()
    settings = Settings(
        gemini_api_key="unused", admin_password="test-password", timezone=TZ,
        rooms=[RoomCfg(id="r1", name="Sala r1", source_type="file", source_url="fake://r1", default_targets=["es"])],
    )
    room = _room("r1")
    await db.upsert_room(room)
    await db.insert_talks([_talk("A", "14:00", "15:00"), _talk("B", "15:00", "16:00")])
    bus = CaptionBus(clock=clock)
    ended: list[str] = []

    async def on_talk_end(talk: Talk) -> None:
        ended.append(talk.id)

    worker = RoomWorker(
        room, settings, bus, db, clock, QuietEngine, ingest_factory=SilentIngest, realtime=False,
        on_talk_end=on_talk_end,
    )
    pilot = Autopilot(db, {"r1": worker}, clock, lead_s=60, tz=TZ)

    move_to(clock, at("13:59"))
    await pilot.tick()
    await _settle()
    assert worker.talk is not None and worker.talk.id == "A"
    assert bus.history("r1", "es", "A")[0].type == "talk"

    move_to(clock, at("14:59"))
    await _settle()
    await pilot.tick()
    await _settle()
    assert worker.talk.id == "B"

    move_to(clock, at("16:00"))
    await _settle()
    await pilot.tick()
    await worker.drain_hooks()

    assert worker.talk is None and worker.status().state == "idle"
    a, b = await db.get_talk("A"), await db.get_talk("B")
    assert (a.status, a.actual_start, a.actual_end) == ("done", at("13:59"), at("14:59"))
    assert (b.status, b.actual_start, b.actual_end) == ("done", at("14:59"), at("16:00"))
    assert ended == ["A", "B"]
