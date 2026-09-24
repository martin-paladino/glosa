"""The autopilot in the app (Task 9): the room controls of the admin API
(mode, start-talk, end-talk, reconnect), Task 7's start/stop switching a room
to manual (Ruling 34), the boot sequence (the agenda's talk of the moment
before the free session, the stored mode before the default) and the
lifespan's tick loop.

The rooms run the real RoomWorker on real time, with a silent audio source
and an engine that stays quiet: no ffmpeg, no API. EventClock is RealClock
with its wall clock pinned to 2030-09-24 14:00 in Buenos Aires, so the
talks' times are fixed while every timer really waits.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from glosa.clock import RealClock
from glosa.config import RoomCfg, Settings
from glosa.db import init_db
from glosa.models import AudioChunk, EngineEvent, Room, Talk
from glosa.scheduler import Autopilot
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, sign_session

ADMIN_PASSWORD = "test-password"
CSRF = {"X-Glosa-Admin": "1"}
TZ = "America/Argentina/Buenos_Aires"
ART = ZoneInfo(TZ)
T0 = datetime(2030, 9, 24, 14, 0, tzinfo=ART)  # the wall clock when each app starts


class EventClock(RealClock):
    def __init__(self, wall_start: datetime = T0) -> None:
        super().__init__()
        self._wall_start = wall_start

    def wall(self) -> datetime:
        return self._wall_start + timedelta(seconds=self.now())


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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="unused",
        admin_password=ADMIN_PASSWORD,
        timezone=TZ,
        db_path=str(tmp_path / "glosa.db"),
        rooms=[
            RoomCfg(id="r1", name="Sala Uno", source_type="file", source_url="fake://r1", default_targets=["es"]),
            RoomCfg(id="r2", name="Sala Dos", source_type="file", source_url="fake://r2", language="es",
                    default_targets=["en"]),
        ],
    )


def _talk(talk_id: str, room_id: str, start_min: float, end_min: float) -> Talk:
    """A talk from T0 + start_min to T0 + end_min minutes."""
    return Talk(
        id=talk_id, room_id=room_id, title=f"Talk {talk_id}", speakers=["Ana"], language="en", targets=["es"],
        engine="fast", start=T0 + timedelta(minutes=start_min), end=T0 + timedelta(minutes=end_min),
        abstract="", tags=[], glossary=[], status="scheduled", actual_start=None, actual_end=None,
    )


@asynccontextmanager
async def _open(settings: Settings, **kwargs) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    kwargs.setdefault("autopilot_interval_s", 3600)  # tests tick by hand unless they say otherwise
    app = create_app(settings, clock=EventClock(), engine_factory=QuietEngine, ingest_factory=SilentIngest, **kwargs)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as client:
            client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
            yield app, client


async def _seed(settings: Settings, *talks: Talk, modes: dict[str, str] | None = None) -> None:
    """Write rooms (with their modes) and talks before the app starts."""
    db = init_db(settings.db_path)
    try:
        for cfg in settings.rooms:
            mode = (modes or {}).get(cfg.id, "auto")
            await db.upsert_room(Room(id=cfg.id, slug=cfg.id, name=cfg.name, source_type=cfg.source_type,
                                      source_url=cfg.source_url, mode=mode, public_token=f"tok-{cfg.id}",
                                      default_targets=list(cfg.default_targets)))
        await db.insert_talks(list(talks))
    finally:
        db.close()


def _is_free(talk: Talk | None) -> bool:
    return talk is not None and talk.id.startswith("free-")


# ---------------------------------------------------------------------------- mode


async def test_the_mode_endpoint_persists_across_a_restart(tmp_path: Path) -> None:  # 9.4b
    settings = _settings(tmp_path)
    async with _open(settings) as (app, client):
        response = await client.post("/api/admin/rooms/r1/mode", json={"mode": "manual"})
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "manual"
        assert response.json()["room"]["state"] in ("green", "yellow", "red")  # the free session goes on
        assert [r.mode for r in await app.state.db.get_rooms()] == ["manual", "auto"]

    async with _open(settings) as (app, client):
        assert app.state.autopilot.mode("r1") == "manual" and app.state.autopilot.mode("r2") == "auto"
        assert [r.mode for r in await app.state.db.get_rooms()] == ["manual", "auto"]  # boot kept it
        listed = (await client.get("/api/admin/rooms")).json()
        assert [(r["id"], r["mode"]) for r in listed] == [("r1", "manual"), ("r2", "auto")]


async def test_bad_modes_and_unknown_rooms(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (_, client):
        assert (await client.post("/api/admin/rooms/r1/mode", json={"mode": "sideways"})).status_code == 422
        assert (await client.post("/api/admin/rooms/nope/mode", json={"mode": "auto"})).status_code == 404
        for action in ("start-talk", "end-talk", "reconnect"):
            assert (await client.post(f"/api/admin/rooms/nope/{action}", json={"talk_id": "x"})).status_code == 404


async def test_back_to_auto_follows_the_agenda_at_once(tmp_path: Path) -> None:  # 9.3
    async with _open(_settings(tmp_path)) as (app, client):
        worker = app.state.workers["r1"]
        assert _is_free(worker.talk)  # no agenda at boot: the free session
        await app.state.db.insert_talks([_talk("now", "r1", -10, 50)])
        await client.post("/api/admin/rooms/r1/mode", json={"mode": "manual"})

        response = await client.post("/api/admin/rooms/r1/mode", json={"mode": "auto"})

        assert response.json()["mode"] == "auto"
        assert worker.talk is not None and worker.talk.id == "now"
        assert response.json()["room"]["talk_id"] == "now"


# ------------------------------------------------------------ start-talk / end-talk


async def test_start_talk_and_end_talk(tmp_path: Path) -> None:  # 9.2, Ruling 34
    settings = _settings(tmp_path)
    async with _open(settings) as (app, client):
        db, worker = app.state.db, app.state.workers["r1"]
        await db.insert_talks([_talk("later", "r1", 120, 160), _talk("other", "r2", 0, 40)])

        started = await client.post("/api/admin/rooms/r1/start-talk", json={"talk_id": "later"})

        assert started.status_code == 200, started.text
        assert started.json()["mode"] == "manual" and started.json()["room"]["talk_id"] == "later"
        assert worker.talk.id == "later" and (await db.get_talk("later")).status == "live"
        free = [e for e in await db.recent_events(20) if e.type == "talk_end"]
        assert free and free[0].message.startswith("free-r1-")  # the free session ended first

        ended = await client.post("/api/admin/rooms/r1/end-talk")

        assert ended.status_code == 200 and ended.json()["room"]["state"] == "idle"
        assert worker.talk is None
        assert (await db.get_talk("later")).status == "done"
        assert (await db.get_rooms())[0].mode == "manual"

        assert (await client.post("/api/admin/rooms/r1/start-talk", json={"talk_id": "missing"})).status_code == 404
        assert (await client.post("/api/admin/rooms/r1/start-talk", json={"talk_id": "other"})).status_code == 409
        assert (await client.post("/api/admin/rooms/r1/start-talk", json={})).status_code == 422


async def test_a_live_targets_edit_survives_a_restart_of_the_talk(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        await app.state.db.insert_talks([_talk("t", "r1", 0, 30)])
        await client.post("/api/admin/rooms/r1/start-talk", json={"talk_id": "t"})

        edited = await client.put("/api/admin/talks/t", json={"targets": ["en", "es"], "title": "Renamed"})
        assert edited.status_code == 200, edited.text
        worker = app.state.workers["r1"]
        assert worker.talk.title == "Renamed"  # at once, for the room list
        await worker.start(worker.talk)  # the same talk on a new pipeline

        assert (await app.state.db.get_talk("t")).targets == ["en", "es"]
        assert worker.talk.targets == ["en", "es"]


async def test_task7_start_and_stop_switch_the_room_to_manual(tmp_path: Path) -> None:  # Ruling 34
    async with _open(_settings(tmp_path)) as (app, client):
        stopped = await client.post("/api/admin/rooms/r1/stop")
        assert stopped.status_code == 200 and stopped.json()["mode"] == "manual"
        assert app.state.autopilot.mode("r1") == "manual"

        await client.post("/api/admin/rooms/r1/mode", json={"mode": "auto"})
        started = await client.post("/api/admin/rooms/r1/start")
        assert started.status_code == 200 and started.json()["mode"] == "manual"
        assert [r.mode for r in await app.state.db.get_rooms()] == ["manual", "auto"]


async def test_reconnect_reaches_the_worker_and_keeps_the_mode(tmp_path: Path) -> None:  # 9.4
    async with _open(_settings(tmp_path)) as (app, client):
        worker = app.state.workers["r1"]
        reasons: list[str] = []
        real = worker.reconnect

        async def spy(reason: str) -> None:
            reasons.append(reason)
            await real(reason)

        worker.reconnect = spy

        response = await client.post("/api/admin/rooms/r1/reconnect")

        assert response.status_code == 200 and response.json()["mode"] == "auto"
        assert reasons == ["manual"]


# ---------------------------------------------------------------------------- boot


async def test_boot_resumes_the_talk_of_the_moment_instead_of_the_free_session(tmp_path: Path) -> None:  # 9.4b
    settings = _settings(tmp_path)
    crashed = _talk("mid", "r1", -20, 40)
    crashed.status, crashed.actual_start = "live", T0 - timedelta(minutes=19)
    await _seed(settings, crashed)
    db = init_db(settings.db_path)
    await db.update_talk("mid", status="live", actual_start=crashed.actual_start)
    db.close()

    async with _open(settings) as (app, _):
        r1, r2 = app.state.workers["r1"], app.state.workers["r2"]
        assert r1.talk is not None and r1.talk.id == "mid"
        assert r1.talk.actual_start == crashed.actual_start
        assert _is_free(r2.talk)  # no agenda today: its free session, as in Task 5
        free_rows = await app.state.db.get_talks("r1", T0.date())
        assert [t.id for t in free_rows] == ["mid"]  # r1 never ran a free session


async def test_boot_leaves_a_room_the_agenda_owns_idle_between_talks(tmp_path: Path) -> None:  # Ruling 33
    settings = _settings(tmp_path)
    await _seed(settings, _talk("later", "r1", 120, 160), _talk("m", "r2", -30, 30), modes={"r2": "manual"})

    async with _open(settings) as (app, _):
        assert app.state.workers["r1"].talk is None  # auto, talks today, none on now
        assert app.state.workers["r2"].talk is None  # manual, nothing was running: idle (Ruling 46)


async def _mark(settings: Settings, talk_id: str, **fields) -> None:
    db = init_db(settings.db_path)
    try:
        await db.update_talk(talk_id, **fields)
    finally:
        db.close()


async def test_boot_resumes_a_manual_room_that_crashed_mid_talk_and_keeps_the_others_idle(
    tmp_path: Path,
) -> None:  # Ruling 46
    settings = _settings(tmp_path)
    x, y = _talk("x", "r1", 60, 100), _talk("y", "r2", -60, -20)  # x is not due now: no tick opens it
    await _seed(settings, x, y, modes={"r1": "manual", "r2": "manual"})
    await _mark(settings, "x", status="live", actual_start=T0 - timedelta(minutes=5))
    await _mark(settings, "y", status="done", actual_start=T0 - timedelta(minutes=60),
                actual_end=T0 - timedelta(minutes=20))

    async with _open(settings) as (app, _):
        r1, r2 = app.state.workers["r1"], app.state.workers["r2"]
        assert r1.talk is not None and r1.talk.id == "x"
        assert r1.talk.actual_start == T0 - timedelta(minutes=5)
        assert r2.talk is None  # manual, its last talk had ended: no free session, no API spend
        assert app.state.autopilot.mode("r1") == "manual"
        assert (await app.state.db.get_talk("x")).status == "live"


async def test_boot_closes_live_rows_that_no_room_resumed(tmp_path: Path) -> None:  # stale live rows
    settings = _settings(tmp_path)
    past = _talk("past", "r1", -120, -60)  # r1 crashed during it; its slot is over
    old_free = _talk("free-r2-20300924T080000", "r2", -360, 360)
    await _seed(settings, past, old_free)
    await _mark(settings, "past", status="live", actual_start=T0 - timedelta(minutes=119))
    await _mark(settings, old_free.id, status="live", actual_start=T0 - timedelta(minutes=360))

    async with _open(settings) as (app, _):
        db = app.state.db
        assert app.state.workers["r1"].talk is None  # auto, talks today, none due
        r2 = app.state.workers["r2"].talk
        assert _is_free(r2) and r2.id != old_free.id  # a fresh free session
        for talk_id in ("past", old_free.id):
            stale = await db.get_talk(talk_id)
            assert stale.status == "done", talk_id
            assert T0 <= stale.actual_end < T0 + timedelta(seconds=10)  # boot time
        closed = [e for e in await db.recent_events(30) if e.type == "stale_live"]
        assert sorted(e.room_id for e in closed) == ["r1", "r2"]
        assert (await db.get_talk(r2.id)).status == "live"  # the new one is untouched


# ------------------------------------------------- integration: engine_mode fake


def _quick_fixture(path: Path) -> Path:
    """A FakeEngine recording that talks right away: a phrase every 0.9 s."""
    lines = []
    for i in range(12):
        end = "." if i % 3 == 2 else ""
        t = 0.2 + 0.3 * i
        lines.append(json.dumps({"t": t, "kind": "source_delta", "text": f" word{i}{end}"}))
        lines.append(json.dumps({"t": t + 0.05, "kind": "target_delta", "text": f" palabra{i}{end}"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def test_an_imported_talk_runs_on_the_fake_engine(tmp_path: Path) -> None:
    """The whole chain with engine_mode fake (FakeEngine, no API): import a
    CSV, the autopilot opens its talk, captions reach the bus and the DB,
    the operator ends it."""
    settings = _settings(tmp_path).model_copy(
        update={"engine_mode": "fake", "fake_fixture": str(_quick_fixture(tmp_path / "quick.jsonl"))}
    )
    csv_text = (
        "sala,inicio,fin,titulo,speakers,idioma,destinos,motor,abstract,tags,glosario\n"
        f"r1,{T0 - timedelta(minutes=5):%Y-%m-%d %H:%M},{T0 + timedelta(minutes=30):%Y-%m-%d %H:%M},"
        "Charla en vivo,Ana,en,es,,,,\n"
    )
    app = create_app(settings, clock=EventClock(), ingest_factory=SilentIngest, autopilot_interval_s=3600)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as client:
            client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
            imported = await client.post(
                "/api/admin/agenda/import", files={"file": ("agenda.csv", csv_text.encode(), "text/csv")}
            )
            assert imported.json() == {"imported": 1, "skipped": [], "removed": []}
            (talk,) = (await client.get("/api/admin/talks", params={"room": "r1", "day": "2030-09-24"})).json()

            await client.post("/api/admin/rooms/r1/mode", json={"mode": "auto"})  # ticks the room now
            worker = app.state.workers["r1"]
            assert worker.talk is not None and worker.talk.id == talk["id"]
            closed = False
            for _ in range(300):
                if any(m.type == "close" for m in app.state.bus.history("r1", "es", talk["id"])):
                    closed = True
                    break
                await asyncio.sleep(0.01)
            assert closed, "no caption segment closed within 3 s"
            ended = await client.post("/api/admin/rooms/r1/end-talk")

        assert ended.json()["room"]["state"] == "idle"
        es = app.state.bus.history("r1", "es", talk["id"])
        assert es[0].type == "talk" and any(m.type == "append" and "palabra" in (m.text or "") for m in es)
        stored = await app.state.db.get_talk(talk["id"])
        assert stored.status == "done" and stored.actual_end is not None
        assert [s.text for s in await app.state.db.get_segments(talk["id"], "es", "live")][0].startswith("palabra0")


# ------------------------------------------------------------------ talk end, loop


async def test_a_talk_end_is_published_and_reaches_the_app_hook(tmp_path: Path) -> None:
    ended: list[str] = []

    async def hook(talk: Talk) -> None:
        ended.append(talk.id)

    async with _open(_settings(tmp_path), on_talk_end=hook) as (app, client):
        await app.state.db.insert_talks([_talk("t", "r1", 0, 30)])
        sub = app.state.admin_events.subscribe()
        await client.post("/api/admin/rooms/r1/start-talk", json={"talk_id": "t"})
        await client.post("/api/admin/rooms/r1/end-talk")
        await app.state.workers["r1"].drain_hooks()

        published = []
        while not sub.empty():
            published.append(sub.get_nowait())
    assert ended[0].startswith("free-r1-") and ended[1] == "t"
    talk_ends = [e.data for e in published if e.kind == "talk_ended"]
    assert [(d["talk_id"] == "t", d["free"]) for d in talk_ends] == [(False, True), (True, False)]
    assert {"room_mode", "talk_started"} <= {e.kind for e in published}


async def test_the_lifespan_ticks_the_autopilot_and_cancels_the_loop_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = 0
    real_tick = Autopilot.tick

    async def counting_tick(self, room_id: str | None = None) -> None:
        nonlocal ticks
        ticks += 1
        await real_tick(self, room_id)

    monkeypatch.setattr(Autopilot, "tick", counting_tick)

    async with _open(_settings(tmp_path), autopilot_interval_s=0.01) as (app, _):
        for _ in range(300):
            if ticks >= 3:
                break
            await asyncio.sleep(0.01)
        loops = [t for t in asyncio.all_tasks() if t.get_name() == "autopilot"]
        assert len(loops) == 1 and not loops[0].done()

    assert ticks >= 3  # the boot tick and the loop's
    assert loops[0].cancelled()
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "autopilot" and not t.done()]


# ---------------------------------------------------------------------------- auth


async def test_the_control_endpoints_need_the_admin_cookie_and_the_csrf_header(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        requests = [
            ("GET", "/api/admin/rooms", None),
            ("POST", "/api/admin/rooms/r1/mode", {"mode": "manual"}),
            ("POST", "/api/admin/rooms/r1/start-talk", {"talk_id": "x"}),
            ("POST", "/api/admin/rooms/r1/end-talk", None),
            ("POST", "/api/admin/rooms/r1/reconnect", None),
        ]
        for method, path, body in requests:
            response = await client.request(method, path, json=body, headers={"X-Glosa-Admin": ""})
            assert response.status_code == 403, path
        client.cookies.clear()
        for method, path, body in requests:
            assert (await client.request(method, path, json=body)).status_code == 401, path
        assert app.state.autopilot.mode("r1") == "auto"
