"""Public API: the room list, the SSE caption stream and the health check.

  - ``GET /api/rooms``: every room (config.yaml order) as the audience pages
    see it (``rooms_view()``: slug, name, langs, now, next) plus a public
    ``status``: ``state``, ``talk_id`` and a fixed ``detail`` per state
    (Ruling 29). The raw detail (ffmpeg or API errors, which can quote a
    source URL with its credentials), the level, latency and cost stay in
    ``RoomWorker.status()`` for the admin API. Room tokens are never listed.
    In ``audience_mode: qr_only`` this returns an empty list -- Ruling 56:
    nothing public may reveal a room's slug->token mapping, and a listed
    slug plus ``/qr/{slug}``'s public_token used to be exactly that chain.
  - ``GET /api/stream/{slug}/{lang}``: SSE of CaptionMsg for one room and
    language. ``lang`` must be one the room speaks or translates to
    (``RoomWorker.stream_langs()``), else 404 before the bus is touched, so
    anonymous requests cannot create bus tracks. The bus replays its buffer
    first; ``Last-Event-ID`` (header, what EventSource sends when it
    reconnects) or ``?lastEventId=`` resumes after that id. In ``qr_only``
    mode the ``slug`` path segment must be the room's ``public_token``
    instead (same rule as ``pages.room_page()``'s ``/s/{slug}`` vs.
    ``/s/{token}``) -- otherwise anyone who knows or guesses a room's slug
    could read its captions without ever having the token.
  - ``GET /healthz``.

create_app() (glosa/web/app.py) provides ``app.state.workers`` (room id ->
RoomWorker), ``app.state.bus`` and ``app.state.settings``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from glosa.models import RoomStatus
from glosa.room import RoomWorker
from glosa.web.sse import sse_response

router = APIRouter()

PUBLIC_DETAIL = {
    "green": "live",
    "yellow": "degraded",
    "red": "captions unavailable",
    "idle": "no talk in progress",
}


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/api/rooms")
async def rooms(request: Request) -> list[dict]:
    if _audience_mode(request) == "qr_only":
        return []
    return [w.view() | {"status": public_status(w.status())} for w in _workers(request)]


@router.get("/api/stream/{slug}/{lang}")
async def stream(slug: str, lang: str, request: Request):
    qr_only = _audience_mode(request) == "qr_only"
    worker = _resolve_worker(_workers(request), slug, qr_only)
    if worker is None or lang not in worker.stream_langs():
        raise HTTPException(status_code=404)
    last_event_id = _last_event_id(request)
    return sse_response(request.app.state.bus.subscribe(worker.room.id, lang, last_event_id))


def public_status(status: RoomStatus) -> dict:
    """What anyone may see of a room's status (Ruling 29)."""
    return {"state": status.state, "talk_id": status.talk_id, "detail": PUBLIC_DETAIL[status.state]}


def _workers(request: Request) -> list[RoomWorker]:
    return list(request.app.state.workers.values())


def _audience_mode(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "audience_mode", "all") if settings is not None else "all"


def _resolve_worker(workers: list[RoomWorker], key: str, qr_only: bool) -> RoomWorker | None:
    """The same identifier rule ``pages.room_page()`` uses for ``/s/{slug}``
    vs. ``/s/{token}``: in ``qr_only`` mode a plain slug never resolves,
    only a room's ``public_token`` does; in ``all`` mode the slug is tried
    first and the token as a (harmless) fallback."""
    if not qr_only:
        worker = next((w for w in workers if w.room.slug == key), None)
        if worker is not None:
            return worker
    return next((w for w in workers if getattr(w.room, "public_token", None) == key), None)


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None
