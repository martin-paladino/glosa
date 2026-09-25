"""The admin panel's live feed (Task 12): GET /api/admin/stream.

What the panel reads from it: every room's raw RoomStatus once a second (with
its mode, current talk and next agenda talk), the event log as it grows (ids
for Last-Event-ID), the in-process admin events, and the last lines of each
room's original-language captions.

The stream never ends on its own, and httpx's ASGITransport buffers a whole
response, so ``_sse`` drives the ASGI app by hand: it collects the body and
sends ``http.disconnect`` once ``until`` says it has seen enough (or after a
timeout), which is also how a closing browser tab looks to the server.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI

from glosa.audio.ingest import StationHub
from glosa.captions.bus import CaptionBus
from glosa.clock import RealClock
from glosa.config import Settings
from glosa.db import init_db
from glosa.metrics import RoomHealth
from glosa.models import CaptionMsg, GlossaryTerm, Room, RoomStatus, Talk
from glosa.scheduler import Autopilot
from glosa.web import admin_stream
from glosa.web.admin_events import AdminEvents
from glosa.web.admin_stream import (
    LATENCY_LIMIT_S,
    LEVEL_MIN_DB,
    QUALITY_MIN,
    AdminMonitor,
    CaptionTail,
    classify,
    describe_event,
    localize,
    monitor_for,
)
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

ADMIN_PASSWORD = "test-password"
TZ = "America/Argentina/Buenos_Aires"
ART = ZoneInfo(TZ)
T0 = datetime(2030, 9, 24, 15, 0, tzinfo=ART)


class EventClock(RealClock):
    """RealClock with its wall clock pinned to T0 when it starts."""

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now())


def _status(**overrides) -> RoomStatus:
    values = dict(
        state="idle", level_db=-96.0, latency_p50_s=None, quality=None,
        cost_usd=0.0, talk_id=None, detail="no talk in progress",
    )
    values.update(overrides)
    return RoomStatus(**values)


def _talk(talk_id: str, room_id: str, start_min: float, end_min: float, **overrides) -> Talk:
    values = dict(
        id=talk_id, room_id=room_id, title=f"Talk {talk_id}", speakers=["Ana Pérez"], language="en",
        targets=["es"], engine="fast", start=T0 + timedelta(minutes=start_min),
        end=T0 + timedelta(minutes=end_min), abstract="", tags=[], glossary=[], status="scheduled",
        actual_start=None, actual_end=None,
    )
    values.update(overrides)
    return Talk(**values)


class FakeWorker:
    """The part of RoomWorker the admin stream reads."""

    def __init__(self, room_id: str, name: str, *, status: RoomStatus | None = None, talk: Talk | None = None,
                 language: str = "en", mode: str = "auto", source_type: str = "file") -> None:
        self.room = Room(id=room_id, slug=room_id, name=name, source_type=source_type,  # type: ignore[arg-type]
                         source_url=f"fake://{room_id}", mode=mode, public_token=f"tok-{room_id}",
                         default_targets=["es"])
        self.talk = talk
        self.language = language
        self.has_source = True
        self.state = status if status is not None else _status()

    def status(self) -> RoomStatus:
        return self.state


@dataclass
class Panel:
    app: FastAPI
    db: object
    workers: dict[str, FakeWorker]
    cookie: str


@pytest.fixture
async def panel(tmp_path: Path) -> AsyncIterator[Callable[..., Panel]]:
    dbs = []

    def make(*workers: FakeWorker, budget_usd: float = 10.0) -> Panel:
        settings = Settings(gemini_api_key="unused", admin_password=ADMIN_PASSWORD, timezone=TZ,
                            budget_usd=budget_usd, db_path=str(tmp_path / "glosa.db"))
        db = init_db(settings.db_path)
        dbs.append(db)
        clock = EventClock()
        by_id = {w.room.id: w for w in workers}
        app = FastAPI()
        app.state.settings = settings
        app.state.clock = clock
        app.state.db = db
        app.state.bus = CaptionBus(clock=clock)
        app.state.admin_events = AdminEvents()
        app.state.workers = by_id
        app.state.autopilot = Autopilot(db, by_id, clock, tz=TZ, events=app.state.admin_events)
        app.state.station_hub = StationHub(clock)
        app.state.admin_secret = new_admin_secret()
        app.state.session_epoch = 0
        app.include_router(admin_stream.stream_router)
        cookie = sign_session(app.state.admin_secret, ADMIN_PASSWORD)
        return Panel(app, db, by_id, cookie)

    yield make
    for db in dbs:
        db.close()


# ---- the raw ASGI client ----------------------------------------------------------------


@dataclass
class Frame:
    event: str
    data: dict
    id: int | None


def _frames(text: str) -> list[Frame]:
    frames = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name] = value.removeprefix(" ")
        if "data" in fields:
            frames.append(Frame(fields.get("event", "message"), json.loads(fields["data"]),
                                int(fields["id"]) if "id" in fields else None))
    return frames


async def _sse(
    app: FastAPI,
    path: str = "/api/admin/stream",
    *,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
    until: Callable[[list[Frame]], bool] | None = None,
    timeout: float = 5.0,
    during: Callable[[], object] | None = None,
) -> tuple[int, list[Frame], bool]:
    """GET ``path`` on ``app`` as a browser would: (status, frames, ended by
    itself). ``during`` runs once the response has started."""
    url = urlsplit(path)
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie is not None:
        raw_headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": url.path, "raw_path": url.path.encode(),
        "query_string": url.query.encode(), "root_path": "", "headers": raw_headers,
        "client": ("127.0.0.1", 50000), "server": ("test", 80), "app": app,
    }
    status = 0
    chunks: list[str] = []
    disconnect = asyncio.Event()
    started = asyncio.Event()

    async def receive() -> dict:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            started.set()
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b"").decode())
            if until is not None and until(_frames("".join(chunks))):
                disconnect.set()

    task = asyncio.create_task(app(scope, receive, send))
    if during is not None:
        await asyncio.wait_for(started.wait(), timeout)
        result = during()
        if asyncio.iscoroutine(result):
            await result
    done, _ = await asyncio.wait({task}, timeout=timeout)
    ended = bool(done)
    if not done:
        disconnect.set()
        await asyncio.wait_for(task, timeout)
    task.result()
    return status, _frames("".join(chunks)), ended


def _has(event: str, n: int = 1) -> Callable[[list[Frame]], bool]:
    return lambda frames: sum(f.event == event for f in frames) >= n


# ---- auth ----------------------------------------------------------------------------------


async def test_the_stream_needs_the_admin_cookie(panel) -> None:
    p = panel(FakeWorker("r1", "Sala Uno"))

    status, frames, _ = await _sse(p.app)
    assert status == 401 and frames == []

    status, _, _ = await _sse(p.app, cookie="0.garbage")
    assert status == 401


async def test_the_stream_is_exempt_from_the_csrf_header(panel) -> None:
    # An EventSource cannot send X-Glosa-Admin: this read-only GET only needs
    # the session cookie (every other /api/admin route still needs both).
    p = panel(FakeWorker("r1", "Sala Uno"))

    status, frames, _ = await _sse(p.app, cookie=p.cookie, until=_has("state"))

    assert status == 200
    assert frames[0].event == "state"


async def test_create_app_mounts_the_stream_behind_the_admin_cookie(tmp_path: Path) -> None:
    settings = Settings(gemini_api_key="unused", admin_password=ADMIN_PASSWORD, db_path=str(tmp_path / "g.db"))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        status, _, _ = await _sse(app)
        assert status == 401
        cookie = sign_session(app.state.admin_secret, ADMIN_PASSWORD)
        status, frames, _ = await _sse(app, cookie=cookie, until=_has("state"))
    assert status == 200 and frames[0].event == "state"


async def test_the_stream_ends_when_the_session_is_logged_out(panel) -> None:
    p = panel(FakeWorker("r1", "Sala Uno"))

    def logout() -> None:
        p.app.state.session_epoch += 1  # what POST /admin/logout does

    status, frames, ended = await _sse(p.app, cookie=p.cookie, during=logout, timeout=5)

    assert status == 200 and ended
    assert frames[-1].event == "bye" and frames[-1].data == {"reason": "session"}


# ---- content -------------------------------------------------------------------------------


async def test_each_state_frame_has_every_room_raw_status_mode_talk_and_next(panel) -> None:
    now = _talk("t-now", "r1", -20, 25, status="live", actual_start=T0 - timedelta(minutes=19))
    live = FakeWorker("r1", "Gran sala", talk=now, status=_status(
        state="yellow", level_db=-18.5, latency_p50_s=6.1, quality=0.9, cost_usd=0.42, talk_id="t-now",
        detail="latency 6.1s exceeds 5.0s"))
    idle = FakeWorker("r2", "Auditorio", mode="manual")
    p = panel(live, idle)
    await p.db.insert_talks([now, _talk("t-next", "r2", 30, 70, title="Postgres a escala")])

    status, frames, _ = await _sse(p.app, cookie=p.cookie, until=_has("state"))

    assert status == 200
    state = next(f for f in frames if f.event == "state").data
    r1, r2 = state["rooms"]
    assert r1["id"] == "r1" and r1["name"] == "Gran sala" and r1["key"] == 1
    assert r1["status"] == {
        "state": "yellow", "level_db": -18.5, "latency_p50_s": 6.1, "quality": 0.9, "cost_usd": 0.42,
        "talk_id": "t-now", "detail": "latency 6.1s exceeds 5.0s", "gated_s": 0.0,
    }
    assert r1["state"] == "degraded" and r1["mode"] == "auto"
    assert r1["talk"]["id"] == "t-now" and r1["talk"]["target"] == "es"
    assert r1["issue"]["kind"] == "latency" and r1["issue"]["action"] == "reconnect"
    assert r2["state"] == "idle" and r2["mode"] == "manual" and r2["talk"] is None
    assert r2["next"]["id"] == "t-next" and r2["next"]["title"] == "Postgres a escala"
    assert state["now"].startswith("2030-09-24T15:00")
    assert state["budget"]["budget"] == 10.0
    assert state["panels"] == 1


async def test_the_state_frame_is_localized_to_the_panel_language(panel) -> None:
    worker = FakeWorker("r1", "Gran sala", talk=_talk("t", "r1", -5, 30), status=_status(
        state="yellow", latency_p50_s=6.1, detail="latency 6.1s exceeds 5.0s", talk_id="t"))
    p = panel(worker)

    _, es, _ = await _sse(p.app, "/api/admin/stream?lang=es", cookie=p.cookie, until=_has("state"))
    _, en, _ = await _sse(p.app, "/api/admin/stream?lang=en", cookie=p.cookie, until=_has("state"))

    assert es[0].data["rooms"][0]["text"]["what"] == "Retraso de 6,1 s; el tope es 5 s."
    assert en[0].data["rooms"][0]["text"]["what"] == "Latency of 6.1 s; the limit is 5 s."
    assert es[0].data["attention"][0]["room_id"] == "r1"


async def test_the_log_backlog_then_new_events_with_ids(panel) -> None:
    p = panel(FakeWorker("r1", "Sala Uno"))
    await p.db.log_event("r1", "info", "rotation", "session handover #1")
    await p.db.log_event("r1", "error", "source_down", "ffmpeg exited 5 times")

    async def later() -> None:
        await asyncio.sleep(0.2)
        await p.db.log_event("r1", "warning", "reconnect", "engine reconnect #1")

    _, frames, _ = await _sse(p.app, cookie=p.cookie, during=later, until=_has("log", 3))

    logs = [f for f in frames if f.event == "log"]
    assert [f.data["type"] for f in logs] == ["rotation", "source_down", "reconnect"]
    assert [f.id for f in logs] == sorted(f.id for f in logs) and all(f.id == f.data["id"] for f in logs)
    assert [f.data["alert"] for f in logs] == [False, True, True]
    assert logs[1].data["room"] == "Sala Uno"
    assert logs[1].data["text"] == "Fuente caída: ffmpeg exited 5 times"


async def test_last_event_id_resumes_the_log(panel) -> None:
    p = panel(FakeWorker("r1", "Sala Uno"))
    for n in range(1, 4):
        await p.db.log_event("r1", "info", "rotation", f"session handover #{n}")
    first = min(e.id for e in await p.db.recent_events(3))

    _, frames, _ = await _sse(p.app, cookie=p.cookie, headers={"Last-Event-ID": str(first)},
                              until=_has("state", 2))

    assert [f.id for f in frames if f.event == "log"] == [first + 1, first + 2]


async def test_admin_events_are_forwarded(panel) -> None:
    p = panel(FakeWorker("r1", "Sala Uno"))

    def publish() -> None:
        p.app.state.admin_events.publish("talk_updated", {"talk": {"id": "x"}, "fields": ["title"]})

    _, frames, _ = await _sse(p.app, cookie=p.cookie, during=publish, until=_has("notice"))

    notice = next(f for f in frames if f.event == "notice")
    assert notice.data["kind"] == "talk_updated" and notice.data["data"]["fields"] == ["title"]


async def test_the_original_language_captions_of_each_room(panel) -> None:
    talk = _talk("t", "r1", -5, 30, language="es", targets=["en"])
    p = panel(FakeWorker("r1", "Sala Uno", talk=talk, status=_status(state="green", detail="ok", talk_id="t")))
    bus = p.app.state.bus
    bus.publish("r1", "es", "talk", data={"talk_id": "t"})
    bus.publish("r1", "es", "append", seg=0, text="Hola a todos.")
    bus.publish("r1", "es", "close", seg=0)
    bus.publish("r1", "en", "append", seg=0, text="Hello everyone.")  # not the original: not shown

    def more() -> None:
        bus.publish("r1", "es", "append", seg=1, text="Bienvenidos a")

    _, frames, _ = await _sse(
        p.app, cookie=p.cookie, during=more,
        until=lambda fs: any(f.event == "cc" and f.data["open"] == "Bienvenidos a" for f in fs),
    )

    cc = [f.data for f in frames if f.event == "cc"]
    assert cc[-1]["room_id"] == "r1" and cc[-1]["lang"] == "es"
    assert cc[-1]["closed"] == "Hola a todos." and cc[-1]["open"] == "Bienvenidos a"
    assert all("Hello" not in c["closed"] + c["open"] for c in cc)


# ---- the monitor ---------------------------------------------------------------------------------


async def test_a_long_silence_raises_the_alarm_once_and_logs_it(panel) -> None:
    quiet = _status(state="yellow", level_db=-80.0, talk_id="t", detail="level -80.0dB below -50dB with active talk")
    p = panel(FakeWorker("r1", "Sala Uno", talk=_talk("t", "r1", -5, 30), status=quiet))
    monitor = AdminMonitor(p.app, silence_alarm_s=0.3)

    first = await monitor.snapshot()
    assert first["rooms"][0]["issue"]["kind"] == "level"
    await asyncio.sleep(0.6)
    later = await monitor.snapshot()
    await asyncio.sleep(0.6)
    await monitor.snapshot()

    assert later["rooms"][0]["issue"]["kind"] == "silence"
    silences = [e for e in await p.db.recent_events(10) if e.type == "silence"]
    assert len(silences) == 1 and silences[0].level == "warning" and silences[0].room_id == "r1"


async def test_the_budget_warns_at_80_percent_and_when_payment_is_refused(panel) -> None:
    ok = FakeWorker("r1", "Sala Uno")
    p = panel(ok, budget_usd=1.0)
    await p.db.add_cost("r1", "live_translate", 20, 0.79)
    assert (await AdminMonitor(p.app).snapshot())["budget"]["alert"] is None

    await p.db.add_cost("r1", "live_translate", 1, 0.02)
    snap = await AdminMonitor(p.app).snapshot()
    assert snap["budget"]["alert"] == "80%" and snap["budget"]["spent"] == pytest.approx(0.81)
    assert snap["rooms"][0]["cost_usd"] == pytest.approx(0.81)
    assert localize(snap, "es")["attention"][0]["key"] == "budget"

    ok.state = _status(state="red", talk_id="t", detail="payment blocked: budget exhausted")
    assert (await AdminMonitor(p.app).snapshot())["budget"]["alert"] == "exhausted"


async def test_attention_lists_down_rooms_first_and_says_all_clear_when_nothing_is_wrong(panel) -> None:
    degraded = FakeWorker("r1", "Sala Abasto", talk=_talk("a", "r1", -5, 30), status=_status(
        state="yellow", latency_p50_s=6.1, talk_id="a", detail="latency 6.1s exceeds 5.0s"))
    down = FakeWorker("r2", "Sala Comunidad", talk=_talk("b", "r2", -5, 30), status=_status(
        state="red", talk_id="b", detail="source is down: ffmpeg exited 5 times"))
    fine = FakeWorker("r3", "Gran sala", talk=_talk("c", "r3", -5, 30), status=_status(
        state="green", level_db=-20.0, talk_id="c", detail="ok"))
    p = panel(degraded, down, fine)

    view = localize(await AdminMonitor(p.app).snapshot(), "es")

    assert [row["room_id"] for row in view["attention"]] == ["r2", "r1"]
    assert view["attention"][0]["action"] == "restart" and view["attention"][1]["action"] == "reconnect"
    assert view["attention"][0]["what"] == "Fuente caída: ffmpeg exited 5 times."
    rooms = {room["id"]: room for room in view["rooms"]}
    assert rooms["r2"]["text"]["state_line"] == "Fuente caída: ffmpeg exited 5 times."  # not "Caída. Fuente caída"
    assert rooms["r1"]["text"]["state_line"] == "Degradada. Retraso de 6,1 s; el tope es 5 s."
    assert [(t["state"], t["count"]) for t in view["tally"]] == [("live", 1), ("degraded", 1), ("down", 1)]

    degraded.state = down.state = fine.state
    calm = localize(await AdminMonitor(p.app).snapshot(), "es")
    assert calm["attention"] == []
    assert calm["all_clear"] == "Todo en orden. Las 3 salas están dentro de los márgenes."


async def test_the_next_automatic_change(panel) -> None:
    running = _talk("now", "r1", -20, 40, status="live")
    p = panel(FakeWorker("r1", "Gran sala", talk=running, status=_status(state="green", detail="ok", talk_id="now")),
              FakeWorker("r2", "Sala Talleres"), FakeWorker("r3", "Manual", mode="manual"))
    await p.db.insert_talks([running, _talk("later", "r2", 29, 90, title="Taller de Go"),
                             _talk("sooner-but-manual", "r3", 10, 50)])

    view = localize(await AdminMonitor(p.app).snapshot(), "es")

    change = view["next_change"]
    assert change["room_id"] == "r2" and change["kind"] == "open"
    assert change["at"].startswith("2030-09-24T15:29")
    assert change["text"] == "15:29, abre Sala Talleres"


# ---- pure helpers ----------------------------------------------------------------------------------


def _health(**signals) -> RoomStatus:
    values = dict(latency_p50=None, quality_avg=None, level_db=-20.0, stall_active=False, source_down=False,
                  payment_blocked=False, talk_active=True)
    values.update(signals)
    state, detail = RoomHealth.evaluate(**values)
    return _status(state=state, detail=detail, latency_p50_s=values["latency_p50"],
                   quality=values["quality_avg"], level_db=values["level_db"])


@pytest.mark.parametrize(
    ("status", "kind", "action"),
    [
        (_health(), None, None),
        (_health(talk_active=False), None, None),
        (_health(stall_active=True), "stalled", "reconnect"),
        (_health(source_down=True), "source_down", "restart"),
        (_status(state="red", detail="source is down: ffmpeg exited 5 times"), "source_down", "restart"),
        (_status(state="red", detail="engine halted: non-retryable error, waiting for a reconnect"),
         "halted", "reconnect"),
        (_health(payment_blocked=True), "payment", None),
        (_health(latency_p50=6.1), "latency", "reconnect"),
        (_health(quality_avg=0.42), "quality", "open"),
        (_health(level_db=-62.0), "level", "open"),
        (_health(recent_reconnect=True), "reconnected", "open"),
        (_status(state="yellow", detail="something new"), "other", "open"),
    ],
)
def test_classify_names_each_health_rule(status: RoomStatus, kind: str | None, action: str | None) -> None:
    issue = classify(status)
    assert (issue.kind if issue else None, issue.action if issue else None) == (kind, action)


def test_the_limits_are_roomhealths() -> None:
    assert _health(latency_p50=LATENCY_LIMIT_S).state == "green"
    assert _health(latency_p50=LATENCY_LIMIT_S + 0.01).state == "yellow"
    assert _health(quality_avg=QUALITY_MIN).state == "green"
    assert _health(quality_avg=QUALITY_MIN - 0.01).state == "yellow"
    assert _health(level_db=LEVEL_MIN_DB).state == "green"
    assert _health(level_db=LEVEL_MIN_DB - 0.1).state == "yellow"


def test_the_level_issue_reads_the_level_from_the_detail() -> None:
    issue = classify(_health(level_db=-62.0))
    assert issue.values["level"] == pytest.approx(-62.0)


def _msg(id: int, type: str, seg: int | None = None, text: str | None = None, data: dict | None = None,
         ts: float = 1.0) -> CaptionMsg:
    return CaptionMsg(id=id, type=type, seg=seg, text=text, data=data, ts=ts)  # type: ignore[arg-type]


def test_caption_tail_keeps_the_closed_text_and_the_open_phrase() -> None:
    tail = CaptionTail("en")
    tail.feed(_msg(1, "talk", data={"talk_id": "t"}))
    tail.feed(_msg(2, "append", 0, "Hello"))
    tail.feed(_msg(3, "append", 0, " world."))
    tail.feed(_msg(4, "close", 0))
    tail.feed(_msg(5, "append", 1, "And then", ts=9.5))

    assert tail.state() == {"lang": "en", "talk_id": "t", "closed": "Hello world.", "open": "And then", "ts": 9.5}


def test_caption_tail_starts_over_for_a_new_talk_and_keeps_only_the_end() -> None:
    tail = CaptionTail("en")
    tail.feed(_msg(1, "append", 0, "Old talk."))
    tail.feed(_msg(2, "close", 0))
    tail.feed(_msg(3, "talk", data={"talk_id": "next"}))
    assert tail.state()["closed"] == ""
    for n in range(40):
        tail.feed(_msg(10 + 2 * n, "append", n, f"Sentence number {n} is here."))
        tail.feed(_msg(11 + 2 * n, "close", n))
    closed = tail.state()["closed"]
    assert closed.endswith("Sentence number 39 is here.") and "number 0 " not in closed
    assert len(closed) <= CaptionTail.MAX_CHARS + 1


def test_caption_tail_takes_set_messages() -> None:
    # Task 10's "set": the whole text of the open segment so far; "" removes it.
    tail = CaptionTail("es")
    tail.feed(_msg(1, "set", 0, "por cierto"))
    tail.feed(_msg(2, "set", 0, "Por cierto, cuando"))
    assert tail.state()["open"] == "Por cierto, cuando"
    tail.feed(_msg(3, "close", 0))
    tail.feed(_msg(4, "set", 1, "eh"))
    tail.feed(_msg(5, "set", 1, ""))
    assert tail.state()["closed"] == "Por cierto, cuando" and tail.state()["open"] == ""


def test_event_descriptions_in_both_languages() -> None:
    @dataclass
    class Ev:
        id: int
        ts: str
        room_id: str | None
        level: str
        type: str
        message: str

    ev = Ev(1, "2030-09-24T18:00:00+00:00", "r1", "info", "talk_start", "abc123: Rust in the kernel (en -> es)")
    assert describe_event(ev, "es", {}) == "Empezó «Rust in the kernel» (EN ▸ ES)."
    assert describe_event(ev, "en", {}) == "Started “Rust in the kernel” (EN ▸ ES)."
    ended = Ev(2, ev.ts, "r1", "info", "talk_end", "abc123")
    assert describe_event(ended, "es", {"abc123": "Rust in the kernel"}) == "Terminó «Rust in the kernel»."
    restarted = Ev(3, ev.ts, "r1", "info", "restart", "abc123: source reopened by the operator")
    assert describe_event(restarted, "es", {"abc123": "Rust"}) == "Se volvió a abrir la fuente de «Rust»."
    odd = Ev(4, ev.ts, "r1", "info", "something_new", "raw text")
    assert describe_event(odd, "es", {}) == "raw text"


def test_the_glossary_count_of_a_talk() -> None:
    talk = _talk("t", "r1", 0, 30, glossary=[GlossaryTerm("Kubernetes", True), GlossaryTerm("pod", False, "vaina")])
    assert admin_stream.talk_brief(talk, ART)["glossary"] == 2


def test_a_source_error_is_shown_as_its_first_line_without_ffmpeg_prefixes() -> None:
    detail = ("source is down: [in#0 @ 0xbc501c000] Error opening input: No such file or directory\n"
              "Error opening input file /srv/feeds/sala-5.opus.\nError opening input files: No such file or directory")
    issue = classify(_status(state="red", talk_id="t", detail=detail))
    assert issue.values["error"] == "Error opening input: No such file or directory"
    long = classify(_status(state="red", talk_id="t", detail="source is down: " + "x" * 300))
    assert len(long.values["error"]) <= 120 and long.values["error"].endswith("…")


def test_the_log_shows_the_first_line_of_an_ffmpeg_error() -> None:
    @dataclass
    class Ev:
        id: int
        ts: str
        room_id: str | None
        level: str
        type: str
        message: str

    down = Ev(1, "2030-09-24T18:00:00+00:00", "r1", "error", "source_down",
              "[in#0 @ 0x1] Error opening input: No such file or directory\nError opening input file /srv/x.opus.")
    assert describe_event(down, "es", {}) == "Fuente caída: Error opening input: No such file or directory"


# ---- fix round 1 -------------------------------------------------------------------------------


async def test_a_disconnected_panel_leaves_nothing_subscribed(panel) -> None:
    talk = _talk("t", "r1", -5, 30)
    p = panel(FakeWorker("r1", "Sala Uno", talk=talk, status=_status(state="green", detail="ok", talk_id="t")),
              FakeWorker("r2", "Sala Dos"))
    p.app.state.bus.publish("r1", "en", "append", seg=0, text="Hello")

    await _sse(p.app, cookie=p.cookie, until=lambda fs: any(f.event == "cc" for f in fs))
    await asyncio.sleep(0)

    assert p.app.state.admin_events.subscribers == 0
    assert monitor_for(p.app).panels == 0
    tracks = p.app.state.bus._tracks  # no public count: every (room, lang) track the stream opened
    assert tracks and all(not track.subscribers for track in tracks.values())


async def test_the_silence_row_keeps_its_age_when_it_becomes_the_alarm(panel) -> None:
    quiet = _status(state="yellow", level_db=-80.0, talk_id="t", detail="level -80.0dB below -50dB with active talk")
    p = panel(FakeWorker("r1", "Sala Uno", talk=_talk("t", "r1", -5, 30), status=quiet))
    monitor = AdminMonitor(p.app, silence_alarm_s=0.3)

    first = await monitor.snapshot()
    await asyncio.sleep(1.1)
    later = await monitor.snapshot()

    assert (first["rooms"][0]["issue"]["kind"], later["rooms"][0]["issue"]["kind"]) == ("level", "silence")
    assert later["rooms"][0]["since"] == first["rooms"][0]["since"]


async def test_an_emitter_room_carries_its_station_admin_only(panel) -> None:
    detail = "ok | station: Focusrite Scarlett 2i2, -23.0 dBFS, last audio 0.4s ago"
    station = FakeWorker("st", "Sala Estación", source_type="emitter", talk=_talk("t", "st", -5, 30),
                         status=_status(state="green", detail=detail, talk_id="t"))
    p = panel(station, FakeWorker("r1", "Sala Uno"))
    hub = p.app.state.station_hub
    await hub.connect("st", object())  # a station socket; only its presence matters here
    hub.set_hello("st", "Focusrite Scarlett 2i2")
    hub.set_level("st", -23.0)
    hub.push_audio("st", bytes(3200))

    snap = await AdminMonitor(p.app).snapshot()

    st, r1 = snap["rooms"]
    assert st["station"]["connected"] is True and st["station"]["device"] == "Focusrite Scarlett 2i2"
    assert st["station"]["level_db"] == -23.0 and st["station"]["last_audio_age_s"] is not None
    assert r1["station"] is None
    assert st["issue"] is None  # the station suffix of the raw detail is not an issue


def test_a_silent_station_is_an_issue_of_its_own() -> None:
    issue = classify(_status(state="red", talk_id="t",
                             detail="source is down: station disconnected | station: not connected"))
    assert (issue.kind, issue.severity, issue.action) == ("station", "down", "open")
    texts = localize_issue(issue, "es")
    assert texts["what"] == "La estación de la sala no manda audio."
    degraded = classify(_status(state="yellow", latency_p50_s=6.1, talk_id="t",
                                detail="latency 6.1s exceeds 5.0s | station: Mic, -20.0 dBFS, last audio 0.1s ago"))
    assert degraded.kind == "latency"


def localize_issue(issue, lang):
    return admin_stream.issue_texts({"kind": issue.kind, "action": issue.action, "values": issue.values}, lang)


def test_the_station_events_read_as_such() -> None:
    @dataclass
    class Ev:
        id: int
        ts: str
        room_id: str | None
        level: str
        type: str
        message: str

    down = Ev(1, "2030-09-24T18:00:00+00:00", "st", "error", "source_down", "station disconnected")
    back = Ev(2, "2030-09-24T18:01:00+00:00", "st", "info", "source_recovered", "station reconnected")
    assert describe_event(down, "es", {}) == "La estación dejó de mandar audio."
    assert describe_event(back, "es", {}) == "La estación volvió a mandar audio."
