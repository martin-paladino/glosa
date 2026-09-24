"""Tests for glosa.web.admin_api: the room list on /admin and the
start/stop actions.

7.2: start and stop change the RoomWorker's state; here the RoomWorker is a
mock, so these tests only check that the routes call through to it (and are
protected by the admin cookie), not the pipeline behind it.

The admin cookie is set on the TestClient's own cookie jar
(``client.cookies.set``), never passed per-request: httpx deprecates (and
``-W error`` then fails on) a per-request ``cookies=`` argument.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.config import Settings
from glosa.models import RoomStatus
from glosa.web import admin_api
from glosa.web.auth import COOKIE_NAME, sign_session

STATIC_DIR = Path(admin_api.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-pw"


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


def _worker(slug: str, name: str, *, now: dict | None = None, status: RoomStatus | None = None) -> MagicMock:
    worker = MagicMock()
    worker.view.return_value = {"slug": slug, "name": name, "langs": ["en", "es"], "now": now, "next": None}
    worker.status.return_value = status if status is not None else _status()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()
    return worker


def _make_app(workers: dict | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {}
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_api.router)
    return app


def _client(app: FastAPI, *, authenticated: bool = True) -> TestClient:
    client = TestClient(app)
    if authenticated:
        client.cookies.set(COOKIE_NAME, sign_session(ADMIN_PASSWORD))
    return client


# ---- GET /admin: the room list ---------------------------------------------


def test_admin_lists_rooms_with_state_and_talk() -> None:
    w1 = _worker(
        "r1", "Sala Uno",
        now={"talk_id": "t1", "title": "Charla X", "speakers": [], "language": "en"},
        status=_status(state="green", talk_id="t1"),
    )
    w2 = _worker("r2", "Sala Dos", status=_status(state="idle"))
    client = _client(_make_app(workers={"r1": w1, "r2": w2}))

    html = client.get("/admin").text

    assert "Sala Uno" in html and "Sala Dos" in html
    assert "Charla X" in html
    assert 'data-room-row' in html and 'data-room-id="r1"' in html and 'data-room-id="r2"' in html
    assert 'data-action="start"' in html
    assert 'data-action="stop"' in html


def test_admin_page_loads_the_design_system_and_its_own_js() -> None:
    client = _client(_make_app())

    html = client.get("/admin").text

    assert 'href="/static/css/glosa.css"' in html
    assert 'src="/static/js/admin.js"' in html
    assert client.get("/static/js/admin.js").status_code == 200


# ---- POST /api/admin/rooms/{id}/start, /stop -------------------------------


def test_start_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "Sala Uno", status=_status(state="green", talk_id="free-r1", cost_usd=0.01))
    client = _client(_make_app(workers={"r1": worker}))

    response = client.post("/api/admin/rooms/r1/start")

    assert response.status_code == 200
    worker.start.assert_awaited_once_with()
    assert response.json()["room"]["state"] == "green"
    assert response.json()["room"]["talk_id"] == "free-r1"


def test_stop_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}))

    response = client.post("/api/admin/rooms/r1/stop")

    assert response.status_code == 200
    worker.stop.assert_awaited_once_with()
    worker.start.assert_not_awaited()


def test_start_and_stop_require_the_admin_cookie() -> None:
    worker = _worker("r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}), authenticated=False)

    start = client.post("/api/admin/rooms/r1/start")
    stop = client.post("/api/admin/rooms/r1/stop")

    assert start.status_code == 401 and stop.status_code == 401
    worker.start.assert_not_awaited()
    worker.stop.assert_not_awaited()


def test_start_with_a_bad_cookie_is_401() -> None:
    worker = _worker("r1", "Sala Uno")
    client = _client(_make_app(workers={"r1": worker}), authenticated=False)
    client.cookies.set(COOKIE_NAME, "garbage")

    response = client.post("/api/admin/rooms/r1/start")

    assert response.status_code == 401
    worker.start.assert_not_awaited()


def test_unknown_room_is_404() -> None:
    client = _client(_make_app(workers={}))

    start = client.post("/api/admin/rooms/nope/start")
    stop = client.post("/api/admin/rooms/nope/stop")

    assert start.status_code == 404 and stop.status_code == 404
