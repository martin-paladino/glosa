"""Public API: the room list, the SSE caption stream and the health check.

  - ``GET /api/rooms``: every room (config.yaml order) as the audience pages
    see it (``rooms_view()``: slug, name, langs, now, next) plus ``status``
    (RoomStatus). Room tokens are never listed.
  - ``GET /api/stream/{slug}/{lang}``: SSE of CaptionMsg for one room and
    language. The bus replays its buffer first; ``Last-Event-ID`` (header,
    what EventSource sends when it reconnects) or ``?lastEventId=`` resumes
    after that id.
  - ``GET /healthz``.

create_app() (glosa/web/app.py) provides ``app.state.workers`` (room id ->
RoomWorker) and ``app.state.bus``.
"""

from __future__ import annotations

import re
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from glosa.room import RoomWorker
from glosa.web.sse import sse_response

router = APIRouter()

# A language code: "es", "en", "pt-BR", "zh-Hant"...
_LANG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/api/rooms")
async def rooms(request: Request) -> list[dict]:
    return [w.view() | {"status": asdict(w.status())} for w in _workers(request)]


@router.get("/api/stream/{slug}/{lang}")
async def stream(slug: str, lang: str, request: Request):
    worker = next((w for w in _workers(request) if w.room.slug == slug), None)
    if worker is None or not _LANG.fullmatch(lang):
        raise HTTPException(status_code=404)
    last_event_id = _last_event_id(request)
    return sse_response(request.app.state.bus.subscribe(worker.room.id, lang, last_event_id))


def _workers(request: Request) -> list[RoomWorker]:
    return list(request.app.state.workers.values())


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None
