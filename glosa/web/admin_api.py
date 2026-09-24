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
    with a readable detail instead of a 500.

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
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from glosa.room import RoomWorker
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


@api_router.post("/rooms/{room_id}/start")
async def start_room(room_id: str, request: Request) -> dict:
    worker = _worker(request, room_id)
    try:
        await worker.start()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", "room": asdict(worker.status())}


@api_router.post("/rooms/{room_id}/stop")
async def stop_room(room_id: str, request: Request) -> dict:
    worker = _worker(request, room_id)
    await worker.stop()
    return {"status": "ok", "room": asdict(worker.status())}


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
