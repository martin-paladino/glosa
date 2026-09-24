"""Tests for glosa.web.admin_api: the room list on /admin and the
start/stop actions.

7.2: start and stop change the RoomWorker's state; here the RoomWorker is a
mock, so these tests only check that the routes call through to it (and are
protected by the admin cookie), not the pipeline behind it.

Fix round 1 additions:
  - every state-changing POST also requires the X-Glosa-Admin header
    (CSRF: a plain <form> can't send it) -- `CSRF` below, sent on every
    start/stop call so the pre-existing assertions keep testing what they
    always tested; a dedicated test covers the header's absence;
  - rooms are keyed (both in app.state.workers and in the rendered
    data-room-id) by the RoomWorker's config *id*, not its view()'s *slug*
    -- the mock below deliberately gives each room a different id and slug
    so a regression to keying/rendering by slug would fail;
  - RoomWorker.start() raising ValueError (no configured source) becomes a
    409 with the error text, not a 500;
  - the admin-only RoomStatus.detail text is shown on the page;
  - the rendered strip/status classes are asserted per RoomStatus.state.

The admin cookie is set on the TestClient's own cookie jar
(``client.cookies.set``), never passed per-request: httpx deprecates (and
``-W error`` then fails on) a per-request ``cookies=`` argument.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.config import Settings
from glosa.models import RoomStatus
from glosa.web import admin_api
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

STATIC_DIR = Path(admin_api.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-pw"
CSRF = {"X-Glosa-Admin": "1"}


def _settings(**overrides) -> Settings:
    values: dict = dict(gemini_api_key="unused", admin_password=ADMIN_PASSWORD)
    values.update(overrides)
    return Settings(**values)


def _status(**overrides) -> RoomStatus:
    values = dict(
        state="idle", level_db=-60.0, latency_p50_s=None, quality=None,
        cost_usd=0.0, talk_id=None, detail="no talk in progress",
    )
    values.update(overrides)
    return RoomStatus(**values)


def _worker(room_id: str, slug: str, name: str, *, now: dict | None = None, status: RoomStatus | None = None) -> MagicMock:
    worker = MagicMock()
    worker.room.id = room_id
    worker.view.return_value = {"slug": slug, "name": name, "langs": ["en", "es"], "now": now, "next": None}
    worker.status.return_value = status if status is not None else _status()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()
    return worker


def _make_app(workers: dict | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {}
    app.state.admin_secret = new_admin_secret()
    app.state.sessions_valid_after = 0.0
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_api.router)
    app.include_router(admin_api.api_router)
    return app


def _client(app: FastAPI, *, authenticated: bool = True) -> TestClient:
    client = TestClient(app)
    if authenticated:
        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
    return client


# ---- GET /admin: the room list ---------------------------------------------


def test_admin_lists_rooms_with_state_and_talk() -> None:
    w1 = _worker(
        "room-1", "slug-1", "Sala Uno",
        now={"talk_id": "t1", "title": "Charla X", "speakers": [], "language": "en"},
        status=_status(state="green", talk_id="t1"),
    )
    w2 = _worker("room-2", "slug-2", "Sala Dos", status=_status(state="idle"))
    client = _client(_make_app(workers={"room-1": w1, "room-2": w2}))

    html = client.get("/admin").text

    assert "Sala Uno" in html and "Sala Dos" in html
    assert "Charla X" in html
    assert 'data-room-row' in html and 'data-room-id="room-1"' in html and 'data-room-id="room-2"' in html
    assert 'data-action="start"' in html
    assert 'data-action="stop"' in html


def test_admin_keys_rooms_by_id_not_slug() -> None:
    # id and slug deliberately differ: only "id" (never "slug-only") may
    # appear as data-room-id, and start/stop must be reachable at the id.
    worker = _worker("room-id-1", "totally-different-slug", "Sala Uno")
    client = _client(_make_app(workers={"room-id-1": worker}))

    html = client.get("/admin").text
    assert 'data-room-id="room-id-1"' in html
    assert 'data-room-id="totally-different-slug"' not in html

    response = client.post("/api/admin/rooms/room-id-1/start", headers=CSRF)
    assert response.status_code == 200
    worker.start.assert_awaited_once_with()


@pytest.mark.parametrize(
    "state, css",
    [("green", "live"), ("yellow", "degraded"), ("red", "down"), ("idle", "idle")],
)
def test_admin_renders_the_state_class_per_room_status(state: str, css: str) -> None:
    worker = _worker("r1", "r1", "Sala Uno", status=_status(state=state))
    client = _client(_make_app(workers={"r1": worker}))

    html = client.get("/admin").text

    assert f'strip strip--{css}"' in html
    assert f'status status--{css}"' in html
    assert f">{state}<" in html


def test_admin_shows_the_admin_only_raw_status_detail() -> None:
    worker = _worker("r1", "r1", "Sala Uno", status=_status(state="red", detail="source is down: ffmpeg exited 5 times"))
    client = _client(_make_app(workers={"r1": worker}))

    html = client.get("/admin").text

    assert "source is down: ffmpeg exited 5 times" in html


def test_admin_page_loads_the_design_system_and_its_own_js() -> None:
    client = _client(_make_app())

    html = client.get("/admin").text

    assert 'href="/static/css/glosa.css"' in html
    assert 'src="/static/js/admin.js"' in html
    assert client.get("/static/js/admin.js").status_code == 200


# ---- POST /api/admin/rooms/{id}/start, /stop -------------------------------


def test_start_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "r1", "Sala Uno", status=_status(state="green", talk_id="free-r1", cost_usd=0.01))
    client = _client(_make_app(workers={"r1": worker}))

    response = client.post("/api/admin/rooms/r1/start", headers=CSRF)

    assert response.status_code == 200
    worker.start.assert_awaited_once_with()
    assert response.json()["room"]["state"] == "green"
    assert response.json()["room"]["talk_id"] == "free-r1"


def test_stop_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}))

    response = client.post("/api/admin/rooms/r1/stop", headers=CSRF)

    assert response.status_code == 200
    worker.stop.assert_awaited_once_with()
    worker.start.assert_not_awaited()


def test_start_translates_a_missing_source_into_a_409() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    worker.start.side_effect = ValueError("room 'r1' has no audio source")
    client = _client(_make_app(workers={"r1": worker}))

    response = client.post("/api/admin/rooms/r1/start", headers=CSRF)

    assert response.status_code == 409
    assert "no audio source" in response.json()["detail"]


def test_start_and_stop_require_the_admin_cookie() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}), authenticated=False)

    start = client.post("/api/admin/rooms/r1/start", headers=CSRF)
    stop = client.post("/api/admin/rooms/r1/stop", headers=CSRF)

    assert start.status_code == 401 and stop.status_code == 401
    worker.start.assert_not_awaited()
    worker.stop.assert_not_awaited()


def test_start_with_a_bad_cookie_is_401() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}), authenticated=False)
    client.cookies.set(COOKIE_NAME, "garbage")

    response = client.post("/api/admin/rooms/r1/start", headers=CSRF)

    assert response.status_code == 401
    worker.start.assert_not_awaited()


@pytest.mark.parametrize(
    "bad_issued_at", ["1" + "0" * 400, "9" * 5000, ""], ids=["400-digit", "5000-digit", "empty"]
)
def test_start_with_a_crafted_cookie_is_401_not_500(bad_issued_at: str) -> None:
    # Fix round 2, #1: an unbounded issued_at used to crash verify_session
    # (OverflowError / ValueError), which FastAPI turned into a 500 here.
    worker = _worker("r1", "r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}), authenticated=False)
    client.cookies.set(COOKIE_NAME, f"{bad_issued_at}.deadbeef")

    response = client.post("/api/admin/rooms/r1/start", headers=CSRF)

    assert response.status_code == 401
    worker.start.assert_not_awaited()


def test_start_and_stop_require_the_csrf_header() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}))

    start = client.post("/api/admin/rooms/r1/start")
    stop = client.post("/api/admin/rooms/r1/stop")

    assert start.status_code == 403 and stop.status_code == 403
    worker.start.assert_not_awaited()
    worker.stop.assert_not_awaited()


def test_unknown_room_is_404() -> None:
    client = _client(_make_app(workers={}))

    start = client.post("/api/admin/rooms/nope/start", headers=CSRF)
    stop = client.post("/api/admin/rooms/nope/stop", headers=CSRF)

    assert start.status_code == 404 and stop.status_code == 404


# ---- integration: create_app() actually mounts /admin ---------------------


async def test_create_app_mounts_admin(tmp_path: Path) -> None:
    settings = _settings(
        engine_mode="fake",
        fake_fixture=str(Path(__file__).resolve().parents[2] / "samples" / "fixtures" / "lt_en.jsonl"),
        db_path=str(tmp_path / "glosa.db"),
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
