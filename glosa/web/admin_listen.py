"""Task 14b, Ruling 5 (the controller's binding call): "Escuchar el audio",
admin-only and only while a room plays a test file -- a logged-in admin can
listen to a room's file source (source_type "file", or "Probar con audio")
synchronized with its captions, to judge quality by ear and eye at once. The
audio runs 2-4 s ahead of the captions on screen, same as in the room
itself: real-time playback of the same file the pipeline reads, not a copy
kept in lockstep with the assembler.

  - ``GET /api/admin/listen/{room}``: ``{"available", "offset_s", "url"}``,
    entirely driven by ``RoomWorker.test_file()`` (glosa/room.py):
    ``available`` is false whenever the room isn't playing a test file at
    all (idle, or a live source -- url/youtube/emitter), *or* its MP3 isn't
    transcoded yet. The first poll that finds no cached MP3 starts one in
    the background (this module keeps at most one ffmpeg per cache key, so
    concurrent polls for the same file never race each other into starting
    a second).
  - ``GET /api/admin/listen/{room}/audio``: the cached MP3
    (``LISTEN_CACHE_DIR``), Range-enabled via Starlette's own
    ``FileResponse`` -- the same mechanism ``/static`` already relies on for
    206 responses, so this route doesn't hand-rewrite Range parsing.

Both routes carry only ``require_admin`` (no CSRF), like
glosa/web/admin_stream.py's ``stream_router``: an ``<audio>`` element's GET
can't send the ``X-Glosa-Admin`` header a state-changing ``/api/admin/*``
route requires, and neither route changes server state an attacker could
abuse via a forged link (a cache fill keyed off what's already playing, not
an admin action).

Caching: ``LISTEN_CACHE_DIR/<key>.mp3``, where ``key`` hashes the *file's
identity* (its resolved path, mtime and size) rather than its bytes --
cheap enough to redo on every poll (no re-reading a 50 MB upload just to
decide whether its MP3 already exists), while still reusing the cache
whenever the same clip plays again (the repo's samples, or the same
uploaded file replayed).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from glosa.room import RoomWorker
from glosa.web.auth import require_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

LISTEN_CACHE_DIR = Path("data") / "listen"
BITRATE = "64k"

# One in-flight transcode per cache key, shared by every concurrent poller
# for the same file. Module-level: one process, one ffmpeg per file at a
# time, whichever room asks.
_inflight: dict[str, asyncio.Task] = {}


@router.get("/listen/{room_id}")
async def listen_info(room_id: str, request: Request) -> dict:
    worker = _worker(request, room_id)
    info = worker.test_file()
    if info is None:
        return {"available": False, "offset_s": None, "url": None}
    path, offset_s = info
    cache = _cache_path(Path(path))
    if cache is None:
        return {"available": False, "offset_s": offset_s, "url": None}
    if not cache.is_file():
        _ensure_transcoding(Path(path), cache)
        return {"available": False, "offset_s": offset_s, "url": None}
    return {"available": True, "offset_s": offset_s, "url": f"/api/admin/listen/{room_id}/audio"}


@router.get("/listen/{room_id}/audio")
async def listen_audio(room_id: str, request: Request) -> FileResponse:
    worker = _worker(request, room_id)
    info = worker.test_file()
    if info is None:
        raise HTTPException(status_code=404, detail="not playing a test file")
    path, _ = info
    cache = _cache_path(Path(path))
    if cache is None or not cache.is_file():
        raise HTTPException(status_code=404, detail="the audio is not ready yet")
    return FileResponse(cache, media_type="audio/mpeg")


# ---- caching and the background transcode -------------------------------------


def _cache_path(source: Path) -> Path | None:
    """``LISTEN_CACHE_DIR/<key>.mp3`` for ``source``, or None if it no
    longer exists (the room moved on)."""
    try:
        stat = source.stat()
    except OSError:
        return None
    raw = f"{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return LISTEN_CACHE_DIR / f"{key}.mp3"


def _ensure_transcoding(source: Path, cache: Path) -> None:
    key = cache.stem
    existing = _inflight.get(key)
    if existing is not None and not existing.done():
        return  # already being made; this poll just waits for the next one
    LISTEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _inflight[key] = asyncio.create_task(_transcode(source, cache), name=f"listen-transcode-{key}")


async def _transcode(source: Path, cache: Path) -> None:
    """ffmpeg -> mono MP3 at BITRATE, written to a temp file and renamed
    into place atomically (same filesystem: no poller ever sees a partial
    MP3, even mid-encode)."""
    tmp = cache.with_suffix(".mp3.part")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-ac", "1", "-b:a", BITRATE, "-f", "mp3", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await proc.communicate()
        finally:
            # asyncio's subprocess transport doesn't close itself just
            # because the process exited and communicate() returned (a
            # known asyncio wart): left alone, it's only closed whenever
            # the GC happens to collect it, which can land arbitrarily
            # later -- observed here as spurious "subprocess is still
            # running" / "unclosed transport" ResourceWarnings blamed on
            # a *different*, unrelated test. Closing it as soon as we're
            # done frees the pipe/process resources right away instead of
            # waiting on GC, in production too, not just under tests.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        if proc.returncode != 0:
            log.error(
                "listen: ffmpeg failed for %s: %s", source,
                stderr.decode(errors="replace").strip() or f"exit {proc.returncode}",
            )
            tmp.unlink(missing_ok=True)
            return
        tmp.replace(cache)
    except OSError:
        log.exception("listen: could not transcode %s", source)
        tmp.unlink(missing_ok=True)


def _worker(request: Request, room_id: str) -> RoomWorker:
    worker = request.app.state.workers.get(room_id)
    if worker is None:
        raise HTTPException(status_code=404)
    return worker
