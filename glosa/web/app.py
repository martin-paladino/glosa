"""create_app(settings): the Glosa web app.

It mounts ``/static``, the audience pages (glosa/web/pages.py), the public
API (glosa/web/public_api.py) and the admin panel (glosa/web/admin_api.py,
login at ``/admin/login`` protected with ``Settings.admin_password``; see
glosa/web/auth.py), and owns the rooms. The lifespan:

  1. opens the SQLite database and registers every room of config.yaml,
     keeping the mode (auto/manual) and public token already stored;
  2. creates the Autopilot (glosa/scheduler.py, ``lead_s`` 60) and runs one
     tick, which reopens the agenda's talk of the moment after a restart;
  3. resumes, in each manual room, the talk that was still live when the
     server stopped (a crash); other manual rooms stay idle (Ruling 46);
     starts a free session in each auto room with a source that the
     autopilot does not run (no agenda talks today; Ruling 33); and closes
     the talks still marked live that no room runs now (``_boot_rooms``);
  4. runs ``Autopilot.tick()`` every ``autopilot_interval_s`` (5 s) in the
     task named ``autopilot``;
  5. on shutdown cancels that loop (a tick in progress finishes), stops
     every room and gives the talk-end hooks ``HOOK_GRACE_S``.

Every talk that ends publishes ``talk_ended`` on ``admin_events``, then goes
to ``create_app(on_talk_end=...)`` if given (Task 11: exports).

``app.state``:
  - ``settings``, ``clock``, ``bus`` (CaptionBus), ``db`` (Database, once
    started);
  - ``admin_events``: the admin panel's in-process broadcaster
    (glosa/web/admin_events.py);
  - ``workers``: room id -> RoomWorker, in config.yaml order;
  - ``autopilot``: the Autopilot (once started);
  - ``admin_secret``: a fresh per-process key (glosa/web/auth.py) signing
    admin session cookies;
  - ``session_epoch``: an int, 0 until the first ``POST /admin/logout``,
    which increments it and so invalidates every outstanding session
    (Ruling 43: mixed into each session token's HMAC, not compared as a
    timestamp);
  - ``rooms_view()``: the rooms as the pages see them (task-6 contract);
  - ``branding``: {"event_name", "primary", "accent", "logo_url"}.

Engines (``make_engine_factory``), by ``EngineConfig.kind`` (the talk's
engine): "fast" is Live Translate with the API key and ``prices.lt_per_min``
(Ruling 5); "glossary" is transcribe-live with ``prices.transcribe_per_min``
and the glossary as vocabulary. With ``engine_mode: fake``, FakeEngine
replays a recorded session so a demo or a load test spends nothing: for
"fast", ``fake_fixture`` if set, else samples/fixtures/lt_en.jsonl; for
"glossary", samples/fixtures/tr_es.jsonl; each under the working directory,
else the copy in the source checkout. None found is a ConfigError at
startup (an installed package has no samples/). (RoomWorker then uses
FakeTranslator for the text translations: no API either.)

Run it with ``python -m glosa.web.app`` (``main()``: $HOST, default 0.0.0.0,
and $PORT, default 8000), which reads .env and config.yaml from the working
directory (or $GLOSA_ENV_FILE and $GLOSA_CONFIG). Plain ``uvicorn --factory
glosa.web.app:app_from_env`` works too, but then pass
``--timeout-graceful-shutdown``: an audience SSE stream never ends on its
own, and without that timeout uvicorn waits for every open one on SIGTERM,
so the lifespan never stops the rooms (talks not closed, cost not flushed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from glosa.audio.ingest import AudioIngest
from glosa.captions.bus import CaptionBus
from glosa.clock import Clock, RealClock
from glosa.config import ConfigError, Settings
from glosa.db import init_db
from glosa.engines.base import EngineFactory
from glosa.engines.fake import FakeEngine
from glosa.engines.live_translate import LiveTranslateEngine
from glosa.engines.transcribe import TranscribeLiveEngine
from glosa.models import EngineConfig, Room, Talk
from glosa.room import IngestFactory, RoomWorker, TalkEndHook, is_free_talk
from glosa.scheduler import LEAD_S, TICK_S, Autopilot
from glosa.web import admin_api, pages, public_api
from glosa.web.admin_events import AdminEvents
from glosa.web.auth import new_admin_secret

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
FAKE_FIXTURE = Path("samples") / "fixtures" / "lt_en.jsonl"
CHECKOUT_FAKE_FIXTURE = Path(__file__).resolve().parents[2] / FAKE_FIXTURE
FAKE_GLOSSARY_FIXTURE = Path("samples") / "fixtures" / "tr_es.jsonl"
CHECKOUT_FAKE_GLOSSARY_FIXTURE = Path(__file__).resolve().parents[2] / FAKE_GLOSSARY_FIXTURE
# On shutdown, open SSE streams are cut after this long (EventSource
# reconnects by itself) so the lifespan can stop the rooms.
SHUTDOWN_GRACE_S = 3
# On shutdown, the talk-end hooks (Task 11: exports) get this long to finish.
HOOK_GRACE_S = 5.0


def resolve_fake_fixture(settings: Settings, kind: str = "fast") -> str:
    """The recording engine_mode fake replays for an engine kind (see the
    module docstring)."""
    if kind == "glossary":
        candidates = [Path.cwd() / FAKE_GLOSSARY_FIXTURE, CHECKOUT_FAKE_GLOSSARY_FIXTURE]
    elif settings.fake_fixture:
        candidates = [Path(settings.fake_fixture)]
    else:
        candidates = [Path.cwd() / FAKE_FIXTURE, CHECKOUT_FAKE_FIXTURE]
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    tried = ", ".join(str(path) for path in candidates)
    if kind == "glossary":
        hint = f"run from a checkout, or copy {FAKE_GLOSSARY_FIXTURE} into the working directory"
    else:
        hint = "set fake_fixture in config.yaml to a JSONL recording"
    raise ConfigError(f"engine_mode fake: no recorded session found for the {kind} engine (tried {tried}); {hint}")


def make_engine_factory(settings: Settings, clock: Clock) -> EngineFactory:
    if settings.engine_mode == "fake":
        fixtures = {kind: resolve_fake_fixture(settings, kind) for kind in ("fast", "glossary")}

        def fake(cfg: EngineConfig) -> FakeEngine:
            fixture = fixtures.get(cfg.kind, fixtures["fast"])
            return FakeEngine(replace(cfg, kind="fake", fixture_path=fixture), clock)

        return fake

    def live(cfg: EngineConfig) -> LiveTranslateEngine | TranscribeLiveEngine:
        prices = settings.prices
        if cfg.kind == "glossary":
            return TranscribeLiveEngine(cfg, settings.gemini_api_key, clock, price_per_min=prices.transcribe_per_min)
        return LiveTranslateEngine(cfg, settings.gemini_api_key, clock, price_per_min=prices.lt_per_min)

    return live


def create_app(
    settings: Settings,
    *,
    clock: Clock | None = None,
    engine_factory: EngineFactory | None = None,
    ingest_factory: IngestFactory = AudioIngest,
    on_talk_end: TalkEndHook | None = None,
    autopilot_interval_s: float = TICK_S,
) -> FastAPI:
    clock = clock if clock is not None else RealClock()
    bus = CaptionBus(clock=clock)
    admin_events = AdminEvents()
    factory = engine_factory if engine_factory is not None else make_engine_factory(settings, clock)
    workers: dict[str, RoomWorker] = {}

    async def talk_ended(talk: Talk) -> None:
        """Every room's RoomWorker.on_talk_end: tell the admin panel, then
        the caller's hook."""
        admin_events.publish(
            "talk_ended",
            {
                "room_id": talk.room_id,
                "talk_id": talk.id,
                "title": talk.title,
                "free": is_free_talk(talk.id),
                "actual_end": talk.actual_end.isoformat() if talk.actual_end is not None else None,
            },
        )
        if on_talk_end is not None:
            await on_talk_end(talk)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await asyncio.to_thread(init_db, settings.db_path)
        app.state.db = db
        pilot: asyncio.Task | None = None
        try:
            known = {room.id: room for room in await db.get_rooms()}
            for cfg in settings.rooms:
                before = known.get(cfg.id)
                room = Room(
                    id=cfg.id,
                    slug=cfg.id,
                    name=cfg.name,
                    source_type=cfg.source_type,
                    source_url=cfg.source_url,
                    mode=before.mode if before is not None else "auto",
                    public_token=before.public_token if before is not None else secrets.token_urlsafe(16),
                    default_targets=list(cfg.default_targets),
                )
                await db.upsert_room(room)
                workers[room.id] = RoomWorker(
                    room, settings, bus, db, clock, factory, ingest_factory=ingest_factory, on_talk_end=talk_ended
                )
            autopilot = Autopilot(db, workers, clock, lead_s=LEAD_S, tz=settings.timezone, events=admin_events)
            app.state.autopilot = autopilot
            await _boot_rooms(autopilot, workers, db, clock.wall())
            pilot = asyncio.create_task(autopilot.run(autopilot_interval_s), name="autopilot")
            yield
        finally:
            if pilot is not None:  # first, so no tick starts a room while they stop
                pilot.cancel()
                (result,) = await asyncio.gather(pilot, return_exceptions=True)
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    log.error("autopilot loop failed", exc_info=result)
            rooms = list(workers.values())
            results = await asyncio.gather(*(w.stop() for w in rooms), return_exceptions=True)
            for worker, result in zip(rooms, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    log.error("room %s: stop failed", worker.room.id, exc_info=result)
            await asyncio.gather(*(w.drain_hooks(HOOK_GRACE_S) for w in rooms), return_exceptions=True)
            workers.clear()
            db.close()

    app = FastAPI(title="Glosa", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.bus = bus
    app.state.admin_events = admin_events
    app.state.workers = workers
    # A fresh key per process (glosa/web/auth.py, Ruling 36): a restart
    # invalidates every outstanding admin session cookie.
    app.state.admin_secret = new_admin_secret()
    # Incremented by POST /admin/logout: invalidates every outstanding
    # session at once (Ruling 43), not just the browser that logged out.
    app.state.session_epoch = 0
    app.state.rooms_view = lambda: [w.view() for w in workers.values()]
    app.state.branding = {
        "event_name": settings.event_name,
        "primary": settings.branding.primary or None,
        "accent": settings.branding.accent or None,
        "logo_url": settings.branding.logo_url or None,
    }
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(public_api.router)
    app.include_router(admin_api.router)
    app.include_router(admin_api.api_router)
    app.include_router(pages.router)
    return app


async def _boot_rooms(autopilot: Autopilot, workers: dict[str, RoomWorker], db, boot: datetime) -> None:
    """What each room runs at startup:

      1. the autopilot's tick reopens whatever the agenda says is on now in
         the auto rooms (the server restarted mid-talk, spec §6);
      2. a manual room whose last talk was still ``live`` (a crash) resumes
         it; any other manual room stays idle: no free session, no API
         spend (Ruling 46);
      3. an auto room with a source that the autopilot does not run (no
         agenda talks today) starts its free session (Task 5, Ruling 33);
      4. any talk still ``live`` that no room now runs (left by a crash) is
         closed: ``done``, ``actual_end`` = boot time.
    """
    try:
        await autopilot.tick()
    except Exception:
        log.exception("autopilot: the boot tick failed")
    for worker in workers.values():
        if not worker.has_source or worker.talk is not None:
            continue
        room_id = worker.room.id
        if autopilot.mode(room_id) == "manual":
            await _resume_manual_room(worker, db)
            continue
        try:
            owned = await autopilot.in_charge(room_id)
        except Exception:
            log.exception("room %s: could not ask the autopilot", room_id)
            owned = False
        if not owned:
            await _start_free_session(worker, db)
    await _close_stale_live_talks(workers, db, boot)


async def _resume_manual_room(worker: RoomWorker, db) -> None:
    room_id = worker.room.id
    try:
        last = await db.get_last_started_talk(room_id)
        if last is None or last.status != "live":
            return
        await worker.start(last)
        await db.log_event(room_id, "info", "resumed", f"{last.id}: still live when the server stopped")
    except Exception as exc:
        log.exception("room %s: could not resume its talk", room_id)
        try:
            await db.log_event(room_id, "error", "resume_failed", repr(exc))
        except Exception:
            log.exception("room %s: could not log the failure", room_id)


async def _close_stale_live_talks(workers: dict[str, RoomWorker], db, boot: datetime) -> None:
    held = {w.talk.id for w in workers.values() if w.talk is not None}
    try:
        for talk in await db.get_live_talks():
            if talk.id in held:
                continue
            await db.update_talk(talk.id, status="done", actual_end=boot)
            message = f"{talk.id}: still live from a previous run, closed"
            await db.log_event(talk.room_id, "warning", "stale_live", message)
    except Exception:
        log.exception("could not close the talks left live by a previous run")


async def _start_free_session(worker: RoomWorker, db) -> None:
    """One room that cannot start must not keep the others from starting."""
    try:
        await worker.start(None)
    except Exception as exc:
        log.exception("room %s: could not start", worker.room.id)
        try:
            await db.log_event(worker.room.id, "error", "start_failed", repr(exc))
        except Exception:
            log.exception("room %s: could not log the failure", worker.room.id)


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory glosa.web.app:app_from_env``."""
    env_file = os.environ.get("GLOSA_ENV_FILE", ".env")
    config = os.environ.get("GLOSA_CONFIG", "config.yaml")
    return create_app(Settings.load(env_path=env_file, config_path=config))


def main() -> None:
    """``python -m glosa.web.app``: serve Glosa on $HOST:$PORT."""
    import uvicorn

    uvicorn.run(
        "glosa.web.app:app_from_env",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
    )


if __name__ == "__main__":
    main()
