"""The room station (Task 14a): a mini PC's page for one room, replacing the
event's RustDesk-and-a-SaaS-tab setup (docs/field-notes.md). It captures the
audio desk's mic feed in the browser, streams it to the server over a
WebSocket, and shows the room's own captions full screen for the stage
screens -- all unattended, all day, reloadable from the admin panel.

  - ``GET /station/{room_id}?key=<station_key>``: the station page (this
    router's ``templates`` -- ``glosa/web/templates/station.html``, which
    loads ``static/js/station.js`` for capture and ``static/js/room.js``
    unmodified, with the same markup it expects, for the stage caption
    render (docs/design/README.md §5) -- no duplicated rendering logic).
    404 for an unknown room, 403 for a missing/wrong ``key``.
  - ``GET /ws/station/{room_id}?key=...``: the capture WebSocket. Binary
    frames are PCM s16le 16 kHz mono, any size; re-packed here into
    ``CHUNK_BYTES`` (3200, 100 ms) frames pushed onto
    ``app.state.station_hub``'s queue for this room -- ``EmitterIngest``
    (glosa/audio/ingest.py) reads from there, through the same ``Ingest``
    interface as ``AudioIngest`` (glosa/room.py). Text frames are JSON
    control messages: the station sends ``{"type": "hello", "device", ...}``
    once and ``{"type": "level", "db": ...}` once a second; the server
    replies ``{"type": "ack"}`` and, for a remote reload, sends
    ``{"type": "reload"}`` unprompted. A missing/wrong ``key`` closes the
    handshake with code 4401 (Ruling 38); a second connection for the same
    room replaces the first, which is closed with 4409 (StationHub.connect).
  - ``POST /api/admin/rooms/{room_id}/station/reload`` (``api_router``:
    ``require_admin`` + ``require_csrf_header``, glosa/web/auth.py -- same
    dependencies as ``glosa/web/admin_api.py``'s ``api_router``, a separate
    instance so this task doesn't touch that file): sends ``reload`` to the
    room's connected station, if any ("F5 remoto" -- no more RustDesk).
  - ``GET /emitter/{room_id}``: alias for the plan's `/emitter/{room}`
    (task-14-brief.md). Admin-cookie gated like ``/admin``; redirects to
    this room's ``station_url()`` so a logged-in admin doesn't need to know
    the key by hand.

``station_key``/``station_url``/``valid_station_key`` and
``ingest_factory_for`` are this task's exported helpers: ``create_app()``
(glosa/web/app.py) uses ``ingest_factory_for`` to route ``source_type ==
"emitter"`` rooms to ``EmitterIngest`` through the shared ``StationHub``, and
Task 12's admin panel will use ``station_url()`` to link each room's station
from the drawer.

``app.state`` this module reads: ``workers`` (room id -> RoomWorker),
``settings``, ``station_hub`` (the one ``StationHub``, created and stored by
``create_app()``), ``branding``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from glosa.audio.ingest import CHUNK_BYTES, EmitterIngest, StationHub
from glosa.i18n import STRINGS, Lang, detect_lang, endonym, join_names, lang_name, t
from glosa.room import IngestFactory, RoomWorker
from glosa.web.auth import is_authenticated, require_admin, require_csrf_header

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Ruling 38: hex digest length the station key is truncated to.
STATION_KEY_LEN = 32
# Close codes (private-use range, RFC 6455 §7.4.2): an invalid/missing key,
# and a station superseded by a newer connection for the same room.
WS_INVALID_KEY = 4401
WS_SUPERSEDED = 4409
# A station's device label: bounded so a hostile/buggy client can't grow the
# hub's per-room state without limit.
MAX_DEVICE_LEN = 200

router = APIRouter()
# Same dependencies as glosa/web/admin_api.py's api_router (require_admin,
# then require_csrf_header), a separate instance so this task's own router
# stays out of that file (Task 12 owns it).
api_router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin), Depends(require_csrf_header)])


# ---- station key (Ruling 38) ------------------------------------------------


def station_key(admin_password: str, room_id: str) -> str:
    """``hmac_sha256(key=sha256("glosa-station:" + ADMIN_PASSWORD),
    msg=room_id)``, hex, the first ``STATION_KEY_LEN`` characters. Stable
    across a restart (an unattended station keeps working); revoked by
    changing ``ADMIN_PASSWORD``."""
    mac_key = hashlib.sha256(f"glosa-station:{admin_password}".encode("utf-8")).digest()
    return hmac.new(mac_key, room_id.encode("utf-8"), hashlib.sha256).hexdigest()[:STATION_KEY_LEN]


def valid_station_key(admin_password: str, room_id: str, candidate: str | None) -> bool:
    """Constant-time comparison, always on UTF-8 bytes (same discipline as
    glosa/web/auth.py's session/password checks)."""
    expected = station_key(admin_password, room_id)
    return hmac.compare_digest(expected.encode("utf-8"), (candidate or "").encode("utf-8"))


def station_url(room_id: str, admin_password: str) -> str:
    """The station's stable URL (path + query): ``/station/{room_id}?key=..``.
    Task 12's admin drawer links here; ``/emitter/{room_id}`` below
    redirects here too."""
    return f"/station/{room_id}?key={station_key(admin_password, room_id)}"


# ---- ingest wiring (glosa/web/app.py) ---------------------------------------


def ingest_factory_for(base: IngestFactory, hub: StationHub, room_id: str) -> IngestFactory:
    """One room's ``IngestFactory`` for ``RoomWorker``: ``source_type ==
    "emitter"`` reads from ``hub`` instead of running ffmpeg; anything else
    falls through to ``base`` (glosa/web/app.py passes ``AudioIngest``)."""

    def factory(source_type: str, source_url: str, realtime: bool, clock: Any):
        if source_type == "emitter":
            return EmitterIngest(hub, room_id, clock)
        return base(source_type, source_url, realtime, clock)

    return factory


# ---- page -------------------------------------------------------------------


@router.get("/station/{room_id}", include_in_schema=False)
def station_page(room_id: str, request: Request, key: str = ""):
    worker = _worker_or_none(request, room_id)
    if worker is None:
        raise HTTPException(status_code=404)
    settings = request.app.state.settings
    if not valid_station_key(settings.admin_password, room_id, key):
        raise HTTPException(status_code=403, detail="missing or invalid station key")
    ui = detect_lang(request.headers.get("accept-language"))
    branding = getattr(request.app.state, "branding", None) or {}
    now = (worker.view() or {}).get("now")
    context = {
        "ui": ui,
        "tr": lambda name: t(name, ui),
        "event_name": branding.get("event_name") or "Glosa",
        "logo_url": branding.get("logo_url"),
        "room": {"id": worker.room.id, "slug": worker.room.slug, "name": worker.room.name},
        "now": (now | {"speakers_text": join_names(list(now.get("speakers") or []), ui)}) if now else None,
        "config": _room_js_config(worker, ui, key),
    }
    return templates.TemplateResponse(request, "station.html", context)


@router.get("/emitter/{room_id}", include_in_schema=False)
def emitter_alias(room_id: str, request: Request):
    """`/emitter/{room}` of the original plan (task-14-brief.md): this
    page, at its keyed URL. Gated like `/admin` (the session cookie) rather
    than the station key itself, so a logged-in admin can jump here without
    having to know or type the key."""
    if not is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    if _worker_or_none(request, room_id) is None:
        raise HTTPException(status_code=404)
    settings = request.app.state.settings
    return RedirectResponse(station_url(room_id, settings.admin_password), status_code=303)


def _room_js_config(worker: RoomWorker, ui: Lang, key: str) -> dict:
    """The ``#glosa-room`` JSON both ``static/js/room.js`` (stage captions;
    same shape ``glosa/web/pages.py``'s ``room_page()`` builds for
    `/s/{slug}` -- the station has one room and no forced/nav language
    switch, so this is a trimmed-down version, not an import from pages.py,
    kept independent on purpose: see the appended controller section on
    file ownership) and ``static/js/station.js`` (capture: ``wsUrl``, and
    ``i18n`` for its own station_* strings) read."""
    langs = worker.langs()
    now = (worker.view() or {}).get("now")
    default_lang = ui if ui in langs else (langs[0] if langs else ui)
    return {
        "slug": worker.room.slug,
        # B-I2: public_api._resolve_worker only accepts a room's slug in
        # "all" mode -- in qr_only, /api/stream/{slug}/... 404s. The
        # public_token resolves in both modes, so use it unconditionally
        # (the station is a stage screen, not the audience UI, so there's
        # no reason to prefer the prettier slug the way pages.py does).
        "streamBase": f"/api/stream/{worker.room.public_token}/",
        "wsUrl": f"/ws/station/{worker.room.id}?key={key}",
        "langs": langs,
        "defaultLang": default_lang,
        "forcedLang": None,
        "source": now["language"] if now else None,
        "talkId": now["talk_id"] if now else None,
        "endonyms": {code: endonym(code) for code in langs},
        "langNames": {code: lang_name(code, ui) for code in langs},
        "i18n": STRINGS[ui],
    }


def _worker_or_none(request: Request, room_id: str) -> RoomWorker | None:
    return request.app.state.workers.get(room_id)


# ---- websocket ----------------------------------------------------------------


@router.websocket("/ws/station/{room_id}")
async def station_ws(websocket: WebSocket, room_id: str) -> None:
    workers = websocket.app.state.workers
    settings = websocket.app.state.settings
    hub: StationHub = websocket.app.state.station_hub
    key = websocket.query_params.get("key")

    if room_id not in workers or not valid_station_key(settings.admin_password, room_id, key):
        await websocket.close(code=WS_INVALID_KEY)
        return

    await websocket.accept()
    generation = await hub.connect(room_id, websocket)
    buffer = bytearray()
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                buffer += data
                while len(buffer) >= CHUNK_BYTES:
                    hub.push_audio(room_id, bytes(buffer[:CHUNK_BYTES]))
                    del buffer[:CHUNK_BYTES]
                continue
            text = message.get("text")
            if text is not None and _apply_control(hub, room_id, text):
                with contextlib.suppress(Exception):  # a race with a close: never worth crashing the loop
                    await websocket.send_json({"type": "ack"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(room_id, generation)


def _apply_control(hub: StationHub, room_id: str, text: str) -> bool:
    """Apply one control-frame JSON message (``hello``/``level``) to the
    hub; returns whether it was recognized (an ``ack`` is worth sending)."""
    try:
        msg = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(msg, dict):
        return False
    kind = msg.get("type")
    if kind == "hello":
        device = msg.get("device")
        hub.set_hello(room_id, device[:MAX_DEVICE_LEN] if isinstance(device, str) else None)
        return True
    if kind == "level":
        db = msg.get("db")
        if isinstance(db, (int, float)) and not isinstance(db, bool):
            hub.set_level(room_id, float(db))
            return True
    return False


# ---- admin: remote reload ("F5 remoto") -------------------------------------


@api_router.post("/rooms/{room_id}/station/reload")
async def reload_station(room_id: str, request: Request) -> dict:
    if room_id not in request.app.state.workers:
        raise HTTPException(status_code=404)
    hub: StationHub = request.app.state.station_hub
    sent = await hub.reload(room_id)
    return {"status": "ok", "sent": sent}
