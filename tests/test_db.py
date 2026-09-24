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
