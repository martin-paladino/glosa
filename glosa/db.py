"""SQLite persistence (spec §7): rooms, the agenda (talks), caption segments,
the event log and API costs.

``init_db(path)`` creates the tables (idempotent) and returns a ``Database``.
Its public methods are coroutines: each runs its sqlite3 work in a worker
thread (``asyncio.to_thread``), so a slow disk never stalls the event loop
that carries every room's captions. One connection is shared behind a lock
(``check_same_thread=False``), in WAL mode so readers don't block the writer.

Storage conventions:
  - lists (speakers, targets, tags, default_targets) and the glossary are
    JSON text;
  - datetimes are ISO 8601 text *with* their UTC offset. ``get_talks(room,
    day)`` matches the date part of ``start`` as stored, so ``day`` is a date
    in the timezone the agenda was imported with (the event's);
  - ``segments.t_start``/``t_end`` are seconds since the start of the talk;
  - ``ts`` / ``created_at`` are UTC ISO 8601.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from glosa.models import GlossaryTerm, Room, Talk

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_url      TEXT,
    mode            TEXT NOT NULL,
    public_token    TEXT NOT NULL,
    default_targets TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS talks (
    id            TEXT PRIMARY KEY,
    room_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    speakers      TEXT NOT NULL,
    language      TEXT NOT NULL,
    targets       TEXT NOT NULL,
    engine        TEXT NOT NULL,
    "start"       TEXT NOT NULL,
    "end"         TEXT NOT NULL,
    abstract      TEXT NOT NULL,
    tags          TEXT NOT NULL,
    glossary_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    actual_start  TEXT,
    actual_end    TEXT
);
CREATE INDEX IF NOT EXISTS talks_room_start ON talks (room_id, "start");

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    talk_id    TEXT NOT NULL,
    room_id    TEXT NOT NULL,
    lang       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    version    TEXT NOT NULL,
    text       TEXT NOT NULL,
    t_start    REAL NOT NULL,
    t_end      REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_talk ON segments (talk_id, lang, version, t_start);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    room_id TEXT,
    level   TEXT NOT NULL,
    type    TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS costs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    room_id   TEXT,
    component TEXT NOT NULL,
    units     REAL NOT NULL,
    usd       REAL NOT NULL
);
"""

_TALK_COLUMNS = (
    "id", "room_id", "title", "speakers", "language", "targets", "engine", "start", "end",
    "abstract", "tags", "glossary_json", "status", "actual_start", "actual_end",
)
# Fields insert_talks() refreshes on an existing row. status / actual_start /
# actual_end are runtime state: importing the agenda again must not reset a
# talk that is live or done.
_AGENDA_COLUMNS = (
    "room_id", "title", "speakers", "language", "targets", "engine", "start", "end",
    "abstract", "tags", "glossary_json",
)
_TALK_FIELD_TO_COLUMN = {"glossary": "glossary_json"}
FREE_TALK_PREFIX = "free-"  # glosa.room.FREE_SESSION_PREFIX (db.py does not import the pipeline)


@dataclass
class AgendaWrite:
    """What ``Database.upsert_agenda`` did besides inserting and updating."""

    held: dict[str, str]  # id -> status: live/done talks left unchanged
    removed: list[tuple[str, str]]  # (id, title): scheduled talks the agenda no longer lists


@dataclass
class Segment:
    id: int
    talk_id: str
    room_id: str
    lang: str
    kind: str  # "source" | "translation"
    version: str  # "live" | "corrected"
    text: str
    t_start: float  # seconds since the start of the talk
    t_end: float
    created_at: str


@dataclass
class EventRecord:
    id: int
    ts: str
    room_id: str | None
    level: str
    type: str
    message: str


def init_db(path: str | Path) -> "Database":
    """Open (creating it and its parent directory if needed) the database at
    ``path`` and make sure every table exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
            self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------ plumbing

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def call() -> T:
            with self._lock:
                return fn(self._con)

        return await asyncio.to_thread(call)

    # ------------------------------------------------------------ rooms

    async def upsert_room(self, room: Room) -> None:
        row = asdict(room)
        row["default_targets"] = json.dumps(room.default_targets)
        cols = list(row)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        sql = (
            f"INSERT INTO rooms ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}"
        )
        await self._run(lambda con: con.execute(sql, [row[c] for c in cols]))

    async def set_room_mode(self, room_id: str, mode: str) -> None:
        """Persist a room's autopilot mode ("auto" | "manual"); a room that
        is not stored is left alone."""
        if mode not in ("auto", "manual"):
            raise ValueError(f"unknown room mode: {mode!r}")
        await self._run(lambda con: con.execute("UPDATE rooms SET mode = ? WHERE id = ?", (mode, room_id)))

    async def get_rooms(self) -> list[Room]:
        rows = await self._run(lambda con: con.execute("SELECT * FROM rooms ORDER BY rowid").fetchall())
        return [
            Room(
                id=r["id"],
                slug=r["slug"],
                name=r["name"],
                source_type=r["source_type"],
                source_url=r["source_url"],
                mode=r["mode"],
                public_token=r["public_token"],
                default_targets=json.loads(r["default_targets"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------ talks

    async def insert_talks(self, talks: list[Talk]) -> None:
        """Insert talks; for ids already stored, refresh the agenda fields
        and keep ``status`` / ``actual_start`` / ``actual_end``."""
        cols = ", ".join(_q(c) for c in _TALK_COLUMNS)
        marks = ", ".join("?" * len(_TALK_COLUMNS))
        updates = ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in _AGENDA_COLUMNS)
        sql = f"INSERT INTO talks ({cols}) VALUES ({marks}) ON CONFLICT (id) DO UPDATE SET {updates}"
        rows = [[_talk_row(t)[c] for c in _TALK_COLUMNS] for t in talks]

        def write(con: sqlite3.Connection) -> None:
            con.execute("BEGIN")
            try:
                con.executemany(sql, rows)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

        await self._run(write)

    async def upsert_agenda(self, talks: list[Talk]) -> AgendaWrite:
        """An agenda import (Ruling 45), in one transaction:

          - insert new talks and refresh the agenda fields of stored talks
            that are still ``scheduled``; a ``live`` or ``done`` talk is left
            exactly as it is (fields and ``actual_*``): ``held``;
          - for every (room, day) the import covers (``day`` as stored, the
            date part of ``start``: the admin API stores it in the event's
            timezone), delete the ``scheduled`` talks it no longer lists (a
            talk cancelled upstream, or one whose id changed because its
            title or time was fixed): ``removed``. Live, done and free
            session rows are never deleted.
        """
        if not talks:
            return AgendaWrite(held={}, removed=[])
        cols = ", ".join(_q(c) for c in _TALK_COLUMNS)
        marks = ", ".join("?" * len(_TALK_COLUMNS))
        updates = ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in _AGENDA_COLUMNS)
        sql = (
            f"INSERT INTO talks ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates} WHERE talks.status = 'scheduled'"
        )
        rows = [_talk_row(t) for t in talks]
        values = [[row[c] for c in _TALK_COLUMNS] for row in rows]
        ids = [t.id for t in talks]
        covered = sorted({(row["room_id"], row["start"][:10]) for row in rows})
        scheduled_of_day = (
            'SELECT id, title FROM talks WHERE room_id = ? AND substr("start", 1, 10) = ? '
            "AND status = 'scheduled' ORDER BY \"start\", id"
        )

        def write(con: sqlite3.Connection) -> AgendaWrite:
            con.execute("BEGIN")
            try:
                held: dict[str, str] = {}
                for i in range(0, len(ids), 500):  # below SQLite's host-parameter limit
                    chunk = ids[i : i + 500]
                    marks = ", ".join("?" * len(chunk))
                    query = f"SELECT id, status FROM talks WHERE status != 'scheduled' AND id IN ({marks})"
                    held |= {row["id"]: row["status"] for row in con.execute(query, chunk)}
                listed = set(ids)
                removed = [
                    (row["id"], row["title"])
                    for room_id, day in covered
                    for row in con.execute(scheduled_of_day, (room_id, day)).fetchall()
                    if row["id"] not in listed and not row["id"].startswith(FREE_TALK_PREFIX)
                ]
                con.executemany("DELETE FROM talks WHERE id = ? AND status = 'scheduled'", [(r[0],) for r in removed])
                con.executemany(sql, values)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
            return AgendaWrite(held=held, removed=removed)

        return await self._run(write)

    async def delete_scheduled_talk(self, talk_id: str) -> bool:
        """Delete a talk that is still ``scheduled``; False if there is no
        such talk or it is live or done (those are never deleted)."""
        sql = "DELETE FROM talks WHERE id = ? AND status = 'scheduled'"
        cursor = await self._run(lambda con: con.execute(sql, (talk_id,)))
        return cursor.rowcount > 0

    async def get_live_talks(self) -> list[Talk]:
        """Every talk marked ``live``, in any room."""
        sql = "SELECT * FROM talks WHERE status = 'live' ORDER BY room_id, \"start\", id"
        rows = await self._run(lambda con: con.execute(sql).fetchall())
        return [_talk_from_row(r) for r in rows]

    async def get_last_started_talk(self, room_id: str) -> Talk | None:
        """The room's talk (free sessions included) with the latest
        ``actual_start``, or None if none ever started."""
        sql = "SELECT * FROM talks WHERE room_id = ? AND actual_start IS NOT NULL ORDER BY actual_start DESC LIMIT 1"
        row = await self._run(lambda con: con.execute(sql, (room_id,)).fetchone())
        return _talk_from_row(row) if row is not None else None

    async def get_talk(self, talk_id: str) -> Talk | None:
        row = await self._run(lambda con: con.execute("SELECT * FROM talks WHERE id = ?", (talk_id,)).fetchone())
        return _talk_from_row(row) if row is not None else None

    async def get_talks(self, room_id: str, day: date) -> list[Talk]:
        """The room's talks starting on ``day`` (see the module docstring for
        the timezone), in start order."""
        sql = 'SELECT * FROM talks WHERE room_id = ? AND substr("start", 1, 10) = ? ORDER BY "start", id'
        rows = await self._run(lambda con: con.execute(sql, (room_id, day.isoformat())).fetchall())
        return [_talk_from_row(r) for r in rows]

    async def update_talk(self, talk_id: str, **fields: Any) -> None:
        """Change some fields of a talk (Talk field names, e.g. status=...,
        actual_start=...). Raises ValueError for an unknown field and KeyError
        if there is no such talk."""
        allowed = set(Talk.__dataclass_fields__) - {"id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown talk fields: {sorted(unknown)}")
        if not fields:
            return
        values = {_TALK_FIELD_TO_COLUMN.get(k, k): _to_db(k, v) for k, v in fields.items()}
        sets = ", ".join(f"{_q(c)} = ?" for c in values)
        sql = f"UPDATE talks SET {sets} WHERE id = ?"
        cursor = await self._run(lambda con: con.execute(sql, [*values.values(), talk_id]))
        if cursor.rowcount == 0:
            raise KeyError(talk_id)

    # ------------------------------------------------------------ segments

    async def save_segment(
        self,
        talk_id: str,
        room_id: str,
        lang: str,
        kind: str,
        version: str,
        text: str,
        t_start: float,
        t_end: float,
    ) -> int:
        sql = (
            "INSERT INTO segments (talk_id, room_id, lang, kind, version, text, t_start, t_end, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        args = (talk_id, room_id, lang, kind, version, text, t_start, t_end, _now_iso())
        cursor = await self._run(lambda con: con.execute(sql, args))
        return int(cursor.lastrowid or 0)

    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Segment]:
        sql = "SELECT * FROM segments WHERE talk_id = ? AND lang = ? AND version = ? ORDER BY t_start, id"
        rows = await self._run(lambda con: con.execute(sql, (talk_id, lang, version)).fetchall())
        return [Segment(**dict(r)) for r in rows]

    # ------------------------------------------------------------ events

    async def log_event(self, room_id: str | None, level: str, type: str, message: str) -> None:
        sql = "INSERT INTO events (ts, room_id, level, type, message) VALUES (?, ?, ?, ?, ?)"
        await self._run(lambda con: con.execute(sql, (_now_iso(), room_id, level, type, message)))

    async def recent_events(self, n: int) -> list[EventRecord]:
        """The last ``n`` events, newest first."""
        sql = "SELECT * FROM events ORDER BY id DESC LIMIT ?"
        rows = await self._run(lambda con: con.execute(sql, (n,)).fetchall())
        return [EventRecord(**dict(r)) for r in rows]

    async def events_after(self, after_id: int, limit: int) -> list[EventRecord]:
        """Up to ``limit`` events logged after event ``after_id``, oldest first
        (the admin stream's new events and its Last-Event-ID resume)."""
        sql = "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?"
        rows = await self._run(lambda con: con.execute(sql, (after_id, limit)).fetchall())
        return [EventRecord(**dict(r)) for r in rows]

    # ------------------------------------------------------------ costs

    async def add_cost(self, room_id: str | None, component: str, units: float, usd: float) -> None:
        sql = "INSERT INTO costs (ts, room_id, component, units, usd) VALUES (?, ?, ?, ?, ?)"
        await self._run(lambda con: con.execute(sql, (_now_iso(), room_id, component, units, usd)))

    async def total_cost(self) -> float:
        row = await self._run(lambda con: con.execute("SELECT COALESCE(SUM(usd), 0.0) FROM costs").fetchone())
        return float(row[0])

    async def cost_by_room(self) -> dict[str | None, float]:
        """Every room's spend (``None``: costs of no room), all time."""
        sql = "SELECT room_id, SUM(usd) FROM costs GROUP BY room_id"
        rows = await self._run(lambda con: con.execute(sql).fetchall())
        return {row[0]: float(row[1]) for row in rows}


# ---------------------------------------------------------------- helpers


def _q(column: str) -> str:
    return f'"{column}"'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_db(field: str, value: Any) -> Any:
    if field == "glossary":
        return json.dumps([asdict(term) for term in value])
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return json.dumps(value)
    return value


def _talk_row(talk: Talk) -> dict[str, Any]:
    row = {_TALK_FIELD_TO_COLUMN.get(k, k): _to_db(k, v) for k, v in vars(talk).items()}
    return row


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _talk_from_row(row: sqlite3.Row) -> Talk:
    return Talk(
        id=row["id"],
        room_id=row["room_id"],
        title=row["title"],
        speakers=json.loads(row["speakers"]),
        language=row["language"],
        targets=json.loads(row["targets"]),
        engine=row["engine"],
        start=datetime.fromisoformat(row["start"]),
        end=datetime.fromisoformat(row["end"]),
        abstract=row["abstract"],
        tags=json.loads(row["tags"]),
        glossary=[GlossaryTerm(**term) for term in json.loads(row["glossary_json"])],
        status=row["status"],
        actual_start=_dt(row["actual_start"]),
        actual_end=_dt(row["actual_end"]),
    )
