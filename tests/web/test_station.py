"""Tests for glosa.web.station (Task 14a): the station_key/station_url
helpers (Ruling 38), the `/station/{room}` page, the `/ws/station/{room}`
capture WebSocket, the `/emitter/{room}` alias and the admin remote-reload
endpoint.

A minimal FastAPI app (mirroring tests/web/test_admin_api.py's pattern) is
built directly around glosa.web.station's routers, with mocked RoomWorkers
(only the attributes station.py actually reads) and a real StationHub
(glosa.audio.ingest), so the WebSocket framing/repacking and the hub
interactions are exercised for real -- only ffmpeg/RoomWorker's own pipeline
is out of scope here (that's tests/test_room.py's integration test).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from glosa.audio.ingest import CHUNK_BYTES, AudioIngest, EmitterIngest, StationHub
from glosa.clock import FakeClock
from glosa.config import RoomCfg, Settings
from glosa.web import station
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

STATIC_DIR = Path(station.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-station-pw"
CSRF = {"X-Glosa-Admin": "1"}


def _settings(**overrides) -> Settings:
    values: dict = dict(gemini_api_key="unused", admin_password=ADMIN_PASSWORD)
    values.update(overrides)
    return Settings(**values)


def _worker(
    room_id: str = "r1", slug: str | None = None, name: str = "Sala Uno", public_token: str | None = None
) -> MagicMock:
    worker = MagicMock()
    worker.room.id = room_id
    worker.room.slug = slug or room_id
    worker.room.name = name
    worker.room.public_token = public_token or f"tok-{room_id}"
    worker.langs.return_value = ["en", "es"]
    worker.view.return_value = {"now": None}
    return worker


def _room_config(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="glosa-room">(.*?)</script>', html, re.S)
    assert match, "expected a #glosa-room JSON script tag"
    return json.loads(match.group(1))


def _make_app(workers: dict | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {"r1": _worker()}
    app.state.station_hub = StationHub(FakeClock())
    app.state.admin_secret = new_admin_secret()
    app.state.session_epoch = 0
    app.state.branding = {"event_name": "Glosa demo", "logo_url": None}
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(station.router)
    app.include_router(station.api_router)
    return app


def _client(app: FastAPI, *, authenticated: bool = False) -> TestClient:
    client = TestClient(app)
    if authenticated:
        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
    return client


# ---- station_key / station_url (Ruling 38) ----------------------------------


def test_station_key_matches_the_ruling_38_formula() -> None:
    expected_mac_key = hashlib.sha256(b"glosa-station:" + ADMIN_PASSWORD.encode()).digest()
    expected = hmac.new(expected_mac_key, b"main-stage", hashlib.sha256).hexdigest()[:32]
    assert station.station_key(ADMIN_PASSWORD, "main-stage") == expected
    assert len(expected) == 32


def test_station_key_is_stable_and_distinct_per_room() -> None:
    a1 = station.station_key(ADMIN_PASSWORD, "room-a")
    a2 = station.station_key(ADMIN_PASSWORD, "room-a")
    b = station.station_key(ADMIN_PASSWORD, "room-b")
    assert a1 == a2  # stable: an unattended station survives a restart
    assert a1 != b  # distinct per room


def test_station_key_changes_with_the_admin_password() -> None:
    assert station.station_key("pw-one", "r1") != station.station_key("pw-two", "r1")


def test_valid_station_key_rejects_a_wrong_or_missing_key() -> None:
    good = station.station_key(ADMIN_PASSWORD, "r1")
    assert station.valid_station_key(ADMIN_PASSWORD, "r1", good) is True
    assert station.valid_station_key(ADMIN_PASSWORD, "r1", good + "0") is False
    assert station.valid_station_key(ADMIN_PASSWORD, "r1", "") is False
    assert station.valid_station_key(ADMIN_PASSWORD, "r1", None) is False


def test_station_url_embeds_a_valid_key() -> None:
    url = station.station_url("r1", ADMIN_PASSWORD)
    assert url.startswith("/station/r1?key=")
    key = url.split("key=", 1)[1]
    assert station.valid_station_key(ADMIN_PASSWORD, "r1", key) is True


# ---- ingest_factory_for -------------------------------------------------------


def test_ingest_factory_for_routes_emitter_rooms_to_the_hub() -> None:
    hub = StationHub(FakeClock())
    factory = station.ingest_factory_for(AudioIngest, hub, "r1")

    ingest = factory("emitter", "unused", True, FakeClock())

    assert isinstance(ingest, EmitterIngest)


def test_ingest_factory_for_falls_through_to_base_otherwise() -> None:
    hub = StationHub(FakeClock())
    calls = []

    def base(source_type, source_url, realtime, clock):
        calls.append((source_type, source_url, realtime))
        return "base-ingest"

    factory = station.ingest_factory_for(base, hub, "r1")

    assert factory("file", "clip.opus", True, FakeClock()) == "base-ingest"
    assert calls == [("file", "clip.opus", True)]


# ---- GET /station/{room} -----------------------------------------------------


def test_station_page_200_with_a_valid_key() -> None:
    client = _client(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "r1")

    response = client.get(f"/station/r1?key={key}")

    assert response.status_code == 200
    assert "Sala Uno" in response.text


def test_station_streambase_uses_the_room_public_token_not_the_slug() -> None:
    # B-I2: the station page's streamBase must resolve in qr_only too --
    # /api/stream/{slug}/... 404s there (public_api._resolve_worker only
    # accepts a room's public_token in that mode), which left the stage
    # screens stuck retrying "Reconectando" forever. The token resolves in
    # "all" mode as well, so this is unconditional, not mode-dependent.
    worker = _worker(public_token="tok-r1-secret")
    client = _client(_make_app(workers={"r1": worker}))
    key = station.station_key(ADMIN_PASSWORD, "r1")

    response = client.get(f"/station/r1?key={key}")

    assert response.status_code == 200
    config = _room_config(response.text)
    assert config["streamBase"] == "/api/stream/tok-r1-secret/"


def test_station_page_has_a_replaced_by_another_station_block() -> None:
    # B-I4: StationHub.connect closes the older socket with 4409 ("replaced
    # by another station") when a second client opens the same room. The
    # page needs a hidden block station.js can reveal instead of silently
    # reconnecting (which would just supersede the other client back,
    # forever) -- a clear message plus a "take over" control that
    # reconnects deliberately. No JS harness covers station.js (unlike
    # room.js/room_js_harness.js), so this is the template/i18n half of the
    # fix: the markup and hooks station.js needs are present and correctly
    # localized; the close-code branch itself is a code-reading check on
    # static/js/station.js below.
    client = _client(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "r1")

    response = client.get(f"/station/r1?key={key}", headers={"Accept-Language": "es"})

    html = response.text
    assert "data-replaced" in html
    assert "data-retake" in html
    assert "Esta estación se abrió en otro equipo" in html
    assert "Tomar el control" in html


def test_station_js_does_not_reconnect_on_4409_and_offers_a_take_over_button() -> None:
    # Code-reading check (see note above): station.js must special-case the
    # StationHub's 4409 close code by NOT calling its normal reconnect path,
    # and must wire a click on the take-over control to reconnect instead.
    js = (Path(station.__file__).parent / "static" / "js" / "station.js").read_text()
    assert "4409" in js
    assert "data-retake" in js


def test_station_js_retries_as_standby_every_10s_instead_of_sitting_dead() -> None:
    # Ruling 62 (round 2): a 4409 must not be a one-way trap any more --
    # station.js has to retry as a ?standby=1 attempt on a fixed 10 s
    # interval (not the growing backoff used for ordinary drops), so a
    # transient replacer (e.g. an admin's preview tab) leaving lets the real
    # station recover on its own. The URL-building/state-transition logic
    # itself is exercised for real under Node in test_station_js.py (this
    # file has no DOM/WebSocket harness); this is a code-reading check that
    # the runtime wiring calls into it.
    js = (Path(station.__file__).parent / "static" / "js" / "station.js").read_text()
    assert "standby=1" in js or '"standby"' in js
    assert "10000" in js  # STANDBY_RETRY_MS
    assert "scheduleStandbyRetry" in js
    assert "stationWsUrl" in js and "nextStationAction" in js


def test_station_page_sends_cache_control_no_store() -> None:
    # B-Minor #8: the station page embeds the station key in its JSON
    # config (config.wsUrl's ?key=...), same as the admin panel embedding
    # station links -- admin_api.py's pages already send this header
    # (_PAGE_HEADERS); the station page didn't.
    client = _client(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "r1")

    response = client.get(f"/station/r1?key={key}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_station_page_403_without_a_key() -> None:
    client = _client(_make_app())
    assert client.get("/station/r1").status_code == 403


def test_station_page_403_with_a_wrong_key() -> None:
    client = _client(_make_app())
    assert client.get("/station/r1?key=not-the-right-one").status_code == 403


def test_station_page_404_for_an_unknown_room() -> None:
    client = _client(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "no-such-room")
    assert client.get(f"/station/no-such-room?key={key}").status_code == 404


# ---- GET /emitter/{room} alias -----------------------------------------------


def test_emitter_alias_redirects_to_the_station_url_when_authenticated() -> None:
    client = _client(_make_app(), authenticated=True)

    response = client.get("/emitter/r1", follow_redirects=False)

    assert response.status_code == 303
    key = station.station_key(ADMIN_PASSWORD, "r1")
    assert response.headers["location"] == f"/station/r1?key={key}"


def test_emitter_alias_redirects_to_login_when_not_authenticated() -> None:
    client = _client(_make_app(), authenticated=False)
    response = client.get("/emitter/r1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# ---- WebSocket ----------------------------------------------------------------


def _ws_url(room_id: str = "r1") -> str:
    return f"/ws/station/{room_id}?key={station.station_key(ADMIN_PASSWORD, room_id)}"


def test_ws_rejects_a_missing_or_wrong_key_with_4401() -> None:
    client = TestClient(_make_app())
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/station/r1?key=wrong"):
            pass
    assert excinfo.value.code == 4401


def test_ws_rejects_an_unknown_room_with_4401() -> None:
    client = TestClient(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "ghost-room")
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws/station/ghost-room?key={key}"):
            pass
    assert excinfo.value.code == 4401


def test_ws_binary_frames_are_repacked_into_3200_byte_chunks() -> None:
    app = _make_app()
    client = TestClient(app)
    hub: StationHub = app.state.station_hub

    with client.websocket_connect(_ws_url()) as ws:
        # Two uneven frames (1000 + the rest) complete exactly one
        # CHUNK_BYTES frame of 0x01, then one CHUNK_BYTES frame of 0x02:
        # the server must repack, not just forward what arrived.
        ws.send_bytes(b"\x01" * 1000)
        ws.send_bytes(b"\x01" * (CHUNK_BYTES - 1000))
        ws.send_bytes(b"\x02" * CHUNK_BYTES)

    queue = hub.queue("r1")
    assert queue.qsize() == 2
    first = queue.get_nowait()
    second = queue.get_nowait()
    assert len(first) == CHUNK_BYTES and first == b"\x01" * CHUNK_BYTES
    assert len(second) == CHUNK_BYTES and second == b"\x02" * CHUNK_BYTES


def test_ws_a_partial_trailing_frame_is_held_back_not_dropped() -> None:
    app = _make_app()
    client = TestClient(app)
    hub: StationHub = app.state.station_hub

    with client.websocket_connect(_ws_url()) as ws:
        ws.send_bytes(b"\x09" * (CHUNK_BYTES + 100))  # one full chunk + a 100-byte remainder

    assert hub.queue("r1").qsize() == 1  # the 100-byte remainder was not pushed as a short chunk


def test_ws_second_connection_replaces_the_first_with_4409() -> None:
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect(_ws_url()) as ws1:
        with client.websocket_connect(_ws_url()) as ws2:
            ws2.send_bytes(b"\x00" * CHUNK_BYTES)
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws1.receive_bytes()
            assert excinfo.value.code == 4409
        assert app.state.station_hub.info("r1").connected is False
    # closing the (already-superseded) ws1 context must not un-set that.


def test_ws_standby_connection_is_rejected_with_4409_while_active_and_active_keeps_working() -> None:
    """Ruling 62: a ?standby=1 attempt is closed with 4409 at once while a
    station is already active -- and that active station is never touched,
    unlike a normal second connection (which always replaces it)."""
    app = _make_app()
    client = TestClient(app)
    hub: StationHub = app.state.station_hub

    with client.websocket_connect(_ws_url()) as ws1:
        ws1.send_json({"type": "hello", "device": "Focusrite Scarlett 2i2", "version": 1})
        assert ws1.receive_json() == {"type": "ack"}

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(_ws_url() + "&standby=1") as ws2:
                ws2.receive_bytes()
        assert excinfo.value.code == 4409

        # the active station was never touched by the rejected standby
        # attempt: same device, still connected, still able to stream.
        assert hub.info("r1").connected is True
        assert hub.info("r1").device == "Focusrite Scarlett 2i2"
        ws1.send_bytes(b"\x00" * CHUNK_BYTES)

    assert hub.queue("r1").qsize() == 1


def test_ws_standby_connection_is_accepted_when_no_station_is_active() -> None:
    """Ruling 62: with nobody active, ?standby=1 is accepted exactly like a
    normal connect (this is how the real station recovers on its own once
    a transient replacer -- e.g. an admin's preview tab -- disconnects)."""
    app = _make_app()
    client = TestClient(app)
    hub: StationHub = app.state.station_hub

    with client.websocket_connect(_ws_url() + "&standby=1") as ws:
        ws.send_json({"type": "hello", "device": "Focusrite Scarlett 2i2", "version": 1})
        assert ws.receive_json() == {"type": "ack"}
        assert hub.info("r1").connected is True
        assert hub.info("r1").device == "Focusrite Scarlett 2i2"


def test_ws_hello_and_level_update_the_hub_and_are_acked() -> None:
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect(_ws_url()) as ws:
        ws.send_json({"type": "hello", "device": "Focusrite Scarlett 2i2", "version": 1})
        assert ws.receive_json() == {"type": "ack"}
        ws.send_json({"type": "level", "db": -23.4})
        assert ws.receive_json() == {"type": "ack"}

    info = app.state.station_hub.info("r1")
    assert info.device == "Focusrite Scarlett 2i2"
    assert info.level_db == -23.4


def test_ws_unrecognized_text_is_ignored_not_acked() -> None:
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect(_ws_url()) as ws:
        ws.send_json({"type": "nonsense"})
        ws.send_bytes(b"\x00" * CHUNK_BYTES)  # something that *does* get a reaction downstream
        # no ack was queued for the nonsense message: the next thing sent
        # to us, if anything, would only be a reaction to real data, and
        # binary frames get none -- so nothing should be waiting.
        ws.close()

    assert app.state.station_hub.queue("r1").qsize() == 1


# ---- admin: remote reload ----------------------------------------------------


def test_reload_endpoint_requires_admin_auth() -> None:
    client = _client(_make_app(), authenticated=False)
    response = client.post("/api/admin/rooms/r1/station/reload", headers=CSRF)
    assert response.status_code == 401


def test_reload_endpoint_requires_the_csrf_header() -> None:
    client = _client(_make_app(), authenticated=True)
    response = client.post("/api/admin/rooms/r1/station/reload")
    assert response.status_code == 403


def test_reload_endpoint_reports_no_station_connected() -> None:
    client = _client(_make_app(), authenticated=True)
    response = client.post("/api/admin/rooms/r1/station/reload", headers=CSRF)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "sent": False}


def test_reload_endpoint_reaches_a_connected_station() -> None:
    app = _make_app()
    ws_client = TestClient(app)
    admin_client = _client(app, authenticated=True)

    with ws_client.websocket_connect(_ws_url()) as ws:
        response = admin_client.post("/api/admin/rooms/r1/station/reload", headers=CSRF)
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "sent": True}
        assert ws.receive_json() == {"type": "reload"}


def test_reload_endpoint_404_for_an_unknown_room() -> None:
    client = _client(_make_app(), authenticated=True)
    response = client.post("/api/admin/rooms/ghost/station/reload", headers=CSRF)
    assert response.status_code == 404


# ---- create_app() wiring: source_type "emitter" end to end -------------------


def test_create_app_wires_an_emitter_room_to_the_station_hub(tmp_path: Path) -> None:
    """glosa/web/app.py's own wiring (ingest_factory_for + station_hub, the
    station router), exercised through the real create_app() -- everything
    else above uses a minimal app around glosa.web.station directly."""
    settings = Settings(
        gemini_api_key="unused",
        admin_password=ADMIN_PASSWORD,
        engine_mode="fake",
        db_path=str(tmp_path / "glosa.db"),
        rooms=[
            RoomCfg(
                id="r1", name="Sala Uno", source_type="emitter", source_url="r1",
                language="en", default_targets=["es"],
            )
        ],
    )
    app = create_app(settings)

    with TestClient(app) as client:
        assert isinstance(app.state.station_hub, StationHub)
        key = station.station_key(ADMIN_PASSWORD, "r1")

        page = client.get(f"/station/r1?key={key}")
        assert page.status_code == 200
        assert "Sala Uno" in page.text

        with client.websocket_connect(f"/ws/station/r1?key={key}") as ws:
            ws.send_bytes(b"\x00" * CHUNK_BYTES)
            ws.send_json({"type": "hello", "device": "Test Mic", "version": 1})
            assert ws.receive_json() == {"type": "ack"}

        info = app.state.station_hub.info("r1")
        assert info.device == "Test Mic"
        # The pushed frame was already drained: a real RoomWorker's audio
        # loop (auto-started at boot, has_source=True) is actively consuming
        # it through EmitterIngest, which is the wiring this test is for.
        assert app.state.station_hub.queue("r1").qsize() == 0
        assert "station:" in app.state.workers["r1"].status().detail


# ---- fix round 1: Referrer-Policy (review #1) ---------------------------------


def test_referrer_policy_header_and_meta_on_the_station_page(tmp_path: Path) -> None:
    """The station URL carries a stable secret (?key=...) in its own
    address; base.html loads Google Fonts cross-origin, so without a
    referrer policy that secret would leak via the Referer header. Both
    the response header (create_app()'s middleware -- catches every
    route, HTML or not) and the meta tag (base.html -- every page that
    extends it) must be present."""
    settings = Settings(
        gemini_api_key="unused",
        admin_password=ADMIN_PASSWORD,
        engine_mode="fake",
        db_path=str(tmp_path / "glosa.db"),
        rooms=[
            RoomCfg(
                id="r1", name="Sala Uno", source_type="emitter", source_url="r1",
                language="en", default_targets=["es"],
            )
        ],
    )
    app = create_app(settings)

    with TestClient(app) as client:
        key = station.station_key(ADMIN_PASSWORD, "r1")
        response = client.get(f"/station/r1?key={key}")

        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "same-origin"
        assert '<meta name="referrer" content="same-origin">' in response.text


def test_station_page_follows_the_configured_interface_language() -> None:
    client = _client(_make_app())
    key = station.station_key(ADMIN_PASSWORD, "r1")

    html = client.get(f"/station/r1?key={key}", headers={"Accept-Language": "en-US,en;q=0.9"}).text

    assert '<html lang="es"' in html  # Settings.ui_language defaults to "es" (Ruling 63)
