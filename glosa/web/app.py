"""create_app(settings): the Glosa web app.

It mounts ``/static``, the audience pages (glosa/web/pages.py), the public
API (glosa/web/public_api.py) and the admin panel (glosa/web/admin_api.py,
login at ``/admin/login`` protected with ``Settings.admin_password``; see
glosa/web/auth.py), and owns the rooms: the lifespan opens the SQLite
database, registers every room of config.yaml, starts a free session in
each room that has a source, and stops them all on shutdown.

``app.state``:
  - ``settings``, ``bus`` (CaptionBus), ``db`` (Database, once started);
  - ``workers``: room id -> RoomWorker, in config.yaml order;
  - ``admin_secret``: a fresh per-process key (glosa/web/auth.py) signing
    admin session cookies;
  - ``rooms_view()``: the rooms as the pages see them (task-6 contract);
  - ``branding``: {"event_name", "primary", "accent", "logo_url"}.

Engines (``make_engine_factory``): Live Translate with the API key and
``prices.lt_per_min`` (Ruling 5), or, with ``engine_mode: fake``, FakeEngine
replaying a recorded session so a demo or a load test spends nothing:
``fake_fixture`` if set, else samples/fixtures/lt_en.jsonl under the working
directory, else the copy in the source checkout. None found is a
ConfigError at startup (an installed package has no samples/).

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
from glosa.models import EngineConfig, Room
from glosa.room import IngestFactory, RoomWorker
from glosa.web import admin_api, pages, public_api
from glosa.web.auth import new_admin_secret

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
FAKE_FIXTURE = Path("samples") / "fixtures" / "lt_en.jsonl"
CHECKOUT_FAKE_FIXTURE = Path(__file__).resolve().parents[2] / FAKE_FIXTURE
# On shutdown, open SSE streams are cut after this long (EventSource
# reconnects by itself) so the lifespan can stop the rooms.
SHUTDOWN_GRACE_S = 3


def resolve_fake_fixture(settings: Settings) -> str:
    """The recording engine_mode fake replays (see the module docstring)."""
    if settings.fake_fixture:
        candidates = [Path(settings.fake_fixture)]
    else:
        candidates = [Path.cwd() / FAKE_FIXTURE, CHECKOUT_FAKE_FIXTURE]
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    tried = ", ".join(str(path) for path in candidates)
    raise ConfigError(
        f"engine_mode fake: no recorded session found (tried {tried}); "
        "set fake_fixture in config.yaml to a JSONL recording"
    )


def make_engine_factory(settings: Settings, clock: Clock) -> EngineFactory:
    if settings.engine_mode == "fake":
        fixture = resolve_fake_fixture(settings)

        def fake(cfg: EngineConfig) -> FakeEngine:
            return FakeEngine(replace(cfg, kind="fake", fixture_path=fixture), clock)

        return fake

    def live(cfg: EngineConfig) -> LiveTranslateEngine:
        return LiveTranslateEngine(cfg, settings.gemini_api_key, clock, price_per_min=settings.prices.lt_per_min)

    return live


def create_app(
    settings: Settings,
    *,
    clock: Clock | None = None,
    engine_factory: EngineFactory | None = None,
    ingest_factory: IngestFactory = AudioIngest,
) -> FastAPI:
    clock = clock if clock is not None else RealClock()
    bus = CaptionBus(clock=clock)
    factory = engine_factory if engine_factory is not None else make_engine_factory(settings, clock)
    workers: dict[str, RoomWorker] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await asyncio.to_thread(init_db, settings.db_path)
        app.state.db = db
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
                workers[room.id] = RoomWorker(room, settings, bus, db, clock, factory, ingest_factory=ingest_factory)
            for worker in workers.values():
                if worker.has_source:
                    await _start_free_session(worker, db)
            yield
        finally:
            rooms = list(workers.values())
            results = await asyncio.gather(*(w.stop() for w in rooms), return_exceptions=True)
            for worker, result in zip(rooms, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    log.error("room %s: stop failed", worker.room.id, exc_info=result)
            workers.clear()
            db.close()

    app = FastAPI(title="Glosa", lifespan=lifespan)
    app.state.settings = settings
    app.state.bus = bus
    app.state.workers = workers
    # A fresh key per process (glosa/web/auth.py, Ruling 36): a restart
    # invalidates every outstanding admin session cookie.
    app.state.admin_secret = new_admin_secret()
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
    app.include_router(pages.router)
    return app


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
