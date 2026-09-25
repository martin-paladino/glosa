"""Task 14b, plan case 14.3: "Probar con audio" -- one admin action that
plays a clip through a room's pipeline exactly as if it were the source,
so the operator can judge caption/translation quality without a live talk.

``POST /api/admin/rooms/{room_id}/test-audio``: multipart form, either
``sample`` (``en`` or ``es`` -- the repo's own ``samples/{lang}_clip.opus``)
or ``file`` (an upload, saved under ``UPLOADS_DIR``, 50 MB max -> 413).
Either way this ends in ``RoomWorker.play_file(path)`` (glosa/room.py),
which plays the file at real-time speed as the room's audio -- a running
talk keeps its engine session, an idle room starts its free session.

A router of its own (not glosa/web/admin_api.py, Task 12's file) with the
same dependencies as its ``api_router`` (``require_admin`` then
``require_csrf_header`` -- glosa/web/auth.py), mounted alongside it by
create_app() (glosa/web/app.py), so this task's own file doesn't collide
with Task 12's. admin.html already carries the button this fills in (a
hidden ``data-slot="test-audio"``, Task 12); admin.js POSTs here.

The uploaded/resolved path becomes admin-only, admin-only-time data (Ruling
5's "Escuchar el audio" listens to whatever RoomWorker.play_file() is
playing, via ``RoomWorker.test_file()`` -- glosa/web/admin_listen.py, not
this module): nothing here needs to know about that.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from glosa.room import RoomWorker
from glosa.web.auth import require_admin, require_csrf_header

# Same dependency order as glosa/web/admin_api.py's api_router (require_admin,
# then require_csrf_header): a separate APIRouter instance so this task's own
# file stays out of Task 12's.
api_router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin), Depends(require_csrf_header)])

# Task-14 spec: "un archivo subido (máx. 50 MB → 413)".
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

UPLOADS_DIR = Path("data") / "uploads"
SAMPLES_DIR = Path("samples")
# Packaged installs (no samples/ next to the working directory) fall back to
# the checkout's own copy -- same trick as glosa/web/app.py's fixture lookup.
CHECKOUT_SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

Sample = Literal["en", "es"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sample_path(sample: str) -> Path:
    name = f"{sample}_clip.opus"
    for directory in (SAMPLES_DIR, CHECKOUT_SAMPLES_DIR):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=500, detail=f"sample clip {name!r} is not installed")


def _safe_name(filename: str) -> str:
    """The upload's own file name, sanitized: no directory components, no
    characters outside a small safe set (an upload's name is untrusted)."""
    name = Path(filename).name or "upload"
    name = _UNSAFE.sub("_", name)
    return name[-100:] or "upload"


@api_router.post("/rooms/{room_id}/test-audio")
async def play_test_audio(
    room_id: str,
    request: Request,
    sample: Sample | None = Form(None),
    file: UploadFile | None = File(None),
) -> dict:
    worker = _worker(request, room_id)
    has_file = file is not None and bool(file.filename)
    if has_file == bool(sample):
        raise HTTPException(status_code=422, detail="send either a sample (en|es) or a file, not both")
    if has_file:
        assert file is not None
        path = await _save_upload(room_id, file)
    else:
        assert sample is not None
        path = _sample_path(sample)
    try:
        await worker.play_file(str(path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"clip not found: {path}") from exc
    return {"ok": True}


async def _save_upload(room_id: str, file: UploadFile) -> Path:
    """Read at most MAX_UPLOAD_BYTES + 1 (never trusting Content-Length
    alone) and save under UPLOADS_DIR; 413 for anything larger, with no
    partial file left behind."""
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"the file is larger than {MAX_UPLOAD_BYTES} bytes")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{room_id}-{secrets.token_hex(6)}-{_safe_name(file.filename or 'upload')}"
    path = UPLOADS_DIR / name
    path.write_bytes(raw)
    return path


def _worker(request: Request, room_id: str) -> RoomWorker:
    worker = request.app.state.workers.get(room_id)
    if worker is None:
        raise HTTPException(status_code=404)
    return worker
