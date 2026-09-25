"""SQLite persistence (glosa.db): schema of spec §7 and the repository used by
RoomWorker, the agenda and the admin panel."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from glosa.db import init_db
from glosa.models import GlossaryTerm, Room, Talk

ART = timezone(timedelta(hours=-3))


def _talk(talk_id: str = "t1", room_id: str = "r1", start_h: int = 14, day: int = 25) -> Talk:
    start = datetime(2026, 9, day, start_h, 0, tzinfo=ART)
    return Talk(
        id=talk_id,
        room_id=room_id,
        title="What your Kubernetes control plane really costs",
        speakers=["Priya Raman", "Diego Ferreyra"],
        language="en",
        targets=["es"],
        engine="fast",
        start=start,
        end=start + timedelta(minutes=45),
        abstract="Costs, measured.",
        tags=["k8s", "finops"],
        glossary=[
            GlossaryTerm(term="control plane", keep_in_english=True),
            GlossaryTerm(term="bill", keep_in_english=False, translation="factura"),
        ],
        status="scheduled",
        actual_start=None,
        actual_end=None,
    )


@pytest.fixture
def db(tmp_path: Path):
    database = init_db(tmp_path / "data" / "glosa.db")
    yield database
    database.close()


def test_init_db_creates_the_spec_tables_and_columns(tmp_path: Path) -> None:
    path = tmp_path / "glosa.db"
    init_db(path).close()

    con = sqlite3.connect(path)
    try:
        def columns(table: str) -> set[str]:
            return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}

        assert {"id", "slug", "name", "source_type", "source_url", "mode", "public_token"} <= columns("rooms")
        assert {
            "id", "room_id", "title", "speakers", "language", "targets", "engine", "start", "end",
            "abstract", "tags", "glossary_json", "status", "actual_start", "actual_end",
        } <= columns("talks")
        assert {
            "id", "talk_id", "room_id", "lang", "kind", "version", "text", "t_start", "t_end", "created_at",
        } <= columns("segments")
        assert {"id", "ts", "room_id", "level", "type", "message"} <= columns("events")
        assert {"id", "ts", "room_id", "component", "units", "usd"} <= columns("costs")
    finally:
        con.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "glosa.db"
    init_db(path).close()
    init_db(path).close()  # tables already exist: no error


async def test_talk_round_trip(db) -> None:  # 5.1
    talk = _talk()
    other_day = _talk("t2", day=26)
    other_room = _talk("t3", room_id="r2")
    await db.insert_talks([talk, other_day, other_room])

    assert await db.get_talks("r1", date(2026, 9, 25)) == [talk]
    assert await db.get_talk("t1") == talk
    assert await db.get_talk("missing") is None


async def test_get_talks_orders_by_start(db) -> None:
    late, early = _talk("late", start_h=16), _talk("early", start_h=10)
    await db.insert_talks([late, early])

    assert [t.id for t in await db.get_talks("r1", date(2026, 9, 25))] == ["early", "late"]


async def test_update_talk_changes_only_the_given_fields(db) -> None:
    talk = _talk()
    await db.insert_talks([talk])
    began = datetime(2026, 9, 25, 17, 1, tzinfo=timezone.utc)

    await db.update_talk("t1", status="live", actual_start=began, targets=["es", "pt"])

    got = await db.get_talk("t1")
    assert got is not None
    assert (got.status, got.actual_start, got.targets) == ("live", began, ["es", "pt"])
    assert got.title == talk.title and got.glossary == talk.glossary


async def test_update_talk_rejects_unknown_fields_and_missing_talks(db) -> None:
    await db.insert_talks([_talk()])
    with pytest.raises(ValueError):
        await db.update_talk("t1", colour="red")
    with pytest.raises(KeyError):
        await db.update_talk("nope", status="live")


async def test_insert_talks_again_updates_the_agenda_but_keeps_runtime_state(db) -> None:
    talk = _talk()
    await db.insert_talks([talk])
    began = datetime(2026, 9, 25, 17, 2, tzinfo=timezone.utc)
    await db.update_talk("t1", status="live", actual_start=began)

    renamed = _talk()
    renamed.title = "A new title"
    await db.insert_talks([renamed])  # e.g. the agenda imported again

    got = await db.get_talk("t1")
    assert got is not None
    assert (got.title, got.status, got.actual_start) == ("A new title", "live", began)


async def test_segments_round_trip(db) -> None:  # 5.1
    await db.save_segment("t1", "r1", "es", "translation", "live", "Un gran escenario.", 5.0, 6.5)
    await db.save_segment("t1", "r1", "es", "translation", "live", "Sin duda.", 7.0, 8.25)
    await db.save_segment("t1", "r1", "en", "source", "live", "Great starting scenario.", 4.9, 6.1)
    await db.save_segment("t1", "r1", "es", "translation", "corrected", "Un gran escenario inicial.", 5.0, 6.5)
    await db.save_segment("t2", "r1", "es", "translation", "live", "Otra charla.", 1.0, 2.0)

    live_es = await db.get_segments("t1", "es", "live")

    assert [(s.text, s.t_start, s.t_end) for s in live_es] == [
        ("Un gran escenario.", 5.0, 6.5),
        ("Sin duda.", 7.0, 8.25),
    ]
    assert {(s.talk_id, s.room_id, s.lang, s.kind, s.version) for s in live_es} == {
        ("t1", "r1", "es", "translation", "live")
    }
    assert all(s.created_at for s in live_es)
    assert [s.text for s in await db.get_segments("t1", "en", "live")] == ["Great starting scenario."]
    assert [s.text for s in await db.get_segments("t1", "es", "corrected")] == ["Un gran escenario inicial."]


async def test_segments_come_back_in_time_order(db) -> None:
    await db.save_segment("t1", "r1", "es", "translation", "live", "segundo", 9.0, 10.0)
    await db.save_segment("t1", "r1", "es", "translation", "live", "primero", 3.0, 4.0)

    assert [s.text for s in await db.get_segments("t1", "es", "live")] == ["primero", "segundo"]


async def test_total_cost_adds_every_row(db) -> None:  # 5.1
    assert await db.total_cost() == 0.0
    await db.add_cost("r1", "live_translate", 1.5, 0.0552)
    await db.add_cost("r2", "live_translate", 0.5, 0.0184)
    await db.add_cost("r1", "flash_lite", 1200, 0.0031)

    assert await db.total_cost() == pytest.approx(0.0552 + 0.0184 + 0.0031)


async def test_recent_events_newest_first(db) -> None:
    await db.log_event("r1", "info", "talk_start", "first")
    await db.log_event("r2", "warning", "reconnect", "second")
    await db.log_event(None, "error", "startup", "third")

    events = await db.recent_events(2)

    assert [(e.room_id, e.level, e.type, e.message) for e in events] == [
        (None, "error", "startup", "third"),
        ("r2", "warning", "reconnect", "second"),
    ]
    assert all(e.ts for e in events)


async def test_room_upsert_round_trip(db) -> None:
    room = Room(
        id="r1", slug="r1", name="Gran sala", source_type="file", source_url="samples/en_clip.opus",
        mode="auto", public_token="tok-1", default_targets=["es"],
    )
    await db.upsert_room(room)
    room.name = "Sala principal"
    await db.upsert_room(room)

    assert await db.get_rooms() == [room]


async def test_data_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "glosa.db"
    first = init_db(path)
    await first.insert_talks([_talk()])
    await first.save_segment("t1", "r1", "es", "translation", "live", "Hola.", 1.0, 2.0)
    first.close()

    second = init_db(path)
    try:
        assert await second.get_talk("t1") == _talk()
        assert [s.text for s in await second.get_segments("t1", "es", "live")] == ["Hola."]
    finally:
        second.close()


async def test_upsert_agenda_updates_scheduled_talks_and_leaves_live_and_done_alone(db) -> None:
    scheduled, live, done = _talk("s"), _talk("l", start_h=10), _talk("d", start_h=8)
    await db.insert_talks([scheduled, live, done])
    began = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 9, 25, 11, 45, tzinfo=timezone.utc)
    await db.update_talk("l", status="live", actual_start=began)
    await db.update_talk("d", status="done", actual_start=ended - timedelta(minutes=45), actual_end=ended)

    again = [_talk("s"), _talk("l", start_h=10), _talk("d", start_h=8), _talk("new", start_h=18)]
    for talk in again:
        talk.title = "Renamed on re-import"
    result = await db.upsert_agenda(again)

    assert result.held == {"l": "live", "d": "done"}
    assert result.removed == []
    got = {t.id: t for t in await db.get_talks("r1", date(2026, 9, 25))}
    assert got["s"].title == "Renamed on re-import" and got["s"].status == "scheduled"
    assert got["new"].title == "Renamed on re-import"
    assert (got["l"].title, got["l"].status, got["l"].actual_start) == (live.title, "live", began)
    assert (got["d"].title, got["d"].status, got["d"].actual_end) == (done.title, "done", ended)


async def test_upsert_agenda_with_nothing_to_write(db) -> None:
    result = await db.upsert_agenda([])
    assert result.held == {} and result.removed == []


async def test_upsert_agenda_removes_the_scheduled_talks_a_covered_day_no_longer_lists(db) -> None:  # Ruling 45
    kept, dropped = _talk("kept", start_h=10), _talk("dropped", start_h=12)
    live, done = _talk("live", start_h=8), _talk("done", start_h=9)
    other_day, other_room = _talk("other-day", day=26), _talk("other-room", room_id="r2")
    await db.insert_talks([kept, dropped, live, done, other_day, other_room])
    await db.update_talk("live", status="live", actual_start=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc))
    await db.update_talk("done", status="done", actual_end=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
    free = _talk("free-r1-20260925T090000", start_h=9)  # a free session's row, whatever its status
    free.status = "scheduled"
    await db.insert_talks([free])

    result = await db.upsert_agenda([_talk("kept", start_h=10), _talk("new", start_h=16)])  # covers (r1, 25/9)

    assert result.removed == [("dropped", dropped.title)]
    ids = {t.id for t in await db.get_talks("r1", date(2026, 9, 25))}
    assert ids == {"kept", "new", "live", "done", "free-r1-20260925T090000"}
    assert await db.get_talk("other-day") is not None and await db.get_talk("other-room") is not None


async def test_upsert_agenda_keep_protects_a_row_from_removal_without_upserting_it(db) -> None:
    """task-11r-brief.md Ruling 6B: a row the admin API skipped this import
    (e.g. an invalid schedule) must not come back as ``removed`` just
    because it isn't in this call's talks -- the caller passes its id via
    ``keep`` instead."""
    kept, protected = _talk("kept", start_h=10), _talk("protected", start_h=12)
    await db.insert_talks([kept, protected])

    result = await db.upsert_agenda([_talk("kept", start_h=10)], keep={"protected"})

    assert result.removed == []
    assert (await db.get_talk("protected")).status == "scheduled"  # untouched, not deleted


async def test_upsert_agenda_removes_across_the_whole_imports_date_range_per_room(db) -> None:
    """task-11r-brief.md Ruling 6D: the scheduled talks an import drops are
    the ones in the date range the WHOLE import spans (min day to max day
    across every row, not just the exact days a given room's new rows fall
    on), for every room the import mentions. Otherwise a room's only talk of
    a day, moved to another day the import also covers (via a different
    room's row), never gets cleaned up."""
    old = _talk("old", room_id="r1", start_h=10, day=24)  # r1's only talk of the 24th
    await db.insert_talks([old])

    # A re-import moves r1's talk to the 25th; some other room (r2) still has
    # a row on the 24th, so the whole import's date range is 24..25 and r1's
    # stale "old" (day 24) falls inside it even though r1 has no new row there.
    result = await db.upsert_agenda(
        [_talk("moved", room_id="r1", start_h=10, day=25), _talk("anchor", room_id="r2", start_h=9, day=24)]
    )

    assert result.removed == [("old", old.title)]


async def test_delete_scheduled_talk_only_deletes_scheduled_talks(db) -> None:
    await db.insert_talks([_talk("s"), _talk("l", start_h=10)])
    await db.update_talk("l", status="live")

    assert await db.delete_scheduled_talk("s") is True
    assert await db.delete_scheduled_talk("l") is False
    assert await db.delete_scheduled_talk("missing") is False
    assert await db.get_talk("s") is None and await db.get_talk("l") is not None


async def test_live_talks_and_the_last_started_talk_of_a_room(db) -> None:
    first, second, third = _talk("a", start_h=9), _talk("b", start_h=11), _talk("c", start_h=13)
    elsewhere = _talk("x", room_id="r2")
    await db.insert_talks([first, second, third, elsewhere])
    await db.update_talk("a", status="live", actual_start=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
    await db.update_talk("b", status="done", actual_start=datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
    await db.update_talk("x", status="live", actual_start=datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc))

    assert sorted(t.id for t in await db.get_live_talks()) == ["a", "x"]
    assert (await db.get_last_started_talk("r1")).id == "b"  # c never started
    assert await db.get_last_started_talk("r9") is None


async def test_get_live_talk_of_a_room_ignores_a_done_talk_started_later(db) -> None:
    """task-11r-brief.md Ruling 6A (Ruling 46 fix): the reviewer's scenario
    -- A is reopened after B, so B's actual_start is the room's latest, but
    B is done and A is the one actually live. get_last_started_talk (above)
    would wrongly return B; get_live_talk is what _resume_manual_room
    (glosa/web/app.py) must use instead: WHERE status='live' ORDER BY
    actual_start DESC LIMIT 1, not "the last one started"."""
    a, b = _talk("a", start_h=9), _talk("b", start_h=11)
    await db.insert_talks([a, b])
    await db.update_talk("b", status="live", actual_start=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))
    await db.update_talk("b", status="done", actual_end=datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc))
    await db.update_talk("a", status="live", actual_start=datetime(2026, 9, 25, 10, 45, tzinfo=timezone.utc))
    # b's actual_start (10:00) predates a's (10:45), so on its own this
    # fixture wouldn't distinguish the two methods; reopen b once more with a
    # LATER actual_start than a's, while a stays live -- the exact ordering
    # bug the ruling calls out.
    await db.update_talk("b", status="done", actual_start=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc))

    assert (await db.get_last_started_talk("r1")).id == "b"  # latest actual_start, but done
    assert (await db.get_live_talk("r1")).id == "a"  # the room's actually-live talk
    assert await db.get_live_talk("r9") is None


async def test_set_room_mode_persists_only_the_mode(db) -> None:
    room = Room(
        id="r1", slug="r1", name="Gran sala", source_type="file", source_url=None,
        mode="auto", public_token="tok-1", default_targets=["es"],
    )
    await db.upsert_room(room)

    await db.set_room_mode("r1", "manual")
    await db.set_room_mode("missing", "manual")  # no such room: nothing to do

    (stored,) = await db.get_rooms()
    assert stored.mode == "manual" and stored.public_token == "tok-1"
    with pytest.raises(ValueError):
        await db.set_room_mode("r1", "sideways")


async def test_events_after_an_id_come_oldest_first_and_bounded(db) -> None:  # Task 12: the admin stream
    for n in range(5):
        await db.log_event("r1", "info", "tick", f"event {n}")
    first, second, *_ = sorted(await db.recent_events(5), key=lambda e: e.id)

    after = await db.events_after(second.id, limit=2)

    assert [e.message for e in after] == ["event 2", "event 3"]
    assert await db.events_after(first.id + 100, limit=10) == []


async def test_cost_by_room_sums_each_room(db) -> None:  # Task 12: the drawer's room cost
    assert await db.cost_by_room() == {}
    await db.add_cost("r1", "live_translate", 1.5, 0.05)
    await db.add_cost("r2", "live_translate", 0.5, 0.02)
    await db.add_cost("r1", "flash_lite", 1200, 0.01)
    await db.add_cost(None, "glossary", 1, 0.03)

    costs = await db.cost_by_room()

    assert costs == pytest.approx({"r1": 0.06, "r2": 0.02, None: 0.03})


async def test_pending_exports_lists_every_build_left_pending(db) -> None:  # final-review-A I4
    await db.set_export_status("t1", "es", "pending")
    await db.set_export_status("t1", "pt", "ready")
    await db.set_export_status("t2", "en", "failed")
    await db.set_export_status("t3", "es", "pending")
    await db.set_export_status("t3", "pt", "pending")

    assert await db.get_pending_exports() == [("t1", "es"), ("t3", "es"), ("t3", "pt")]
