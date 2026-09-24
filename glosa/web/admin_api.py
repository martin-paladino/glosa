"""Admin: password login and room start/stop.

  - ``GET /admin``: the room console (the login form if there is no valid
    session cookie; otherwise the room list with Start/Stop).
  - ``GET /admin/login``: the login form on its own (redirects to
    ``/admin`` if already authenticated).
  - ``POST /admin/login``: checks ``password`` against
    ``Settings.admin_password`` (constant time) and sets the signed session
    cookie (``glosa.web.auth``), or raises 401 for a wrong password (after
    a 1s sleep -- a cheap brake on brute-forcing it).
  - ``POST /admin/logout``: clears the session cookie and, since one
    ``ADMIN_PASSWORD`` is shared by everyone with it, invalidates every
    other outstanding session too (``app.state.session_epoch``, Ruling 43).
  - ``POST /api/admin/rooms/{room_id}/start`` / ``.../stop``: call through
    to that room's ``RoomWorker``, keyed by its config ``id`` (not its
    ``slug`` -- they're equal today, but that's an implementation detail of
    ``create_app``, not a contract). ``start`` turns a room with no
    configured source (``RoomWorker.start()``'s ``ValueError``) into a 409
    with a readable detail instead of a 500. Both switch the room to
    ``manual`` first (Ruling 34), or the autopilot's next tick would undo
    them.

The autopilot's controls (Task 9, glosa/scheduler.py on
``app.state.autopilot``); each replies ``{"status": "ok", "mode", "room":
RoomStatus}``:

  - ``POST .../rooms/{id}/mode`` ``{"mode": "auto"|"manual"}``: persisted;
    back to ``auto`` ticks the room at once;
  - ``POST .../rooms/{id}/start-talk`` ``{"talk_id"}``: end the current talk
    and open that one (manual); 404 no such talk, 409 another room's talk,
    a free session or no source;
  - ``POST .../rooms/{id}/end-talk``: the room goes idle (manual);
  - ``POST .../rooms/{id}/reconnect``: a new engine session, mode unchanged;
  - ``GET /api/admin/rooms``: every room's id, name, mode, status, current
    talk (``now``) and next agenda talk (``next``, free sessions aside).

The agenda (Task 8), JSON only (the panel is Task 12's):

  - ``POST /api/admin/agenda/import``: multipart form with ``file`` (an
    upload) or ``url`` (fetched by the server with the stdlib, http(s)
    only, 15 s timeout), optional ``format`` (``csv`` | ``nerdearla``,
    guessed from the name or the content) and ``room_map`` (JSON: agenda
    room name -> room id; replaces the default map of each room's id, name
    and ``agenda_names``). Returns ``{"imported": N, "skipped": [{"source_id",
    "title", "reason"}...]}`` (Ruling 17): rows of unmapped rooms, talks
    that end before they start, and talks already ``live``/``done``, which a
    re-import never touches (``Database.upsert_agenda``). A malformed row is
    a 422 ``{"row", "reason"}`` and nothing is imported. Start/end are
    stored in the event's timezone, so ``day`` queries match the event's
    calendar.
  - ``GET /api/admin/talks?room=&day=``: a room's (or every room's) talks
    of ``day`` (default today, event timezone), free sessions left out.
  - ``GET /api/admin/talks/{id}``, ``PUT /api/admin/talks/{id}``: one talk;
    the PUT takes any of ``title, speakers, language, targets, engine,
    start, end, abstract, tags, glossary`` (languages es/en as in the
    parsers, engine fast/glossary, start before end; else 422), 404 for no
    such talk, 409 for a free session or, on a live talk, for anything but
    title/targets/glossary. It updates the DB, logs a ``talk_updated``
    event and publishes one on ``app.state.admin_events``.

Two routers, on purpose (fix round 1 wired ``require_admin``/
``require_csrf_header`` per route; fix round 2 moved them here): ``router``
carries the page/login/logout routes, unprotected by either (login can't
require a session it's there to create; logout only clears one); ``api_router``
is ``APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin),
Depends(require_csrf_header)])``, so *every* route added to it -- today's
start/stop, and whatever ``/api/admin/*`` route comes later -- gets both
checks (401 before 403, same order as before) without having to remember
to add them by hand.

``create_app()`` (glosa/web/app.py) provides ``app.state.workers`` (room id
-> RoomWorker, keyed the same way as ``app.state.settings.rooms``),
``app.state.settings``, ``app.state.admin_secret`` (a fresh per-process
key, ``glosa.web.auth.new_admin_secret()``) and ``app.state.session_epoch``
(an int, 0 until the first logout increments it), and mounts both
``router`` and ``api_router``.

The room list's visual state reuses the four-state vocabulary
(live/degraded/down/idle) already defined in glosa.css for the "Sala de
control" console (docs/design/admin.html), mapped from RoomStatus's
green/yellow/red/idle. It also shows RoomStatus.detail, the raw
possibly-technical status text (ffmpeg/API errors): fine behind admin auth
(Ruling 29), even though the audience-facing bus only ever gets the state
name. Task 12 replaces this page with that full console; this one is the
MVP's bare minimum: name, state, detail, current talk, Start/Stop.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from glosa.agenda import AgendaError
from glosa.agenda.csv_import import VALID_ENGINES, VALID_LANGUAGES, parse_csv
from glosa.agenda.nerdearla_import import SkippedSession, parse_nerdearla_report
from glosa.models import GlossaryTerm, Talk
from glosa.room import RoomWorker, is_free_talk
from glosa.web.auth import (
    COOKIE_MAX_AGE_S,
    COOKIE_NAME,
    cookie_is_secure,
    is_authenticated,
    require_admin,
    require_csrf_header,
    sign_session,
    verify_password,
)

# Pages, login and logout: no auth dependency (login can't require the
# session it's about to create; logout only ever needs to clear one).
router = APIRouter()

# Every state-changing admin API route: both checks apply automatically to
# anything mounted here, present or future (fix round 2, #3). 401 (require_admin)
# runs before 403 (require_csrf_header), same order fix round 1 used per route.
api_router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin), Depends(require_csrf_header)])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# RoomStatus.state (green/yellow/red/idle) -> the live/degraded/down/idle
# vocabulary glosa.css's .strip--*/.status--* classes use.
_STATE_CSS = {"green": "live", "yellow": "degraded", "red": "down", "idle": "idle"}

# A cheap brute-force brake: a wrong password always takes at least this long.
_FAILED_LOGIN_DELAY_S = 1.0


# ---- pages ------------------------------------------------------------------


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    rooms = [_room_summary(w) for w in _workers(request)]
    return templates.TemplateResponse(request, "admin.html", {"authenticated": True, "rooms": rooms})


@router.get("/admin/login", include_in_schema=False)
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {"authenticated": False, "rooms": []})


@router.post("/admin/login", include_in_schema=False)
async def login(request: Request, password: str = Form(...)):
    settings = request.app.state.settings
    if not verify_password(password, settings.admin_password):
        await asyncio.sleep(_FAILED_LOGIN_DELAY_S)
        raise HTTPException(status_code=401, detail="wrong password")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(
            request.app.state.admin_secret, settings.admin_password, epoch=request.app.state.session_epoch
        ),
        max_age=COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
def logout(request: Request):
    # Ruling 43: increment the session epoch -- mixed into every token's
    # HMAC, not compared as a timestamp -- so every session signed under
    # the old epoch fails outright, not just the cookie this browser is
    # deleting. One shared ADMIN_PASSWORD means "log out" should mean
    # "every session", not "this browser". (A prior version compared a
    # wall-clock float against issued_at instead; that let a login landing
    # in the very same second as a logout get wrongly rejected, because
    # sign_session floors issued_at to whole seconds. An epoch has no such
    # boundary to get wrong.)
    request.app.state.session_epoch += 1
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ---- room control -------------------------------------------------------------


class ModeIn(BaseModel):
    mode: Literal["auto", "manual"]


class StartTalkIn(BaseModel):
    talk_id: str


@api_router.get("/rooms")
async def list_rooms(request: Request) -> list[dict]:
    """Every room with its mode, status, current talk and next agenda talk."""
    autopilot = request.app.state.autopilot
    rooms = []
    for worker in _workers(request):
        room_id = worker.room.id
        nxt = await autopilot.next_talk(room_id)
        rooms.append(
            {
                "id": room_id,
                "name": worker.room.name,
                "mode": autopilot.mode(room_id),
                "status": asdict(worker.status()),
                "now": worker.view()["now"],
                "next": talk_json(nxt) if nxt is not None else None,
            }
        )
    return rooms


@api_router.post("/rooms/{room_id}/start")
async def start_room(room_id: str, request: Request) -> dict:
    """Start the room (its current talk again, or the free session). Takes
    the room off the autopilot (Ruling 34)."""
    worker = _worker(request, room_id)
    await request.app.state.autopilot.set_mode(room_id, "manual")
    try:
        await worker.start()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _control_reply(request, worker)


@api_router.post("/rooms/{room_id}/stop")
async def stop_room(room_id: str, request: Request) -> dict:
    """Stop the room. Takes it off the autopilot (Ruling 34)."""
    worker = _worker(request, room_id)
    await request.app.state.autopilot.set_mode(room_id, "manual")
    await worker.stop()
    return _control_reply(request, worker)


@api_router.post("/rooms/{room_id}/mode")
async def set_room_mode(room_id: str, body: ModeIn, request: Request) -> dict:
    """``auto`` or ``manual``, persisted. Back to ``auto``, the room is
    ticked at once: the agenda rules again now, not up to 5 s later."""
    worker = _worker(request, room_id)
    autopilot = request.app.state.autopilot
    await autopilot.set_mode(room_id, body.mode)
    if body.mode == "auto":
        await autopilot.tick(room_id)
    return _control_reply(request, worker)


@api_router.post("/rooms/{room_id}/start-talk")
async def start_talk(room_id: str, body: StartTalkIn, request: Request) -> dict:
    """Open an agenda talk now, ending the current one; the room goes manual.
    404: no such talk; 409: another room's talk, a free session, or no
    audio source."""
    worker = _worker(request, room_id)
    try:
        await request.app.state.autopilot.start_talk(room_id, body.talk_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"no talk {body.talk_id!r}") from exc
    return _control_reply(request, worker)


@api_router.post("/rooms/{room_id}/end-talk")
async def end_talk(room_id: str, request: Request) -> dict:
    """End the room's talk now (the room goes idle and manual)."""
    worker = _worker(request, room_id)
    await request.app.state.autopilot.end_talk(room_id)
    return _control_reply(request, worker)


@api_router.post("/rooms/{room_id}/reconnect")
async def reconnect_room(room_id: str, request: Request) -> dict:
    """A new engine session for the running talk; the mode stays."""
    worker = _worker(request, room_id)
    await request.app.state.autopilot.reconnect(room_id)
    return _control_reply(request, worker)


def _control_reply(request: Request, worker: RoomWorker) -> dict:
    return {
        "status": "ok",
        "mode": request.app.state.autopilot.mode(worker.room.id),
        "room": asdict(worker.status()),
    }


# ---- agenda -------------------------------------------------------------------

# The agenda fields a live talk still takes (TalkEdit lists every editable one).
LIVE_EDITABLE = frozenset({"title", "targets", "glossary"})
MAX_AGENDA_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_S = 15
AgendaFormat = Literal["csv", "nerdearla"]


class GlossaryTermIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    keep_in_english: bool = True
    translation: str | None = None

    @field_validator("term")
    @classmethod
    def _term(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a glossary term cannot be blank")
        return value

    @field_validator("translation")
    @classmethod
    def _translation(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class TalkEdit(BaseModel):
    """PUT /api/admin/talks/{id}: any subset of the editable fields. Anything
    else (status, room_id, actual_*...) is a 422, and so is a null."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    speakers: list[str] | None = None
    language: str | None = None
    targets: list[str] | None = None
    engine: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    abstract: str | None = None
    tags: list[str] | None = None
    glossary: list[GlossaryTermIn] | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("the title cannot be blank")
        return value.strip() if value is not None else None

    @field_validator("speakers", "tags")
    @classmethod
    def _names(cls, value: list[str] | None) -> list[str] | None:
        return [v.strip() for v in value if v.strip()] if value is not None else None

    @field_validator("abstract")
    @classmethod
    def _abstract(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        return _language(value) if value is not None else None

    @field_validator("targets")
    @classmethod
    def _targets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(_language(v) for v in value))

    @field_validator("engine")
    @classmethod
    def _engine(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_ENGINES:
            raise ValueError(f"unknown engine {value!r} (expected fast or glossary)")
        return value

    @model_validator(mode="after")
    def _no_nulls(self) -> TalkEdit:
        nulls = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if nulls:
            raise ValueError(f"these fields cannot be null: {', '.join(nulls)}")
        return self


def _language(code: str) -> str:
    """The parsers' language rule (glosa/agenda/csv_import.py): es or en."""
    code = code.strip().lower()
    if code not in VALID_LANGUAGES:
        raise ValueError(f"unsupported language {code!r} (expected es or en)")
    return code


def talk_json(talk: Talk) -> dict[str, Any]:
    """A Talk as the admin API returns it: datetimes as ISO 8601 with offset."""
    data = asdict(talk)
    for key in ("start", "end", "actual_start", "actual_end"):
        value = data[key]
        data[key] = value.isoformat() if value is not None else None
    return data


@api_router.post("/agenda/import")
async def import_agenda(
    request: Request,
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    source_format: AgendaFormat | None = Form(None, alias="format"),
    room_map: str | None = Form(None),
) -> dict:
    """Import an agenda: a CSV (agenda.example.csv) or Nerdearla's sessions
    JSON, uploaded as ``file`` or fetched by the server from ``url``.
    ``format`` is guessed from the file name or the content when omitted.
    ``room_map`` (a JSON object, agenda room name -> room id) replaces the
    one built from config.yaml (each room's id, name and agenda_names)."""
    settings = request.app.state.settings
    has_file = file is not None and bool(file.filename)
    if has_file == bool(url):
        raise HTTPException(status_code=422, detail="send either a file or a url, not both")
    names = _room_names(request, room_map)
    if has_file:
        assert file is not None
        raw = await file.read(MAX_AGENDA_BYTES + 1)
        source_name = file.filename or ""
    else:
        assert url is not None
        raw = await _fetch(url)
        source_name = urllib.parse.urlparse(url).path
    if len(raw) > MAX_AGENDA_BYTES:
        raise HTTPException(status_code=413, detail=f"the agenda is larger than {MAX_AGENDA_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")  # a spreadsheet's CSV export often starts with a BOM
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="the agenda is not UTF-8 text") from exc
    kind: AgendaFormat = source_format or _sniff(source_name, text)
    try:
        if kind == "csv":
            talks, skipped = _from_csv(text, settings.timezone, names, settings.default_engine_en)
        else:
            talks, skipped = _from_nerdearla(text, settings.timezone, names, settings.default_engine_en)
    except AgendaError as exc:
        raise HTTPException(status_code=422, detail={"row": exc.row, "reason": exc.reason}) from exc

    zone = _event_tz(request)
    unique: dict[str, Talk] = {}
    for talk in talks:
        talk.start, talk.end = talk.start.astimezone(zone), talk.end.astimezone(zone)  # the event's day
        if talk.end <= talk.start:
            skipped.append(SkippedSession(talk.id, talk.title, "end is not after start"))
        else:
            unique[talk.id] = talk  # the same id twice: the last one wins
    held = await request.app.state.db.upsert_agenda(list(unique.values()))
    skipped += [
        SkippedSession(talk_id, unique[talk_id].title, f"talk is {status}: left unchanged")
        for talk_id, status in held.items()
    ]
    imported = len(unique) - len(held)
    await request.app.state.db.log_event(
        None, "info", "agenda_import", f"{kind}: {imported} imported, {len(skipped)} skipped"
    )
    request.app.state.admin_events.publish(
        "agenda_imported", {"format": kind, "imported": imported, "skipped": len(skipped)}
    )
    return {"imported": imported, "skipped": [asdict(s) for s in skipped]}


@api_router.get("/talks")
async def list_talks(request: Request, room: str | None = None, day: date | None = None) -> list[dict]:
    """The agenda of one room (or every room, in config.yaml order) for
    ``day`` (default: today in the event's timezone). Free sessions are not
    part of the agenda."""
    workers = request.app.state.workers
    if room is not None and room not in workers:
        raise HTTPException(status_code=404, detail=f"unknown room {room!r}")
    if day is None:
        day = request.app.state.clock.wall().astimezone(_event_tz(request)).date()
    talks: list[Talk] = []
    for room_id in [room] if room is not None else list(workers):
        talks += await request.app.state.db.get_talks(room_id, day)
    return [talk_json(t) for t in talks if not is_free_talk(t.id)]


@api_router.get("/talks/{talk_id}")
async def get_talk(talk_id: str, request: Request) -> dict:
    talk = await request.app.state.db.get_talk(talk_id)
    if talk is None:
        raise HTTPException(status_code=404, detail=f"no talk {talk_id!r}")
    return talk_json(talk)


@api_router.put("/talks/{talk_id}")
async def update_talk(talk_id: str, edit: TalkEdit, request: Request) -> dict:
    """Edit a talk's agenda fields. 404 if there is no such talk, 409 for a
    free session or for a live talk's fields other than title, targets and
    glossary, 422 for an invalid value or a start that is not before the end.
    A naive start/end is in the event's timezone."""
    db = request.app.state.db
    talk = await db.get_talk(talk_id)
    if talk is None:
        raise HTTPException(status_code=404, detail=f"no talk {talk_id!r}")
    if is_free_talk(talk_id):
        raise HTTPException(status_code=409, detail="a free session is not part of the agenda")
    zone = _event_tz(request)
    fields: dict[str, Any] = {}
    for name in edit.model_fields_set:
        value = getattr(edit, name)
        if name in ("start", "end"):
            value = (value if value.tzinfo is not None else value.replace(tzinfo=zone)).astimezone(zone)
        elif name == "glossary":
            value = [GlossaryTerm(**term.model_dump()) for term in value]
        fields[name] = value
    changes = {name: value for name, value in fields.items() if value != getattr(talk, name)}
    if talk.status == "live":
        blocked = sorted(set(changes) - LIVE_EDITABLE)
        if blocked:
            raise HTTPException(
                status_code=409,
                detail=f"the talk is live: only title, targets and glossary can change, not {', '.join(blocked)}",
            )
    if not changes.get("start", talk.start) < changes.get("end", talk.end):
        raise HTTPException(status_code=422, detail="start must be before end")
    if not changes:
        return talk_json(talk)
    await db.update_talk(talk_id, **changes)
    updated = await db.get_talk(talk_id)
    assert updated is not None
    changed = sorted(changes)
    await db.log_event(talk.room_id, "info", "talk_updated", f"{talk_id}: {', '.join(changed)}")
    request.app.state.admin_events.publish("talk_updated", {"talk": talk_json(updated), "fields": changed})
    _refresh_running_talk(request, updated, changes)
    return talk_json(updated)


def _refresh_running_talk(request: Request, updated: Talk, changes: dict[str, Any]) -> None:
    """A live talk's new title and glossary reach its running worker at once
    (the room list shows the title). New targets apply from its next start:
    the running pipeline keeps the engine session it opened."""
    worker = request.app.state.workers.get(updated.room_id)
    current = worker.talk if worker is not None else None
    if current is None or current.id != updated.id:
        return
    for name in ("title", "glossary"):
        if name in changes:
            setattr(current, name, getattr(updated, name))


def _event_tz(request: Request) -> tzinfo:
    """The event's timezone (Settings.timezone); UTC if it is not a known
    zone, as RoomWorker does."""
    try:
        return ZoneInfo(request.app.state.settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _room_names(request: Request, room_map: str | None) -> dict[str, str]:
    """Agenda room name -> room id: each configured room's id, name and
    agenda_names, or the explicit ``room_map`` JSON, which replaces them."""
    rooms = request.app.state.settings.rooms
    if room_map is None or not room_map.strip():
        names: dict[str, str] = {}
        for cfg in rooms:
            names |= {cfg.id: cfg.id, cfg.name: cfg.id} | {name: cfg.id for name in cfg.agenda_names}
        return names
    try:
        explicit = json.loads(room_map)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="room_map must be a JSON object") from exc
    if not isinstance(explicit, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in explicit.items()
    ):
        raise HTTPException(status_code=422, detail="room_map must map agenda room names to room ids")
    known = {cfg.id for cfg in rooms}
    unknown = sorted(set(explicit.values()) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"room_map names unknown rooms: {', '.join(unknown)}")
    return explicit


def _sniff(name: str, text: str) -> AgendaFormat:
    lowered = name.lower()
    if lowered.endswith(".csv"):
        return "csv"
    if lowered.endswith(".json"):
        return "nerdearla"
    return "nerdearla" if text.lstrip()[:1] in ("{", "[") else "csv"


def _from_csv(
    text: str, tz: str, names: dict[str, str], default_engine_en: Literal["fast", "glossary"]
) -> tuple[list[Talk], list[SkippedSession]]:
    talks: list[Talk] = []
    skipped: list[SkippedSession] = []
    for row, talk in enumerate(parse_csv(text, tz, default_engine_en), start=2):  # parse_csv's row numbers
        room_id = names.get(talk.room_id)
        if room_id is None:
            skipped.append(SkippedSession(None, talk.title, f"row {row}: unmapped room: {talk.room_id!r}"))
            continue
        talk.room_id = room_id
        talks.append(talk)
    return talks, skipped


def _from_nerdearla(
    text: str, tz: str, names: dict[str, str], default_engine_en: Literal["fast", "glossary"]
) -> tuple[list[Talk], list[SkippedSession]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc.msg} (line {exc.lineno})") from exc
    sessions = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
        raise HTTPException(status_code=422, detail="expected Nerdearla's sessions JSON: {\"sessions\": [...]}")
    return parse_nerdearla_report(sessions, names, tz, default_engine_en)


async def _fetch(url: str) -> bytes:
    """GET ``url`` from the server (stdlib only; http/https only)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must be an http(s) URL")

    def get() -> bytes:
        headers = {"User-Agent": "Glosa", "Accept": "application/json, text/csv, */*"}
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            return response.read(MAX_AGENDA_BYTES + 1)

    try:
        return await asyncio.to_thread(get)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"could not fetch the agenda: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise HTTPException(status_code=502, detail=f"could not fetch the agenda: {reason}") from exc


# ---- helpers ------------------------------------------------------------------


def _workers(request: Request) -> list[RoomWorker]:
    return list(request.app.state.workers.values())


def _worker(request: Request, room_id: str) -> RoomWorker:
    worker = request.app.state.workers.get(room_id)
    if worker is None:
        raise HTTPException(status_code=404)
    return worker


def _room_summary(worker: RoomWorker) -> dict:
    status = asdict(worker.status())
    return worker.view() | {
        "id": worker.room.id,
        "status": status,
        "css_state": _STATE_CSS.get(status["state"], "idle"),
    }
