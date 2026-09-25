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
  - ``GET /api/summary/{slug}/{lang}`` (Task 17, "¿Qué me perdí?"): the
    latest summary glosa/summary.py's SummaryScheduler built for this room
    and language -- ``{"talk_id", "generated_at", "bullets"}`` or 404 when
    there is none yet (or ``lang`` is not one of ``stream_langs()``). Same
    slug/token rule as the stream route above (``_resolve_worker``).
  - ``GET /exports/{talk_id}/{lang}.{srt|vtt|txt}?version=live|corrected``
    (task-11r-brief.md Ruling 2): a finished talk's captions, rendered by
    glosa/exports.py from db.get_segments(talk_id, lang, version). 404 for
    an unknown talk/lang, an unsupported {fmt}, or ``version=corrected``
    before its export is "ready" (glosa.db Database.get_export_status --
    "live" always renders on the fly, no status to check). An anonymous
    caller (no valid admin session, glosa.web.auth.is_authenticated) gets
    401 when ``audience_mode: qr_only`` (regardless of ``exports_public`` --
    a talk id is guessable from public agenda fields, and this mode's whole
    point is that nothing public works without the room token), when
    ``Settings.exports_public`` is False, or when the talk's status isn't
    ``"done"`` yet (the one in progress is not public just because its id
    can be guessed). An authenticated admin session always works. ``shift_s``
    is the room's current LatencyTracker p50
    (RoomWorker.latency_p50(), >=10 samples) if there is one, else
    ``Settings.default_export_shift_s``; a glossary-engine talk always uses
    ``exports.GLOSSARY_SHIFT_S`` (its captions are stored at source-cut
    time, so only the transcription lag applies) -- a room-wide estimate, not one
    recomputed per (possibly long-finished) talk. ``Content-Disposition:
    attachment`` names the file "slug-titulo-lang-version.ext"
    (glosa.exports.export_filename).
  - ``GET /healthz``.

create_app() (glosa/web/app.py) provides ``app.state.workers`` (room id ->
RoomWorker), ``app.state.bus``, ``app.state.db``, ``app.state.settings`` and
``app.state.summaries`` (Task 17: glosa.summary.SummaryStore).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from glosa.exports import CONTENT_TYPE, GLOSSARY_SHIFT_S, ExportSegment, export_filename, render
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


@router.get("/api/summary/{slug}/{lang}")
async def summary(slug: str, lang: str, request: Request) -> dict:
    qr_only = _audience_mode(request) == "qr_only"
    worker = _resolve_worker(_workers(request), slug, qr_only)
    if worker is None or lang not in worker.stream_langs():
        raise HTTPException(status_code=404)
    result = request.app.state.summaries.get(worker.room.id, lang)
    # Only the current talk's summary: the scheduler notices a talk change
    # on its next tick (up to SUMMARY_EVERY_S later), so a stored summary of
    # the previous talk must never be served under the new one.
    talk = worker.talk
    if result is None or talk is None or result.talk_id != talk.id:
        raise HTTPException(status_code=404)
    return {"talk_id": result.talk_id, "generated_at": result.generated_at, "bullets": result.bullets}


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
    # B-I1: in qr_only, exports always require an admin session -- talk ids
    # are guessable from public agenda fields, and exports_public must not
    # bypass the room-token boundary that mode is built around. In any
    # mode, an anonymous caller only gets a "done" talk: the one in
    # progress is not public just because its id can be guessed.
    if not is_authenticated(request):
        qr_only = _audience_mode(request) == "qr_only"
        if qr_only or not settings.exports_public or talk.status != "done":
            raise HTTPException(status_code=401, detail="admin login required")
    if version == "corrected" and await db.get_export_status(talk_id, lang) != "ready":
        raise HTTPException(status_code=404)

    segments = await db.get_segments(talk_id, lang, version)
    export_segs = [ExportSegment(text=s.text, t_start=s.t_start, t_end=s.t_end) for s in segments]
    shift_s = GLOSSARY_SHIFT_S if talk.engine == "glossary" else _shift_s(request, talk.room_id, settings)
    body = render(fmt, export_segs, shift_s=shift_s)
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
