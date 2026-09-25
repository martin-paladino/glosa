"""Public API: the room list, the SSE caption stream and the health check.

  - ``GET /api/rooms``: every room (config.yaml order) as the audience pages
    see it (``rooms_view()``: slug, name, langs, now, next) plus a public
    ``status``: ``state``, ``talk_id`` and a fixed ``detail`` per state
    (Ruling 29). The raw detail (ffmpeg or API errors, which can quote a
    source URL with its credentials), the level, latency and cost stay in
    ``RoomWorker.status()`` for the admin API. Room tokens are never listed.
  - ``GET /api/stream/{slug}/{lang}``: SSE of CaptionMsg for one room and
    language. ``lang`` must be one the room speaks or translates to
    (``RoomWorker.stream_langs()``), else 404 before the bus is touched, so
    anonymous requests cannot create bus tracks. The bus replays its buffer
    first; ``Last-Event-ID`` (header, what EventSource sends when it
    reconnects) or ``?lastEventId=`` resumes after that id.
  - ``GET /exports/{talk_id}/{lang}.{srt|vtt|txt}?version=live|corrected``
    (task-11r-brief.md Ruling 2): a finished talk's captions, rendered by
    glosa/exports.py from db.get_segments(talk_id, lang, version). 404 for
    an unknown talk/lang, an unsupported {fmt}, or ``version=corrected``
    before its export is "ready" (glosa.db Database.get_export_status --
    "live" always renders on the fly, no status to check). When
    ``Settings.exports_public`` is False, every version requires a valid
    admin session cookie (glosa.web.auth.is_authenticated): 401 without
    one. ``shift_s`` is the room's current LatencyTracker p50
    (RoomWorker.latency_p50(), >=10 samples) if there is one, else
    ``Settings.default_export_shift_s`` -- a room-wide estimate, not one
    recomputed per (possibly long-finished) talk. ``Content-Disposition:
    attachment`` names the file "slug-titulo-lang-version.ext"
    (glosa.exports.export_filename).
  - ``GET /healthz``.

create_app() (glosa/web/app.py) provides ``app.state.workers`` (room id ->
RoomWorker), ``app.state.bus``, ``app.state.db`` and ``app.state.settings``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from glosa.exports import CONTENT_TYPE, ExportSegment, export_filename, render
from glosa.models import RoomStatus
from glosa.room import RoomWorker
from glosa.web.auth import is_authenticated
from glosa.web.sse import sse_response

router = APIRouter()

PUBLIC_DETAIL = {
    "green": "live",
    "yellow": "degraded",
    "red": "captions unavailable",
    "idle": "no talk in progress",
}

_VERSIONS = ("live", "corrected")


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/api/rooms")
async def rooms(request: Request) -> list[dict]:
    return [w.view() | {"status": public_status(w.status())} for w in _workers(request)]


@router.get("/api/stream/{slug}/{lang}")
async def stream(slug: str, lang: str, request: Request):
    worker = next((w for w in _workers(request) if w.room.slug == slug), None)
    if worker is None or lang not in worker.stream_langs():
        raise HTTPException(status_code=404)
    last_event_id = _last_event_id(request)
    return sse_response(request.app.state.bus.subscribe(worker.room.id, lang, last_event_id))


@router.get("/exports/{talk_id}/{lang}.{fmt}")
async def export_file(talk_id: str, lang: str, fmt: str, request: Request, version: str = "live"):
    if fmt not in CONTENT_TYPE:
        raise HTTPException(status_code=404)
    if version not in _VERSIONS:
        raise HTTPException(status_code=422, detail=f"version must be one of {_VERSIONS}")
    db = request.app.state.db
    talk = await db.get_talk(talk_id)
    if talk is None or lang not in {talk.language, *talk.targets}:
        raise HTTPException(status_code=404)
    settings = request.app.state.settings
    if not settings.exports_public and not is_authenticated(request):
        raise HTTPException(status_code=401, detail="admin login required")
    if version == "corrected" and await db.get_export_status(talk_id, lang) != "ready":
        raise HTTPException(status_code=404)

    segments = await db.get_segments(talk_id, lang, version)
    export_segs = [ExportSegment(text=s.text, t_start=s.t_start, t_end=s.t_end) for s in segments]
    body = render(fmt, export_segs, shift_s=_shift_s(request, talk.room_id, settings))
    room_slug = _room_slug(request, talk.room_id)
    filename = export_filename(room_slug, talk.title, lang, version, fmt)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=body, media_type=CONTENT_TYPE[fmt], headers=headers)


def _shift_s(request: Request, room_id: str, settings) -> float:
    worker = request.app.state.workers.get(room_id)
    p50 = worker.latency_p50() if worker is not None else None
    return p50 if p50 is not None else settings.default_export_shift_s


def _room_slug(request: Request, room_id: str) -> str:
    worker = request.app.state.workers.get(room_id)
    return worker.room.slug if worker is not None else room_id


def public_status(status: RoomStatus) -> dict:
    """What anyone may see of a room's status (Ruling 29)."""
    return {"state": status.state, "talk_id": status.talk_id, "detail": PUBLIC_DETAIL[status.state]}


def _workers(request: Request) -> list[RoomWorker]:
    return list(request.app.state.workers.values())


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None
