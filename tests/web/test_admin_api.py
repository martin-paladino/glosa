"""Tests for glosa.web.admin_api: the production panel on /admin (Task 12:
"Sala de control" v2), its login page, and the start/stop actions.

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

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.captions.bus import CaptionBus
from glosa.clock import RealClock
from glosa.config import Settings
from glosa.models import RoomStatus, Talk
from glosa.web import admin_api
from glosa.web.admin_events import AdminEvents
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


NOW = datetime.now(timezone.utc)


def _talk(talk_id: str, room_id: str, title: str, *, start_min: float = -10, end_min: float = 30) -> Talk:
    return Talk(
        id=talk_id, room_id=room_id, title=title, speakers=["Ana Pérez"], language="en", targets=["es"],
        engine="fast", start=NOW + timedelta(minutes=start_min), end=NOW + timedelta(minutes=end_min),
        abstract="", tags=[], glossary=[], status="live", actual_start=NOW + timedelta(minutes=start_min),
        actual_end=None,
    )


def _worker(room_id: str, slug: str, name: str, *, talk: Talk | None = None, status: RoomStatus | None = None) -> MagicMock:
    worker = MagicMock()
    worker.room.id = room_id
    worker.room.slug = slug
    worker.room.name = name
    worker.talk = talk
    worker.language = "en"
    worker.has_source = True
    worker.view.return_value = {"slug": slug, "name": name, "langs": ["en", "es"], "now": None, "next": None}
    worker.status.return_value = status if status is not None else _status()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()
    return worker


def _autopilot(next_talk: Talk | None = None) -> MagicMock:
    autopilot = MagicMock()
    autopilot.set_mode = AsyncMock()
    autopilot.mode.return_value = "manual"
    autopilot.next_talk = AsyncMock(return_value=next_talk)
    return autopilot


def _db() -> MagicMock:
    db = MagicMock()
    db.cost_by_room = AsyncMock(return_value={})
    db.log_event = AsyncMock()
    return db


def _make_app(workers: dict | None = None, settings: Settings | None = None, *, branding: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {}
    app.state.autopilot = _autopilot()
    app.state.db = _db()
    app.state.clock = RealClock()
    app.state.bus = CaptionBus()
    app.state.admin_events = AdminEvents()
    app.state.admin_secret = new_admin_secret()
    app.state.session_epoch = 0
    if branding is not None:
        app.state.branding = branding
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_api.router)
    app.include_router(admin_api.api_router)
    return app


def _client(app: FastAPI, *, authenticated: bool = True) -> TestClient:
    client = TestClient(app)
    if authenticated:
        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
    return client


# ---- GET /admin: the panel ---------------------------------------------------


def _config(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="glosa-admin">(.*?)</script>', html, re.S)
    assert match, "the page embeds its config for admin.js"
    return json.loads(match.group(1))


def test_admin_renders_a_monitor_per_room_with_its_talk() -> None:
    w1 = _worker("room-1", "slug-1", "Sala Uno", talk=_talk("t1", "room-1", "Charla X"),
                 status=_status(state="green", talk_id="t1", detail="ok", level_db=-20.0))
    w2 = _worker("room-2", "slug-2", "Sala Dos", status=_status(state="idle"))
    client = _client(_make_app(workers={"room-1": w1, "room-2": w2}))

    html = client.get("/admin").text

    assert "Sala Uno" in html and "Sala Dos" in html
    assert 'data-monitor="room-1"' in html and 'data-monitor="room-2"' in html
    assert re.search(r'data-monitor="room-1" data-key="1"', html) and re.search(r'data-monitor="room-2" data-key="2"', html)
    assert _config(html)["state"]["rooms"][0]["talk"]["title"] == "Charla X"  # the drawer shows it


def test_admin_keys_rooms_by_id_not_slug() -> None:
    # id and slug deliberately differ: only "id" (never "slug-only") may
    # key a monitor, and start/stop must be reachable at the id.
    worker = _worker("room-id-1", "totally-different-slug", "Sala Uno")
    client = _client(_make_app(workers={"room-id-1": worker}))

    html = client.get("/admin").text
    assert 'data-monitor="room-id-1"' in html and 'data-track="room-id-1"' in html
    assert 'data-monitor="totally-different-slug"' not in html

    response = client.post("/api/admin/rooms/room-id-1/start", headers=CSRF)
    assert response.status_code == 200
    worker.start.assert_awaited_once_with()


@pytest.mark.parametrize(
    "status, css, action",
    [
        (_status(state="green", talk_id="t", detail="ok", level_db=-20.0), "live", None),
        (_status(state="yellow", talk_id="t", latency_p50_s=6.1, detail="latency 6.1s exceeds 5.0s"), "degraded", "reconnect"),
        (_status(state="red", talk_id="t", detail="source is down: ffmpeg exited 5 times"), "down", "restart"),
        (_status(state="idle"), "idle", None),
    ],
    ids=["live", "degraded", "down", "idle"],
)
def test_admin_renders_each_room_in_its_state(status: RoomStatus, css: str, action: str | None) -> None:
    talk = _talk("t", "r1", "Charla") if status.state != "idle" else None
    client = _client(_make_app(workers={"r1": _worker("r1", "r1", "Sala Uno", talk=talk, status=status)}))

    html = client.get("/admin", headers={"Accept-Language": "es"}).text

    assert f'class="monitor monitor--{css}" data-monitor="r1"' in html
    assert f'<i class="led led--{css}" data-m-led></i>' in html
    footer = re.search(r"<footer class=\"monitor__issue\" data-m-issue( hidden)?>(.*?)</footer>", html, re.S)
    assert footer is not None
    if action is None:
        assert footer.group(1) == " hidden"  # a healthy or idle room stays calm: no metrics, no keys
        assert 'class="attn attn--calm"' in html and "Todo en orden." in html
    else:
        assert footer.group(1) is None
        assert f'data-action="{action}" data-room-id="r1"' in footer.group(2)
        assert f"btn--{css}" in footer.group(2)  # the suggested key takes the state's colour
        assert 'class="attn"' in html and f'attn__row attn__row--{css}" data-attn-row="r1"' in html
    if css == "idle":
        assert re.search(r'<div class="monitor__wait" data-m-wait>', html)
        assert re.search(r'<div class="monitor__cc" data-m-cc lang="en" hidden>', html)


def test_admin_shows_the_admin_only_raw_status_detail() -> None:
    status = _status(state="red", talk_id="t", detail="source is down: ffmpeg exited 5 times")
    worker = _worker("r1", "r1", "Sala Uno", talk=_talk("t", "r1", "Charla"), status=status)
    client = _client(_make_app(workers={"r1": worker}))

    html = client.get("/admin", headers={"Accept-Language": "es"}).text

    assert "Fuente caída: ffmpeg exited 5 times." in html  # the Atención row
    assert _config(html)["state"]["rooms"][0]["status"]["detail"] == "source is down: ffmpeg exited 5 times"


def test_admin_page_has_the_hooks_admin_js_needs() -> None:
    client = _client(_make_app(workers={"r1": _worker("r1", "r1", "Sala Uno")}))

    html = client.get("/admin").text

    for hook in ("data-admin", "data-tally", "data-budget", "data-budget-bar", "data-admin-clock", "data-attn",
                 "data-attn-list", "data-wall", "data-log", "data-log-filter=\"alerts\"", "data-log-more",
                 "data-timeline", "data-track=\"r1\"", "data-now", "data-next-change", "data-drawer", "data-scrim",
                 "data-toast", 'data-tpl="room"', "data-d-start", "data-d-pick", "data-d-end", "data-d-reconnect",
                 "data-mode=\"auto\"", "data-d-level", "data-d-raw", "data-d-log"):
        assert hook in html, hook
    # Task 14's keys: a hole in the drawer, not wired yet.
    assert 'data-slot="station-reload" hidden' in html and 'data-slot="test-audio" hidden' in html
    config = _config(html)
    assert config["streamUrl"].startswith("/api/admin/stream?lang=")
    assert config["limits"] == {"latency": 5.0, "quality": 0.5, "level": -50.0}


def test_admin_page_has_the_agenda_editing_hooks() -> None:
    # Import and talk editing live in the same drawer, without native dialogs.
    client = _client(_make_app(workers={"r1": _worker("r1", "r1", "Sala Uno")}))

    html = client.get("/admin", headers={"Accept-Language": "es"}).text

    for hook in ("data-open-import", 'data-tpl="talk"', 'data-tpl="import"', "data-talk-form", 'name="glossary"',
                 'type="datetime-local" name="start"', "data-t-delete", "data-t-confirm", "data-import-form",
                 'type="file" name="file"', 'type="url" name="url"', "data-use-nerdearla", "data-i-result"):
        assert hook in html, hook
    assert "confirm(" not in html and "prompt(" not in html
    assert "nerdearla.com" in _config(html)["nerdearlaUrl"]
    assert client.get("/static/js/admin.js").text.count("window.confirm") == 0


def test_admin_page_speaks_the_browser_language_or_the_chosen_one() -> None:
    client = _client(_make_app(workers={"r1": _worker("r1", "r1", "Sala Uno")}))

    spanish = client.get("/admin", headers={"Accept-Language": "es-AR,es"}).text
    english = client.get("/admin", headers={"Accept-Language": "en-US,en"}).text
    chosen = client.get("/admin?lang=es", headers={"Accept-Language": "en"}).text

    assert '<html lang="es">' in spanish and "<b>Atención</b>" in spanish and "Registro" in spanish
    assert '<html lang="en">' in english and "<b>Attention</b>" in english and "Import agenda" in english
    assert '<html lang="es">' in chosen and _config(chosen)["i18n"]["log"] == "Registro"
    assert 'href="?lang=en"' in spanish  # the switch


def test_admin_page_shows_the_event_logo_next_to_the_mark() -> None:
    logo = "/static/branding/nerdearla/nerdearla-logo-bw.svg"
    app = _make_app(branding={"event_name": "Nerdearla 2026", "logo_url": logo})
    client = _client(app)

    html = client.get("/admin").text

    assert re.search(r'class="wordmark"[^>]*>glosa</a>\s*<img class="event-logo" src="' + re.escape(logo), html)
    assert "Nerdearla 2026" in html


def test_admin_pages_link_only_assets_that_exist() -> None:
    logo = "/static/branding/nerdearla/nerdearla-logo-bw.svg"
    app = _make_app(workers={"r1": _worker("r1", "r1", "Sala Uno")},
                    branding={"event_name": "Nerdearla 2026", "logo_url": logo})
    for path, authenticated in (("/admin", True), ("/admin/login", False)):
        client = _client(app, authenticated=authenticated)
        html = client.get(path).text
        assert 'href="/static/css/glosa.css"' in html and 'src="/static/js/admin.js"' in html
        refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
        assert logo in refs
        for ref in refs:
            assert client.get(ref).status_code == 200, f"{path} links a missing {ref}"


def test_admin_page_loads_the_design_system_and_its_own_js() -> None:
    client = _client(_make_app())

    html = client.get("/admin").text

    assert 'href="/static/css/glosa.css"' in html
    assert 'src="/static/js/admin.js"' in html
    assert client.get("/static/js/admin.js").status_code == 200


def test_admin_redirects_to_the_login_page_in_the_chosen_language() -> None:
    client = _client(_make_app(), authenticated=False)

    response = client.get("/admin?lang=en", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/admin/login?lang=en"


def test_the_login_page_is_the_panel_s_own() -> None:
    client = _client(_make_app(), authenticated=False)

    spanish = client.get("/admin/login", headers={"Accept-Language": "es"}).text
    english = client.get("/admin/login?lang=en").text

    assert 'class="login-page"' in spanish and 'class="masthead"' in spanish
    assert 'action="/admin/login"' in spanish and 'name="password" type="password"' in spanish
    assert ">Entrar</button>" in spanish and "data-admin-login-error" in spanish
    assert ">Log in</button>" in english and _config(english)["page"] == "login"


# ---- POST /api/admin/rooms/{id}/start, /stop -------------------------------


def test_start_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "r1", "Sala Uno", status=_status(state="green", talk_id="free-r1", cost_usd=0.01))
    app = _make_app(workers={"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/start", headers=CSRF)

    assert response.status_code == 200
    worker.start.assert_awaited_once_with()
    assert response.json()["room"]["state"] == "green"
    assert response.json()["room"]["talk_id"] == "free-r1"
    # Ruling 34: an operator's start takes the room off the autopilot
    app.state.autopilot.set_mode.assert_awaited_once_with("r1", "manual")
    assert response.json()["mode"] == "manual"


def test_stop_calls_the_worker_and_reports_its_status() -> None:
    worker = _worker("r1", "r1", "Sala Uno")
    app = _make_app(workers={"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/stop", headers=CSRF)

    assert response.status_code == 200
    worker.stop.assert_awaited_once_with()
    worker.start.assert_not_awaited()
    app.state.autopilot.set_mode.assert_awaited_once_with("r1", "manual")  # Ruling 34


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
