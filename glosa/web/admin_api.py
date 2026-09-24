"""Admin: password login and room start/stop.

  - ``GET /admin``: the room console (the login form if there is no valid
    session cookie; otherwise the room list with Start/Stop).
  - ``GET /admin/login``: the login form on its own (redirects to
    ``/admin`` if already authenticated).
  - ``POST /admin/login``: checks ``password`` against
    ``Settings.admin_password`` (constant time) and sets the signed session
    cookie (``glosa.web.auth``), or raises 401 for a wrong password.
  - ``POST /api/admin/rooms/{room_id}/start`` / ``.../stop``: call through
    to that room's ``RoomWorker``. Both require the session cookie
    (``glosa.web.auth.require_admin``).

``create_app()`` (glosa/web/app.py) provides ``app.state.workers`` (room id
-> RoomWorker, keyed the same way as ``app.state.settings.rooms``) and
``app.state.settings``.

The room list's visual state reuses the four-state vocabulary
(live/degraded/down/idle) already defined in glosa.css for the "Sala de
control" console (docs/design/admin.html), mapped from RoomStatus's
green/yellow/red/idle. Task 12 replaces this page with that full console;
this one is the MVP's bare minimum: name, state, current talk, Start/Stop.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from glosa.room import RoomWorker
from glosa.web.auth import (
    COOKIE_MAX_AGE_S,
    COOKIE_NAME,
    is_authenticated,
    require_admin,
    sign_session,
    verify_password,
)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# RoomStatus.state (green/yellow/red/idle) -> the live/degraded/down/idle
# vocabulary glosa.css's .strip--*/.status--* classes use.
_STATE_CSS = {"green": "live", "yellow": "degraded", "red": "down", "idle": "idle"}


# ---- pages ------------------------------------------------------------------


@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
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
def login(request: Request, password: str = Form(...)):
    settings = request.app.state.settings
    if not verify_password(password, settings.admin_password):
        raise HTTPException(status_code=401, detail="wrong password")
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(settings.admin_password),
        max_age=COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


# ---- room control -------------------------------------------------------------


@router.post("/api/admin/rooms/{room_id}/start")
async def start_room(room_id: str, request: Request, _: None = Depends(require_admin)) -> dict:
    worker = _worker(request, room_id)
    await worker.start()
    return {"status": "ok", "room": asdict(worker.status())}


@router.post("/api/admin/rooms/{room_id}/stop")
async def stop_room(room_id: str, request: Request, _: None = Depends(require_admin)) -> dict:
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
    return worker.view() | {"status": status, "css_state": _STATE_CSS.get(status["state"], "idle")}
