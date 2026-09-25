"""Tests for glosa.web.admin_listen (Task 14b, Ruling 5): the admin-only
"Escuchar el audio" backend.

  - GET /api/admin/listen/{room}: {"available", "offset_s", "url"}, driven
    entirely by RoomWorker.test_file() (glosa/room.py; tests/test_room.py
    covers that method itself -- here it's mocked).
  - GET /api/admin/listen/{room}/audio: the cached MP3, Range-enabled
    (Starlette's FileResponse).

require_admin only (no CSRF, like admin_stream.py's stream_router): a plain
<audio> GET can't send the X-Glosa-Admin header.

A minimal FastAPI app around glosa.web.admin_listen's own router.
``httpx.AsyncClient`` + ``ASGITransport`` (tests/web/test_admin_controls.py's
own pattern), not the synchronous ``TestClient``: the background transcode
this router starts with ``asyncio.create_task()`` must run on the *same*
event loop the test awaits it from, and a synchronous ``TestClient`` not
used as a context manager opens a fresh portal thread (and loop) per call,
which strands that task on a loop of its own -- observed as the
transcode never finishing (and, worse, dangling-subprocess warnings from
a loop closed out from under it). The transcode itself runs real ffmpeg
against tests/fixtures/short_clip.wav (2.5 s) -- skipped if ffmpeg isn't on
PATH -- plus one mocked-ffmpeg test for the "kicks off a background
transcode" shape without depending on real encoding time.
"""

from __future__ import annotations

import asyncio
import gc
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from glosa.web import admin_listen
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

ADMIN_PASSWORD = "s3cr3t-pw"
FIXTURE_CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "short_clip.wav"


def _worker(test_file=None) -> MagicMock:
    worker = MagicMock()
    worker.test_file.return_value = test_file
    return worker


def _make_app(workers: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = MagicMock(admin_password=ADMIN_PASSWORD)
    app.state.workers = workers if workers is not None else {"r1": _worker()}
    app.state.admin_secret = new_admin_secret()
    app.state.session_epoch = 0
    app.include_router(admin_listen.router)
    return app


@asynccontextmanager
async def _client(app: FastAPI, *, authenticated: bool = True) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if authenticated:
            client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
        yield client


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "listen-cache"
    monkeypatch.setattr(admin_listen, "LISTEN_CACHE_DIR", directory)
    admin_listen._inflight.clear()
    yield directory
    admin_listen._inflight.clear()


async def _drain_inflight() -> None:
    """Wait for every transcode this test kicked off, deterministically
    (no sleep-based polling): the router only ever creates tasks into
    ``_inflight``, on this same event loop."""
    while admin_listen._inflight:
        tasks = list(admin_listen._inflight.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        for key, task in list(admin_listen._inflight.items()):
            if task.done():
                admin_listen._inflight.pop(key, None)
    # A finished task can still be the last reference to the ffmpeg
    # subprocess it awaited; collecting it here, in the same test and while
    # its event loop is still open, means any cleanup warning is raised (and
    # attributed) right here -- not blamed on some unrelated, later test
    # whenever the GC next happens to run.
    gc.collect()


# ---- auth --------------------------------------------------------------------


async def test_without_a_session_is_401_on_both_routes() -> None:
    app = _make_app()
    async with _client(app, authenticated=False) as client:
        assert (await client.get("/api/admin/listen/r1")).status_code == 401
        assert (await client.get("/api/admin/listen/r1/audio")).status_code == 401


async def test_unknown_room_is_404() -> None:
    app = _make_app({})
    async with _client(app) as client:
        assert (await client.get("/api/admin/listen/r1")).status_code == 404


# ---- availability --------------------------------------------------------------


async def test_not_playing_a_file_is_unavailable() -> None:
    app = _make_app({"r1": _worker(test_file=None)})
    async with _client(app) as client:
        response = await client.get("/api/admin/listen/r1")

    assert response.status_code == 200
    assert response.json() == {"available": False, "offset_s": None, "url": None}


async def test_offset_reflects_whatever_the_worker_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live source (url/youtube/emitter) never reaches here as anything
    but None -- RoomWorker.test_file() (glosa/room.py) already encodes that
    rule; the router just relays its offset.

    This is the only assertion here, so the cache miss the router sees for
    ``clip`` (nothing has transcoded it) must not be left to kick off a real
    background ffmpeg this test never awaits or drains: the "real ffmpeg"
    tests below own that behaviour."""
    monkeypatch.setattr(admin_listen, "_ensure_transcoding", lambda *a, **kw: None)
    clip = tmp_path / "clip.wav"
    clip.write_bytes(FIXTURE_CLIP.read_bytes())
    worker = _worker(test_file=(str(clip), 1.5))
    app = _make_app({"r1": worker})
    async with _client(app) as client:
        first = (await client.get("/api/admin/listen/r1")).json()
        worker.test_file.return_value = (str(clip), 4.25)
        second = (await client.get("/api/admin/listen/r1")).json()

    assert first["offset_s"] == 1.5
    assert second["offset_s"] == 4.25
    assert second["offset_s"] > first["offset_s"]


# ---- the audio endpoint: 404 before the mp3 is ready --------------------------


async def test_audio_is_404_before_the_mp3_is_ready(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    clip.write_bytes(FIXTURE_CLIP.read_bytes())
    app = _make_app({"r1": _worker(test_file=(str(clip), 0.0))})
    async with _client(app) as client:
        assert (await client.get("/api/admin/listen/r1/audio")).status_code == 404


# ---- background transcode (mocked ffmpeg: shape only) -------------------------


async def test_a_first_poll_kicks_off_exactly_one_transcode_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.wav"
    clip.write_bytes(FIXTURE_CLIP.read_bytes())
    # The fake process blocks in communicate() until the test releases it, so
    # the first transcode is deterministically still in flight (not merely
    # "probably still running") when the second poll arrives: with an
    # instantly-returning fake, both polls racing to finish before the other
    # even starts is exactly the kind of scheduling accident this test must
    # not depend on.
    release = asyncio.Event()

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            await release.wait()
            return b"", b""

    calls = []

    async def fake_exec(*cmd, **kw):
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    app = _make_app({"r1": _worker(test_file=(str(clip), 0.0))})
    async with _client(app) as client:
        r1 = await client.get("/api/admin/listen/r1")
        r2 = await client.get("/api/admin/listen/r1")  # a second poll before it's ready

        assert r1.json()["available"] is False
        assert r2.json()["available"] is False
        assert len(calls) == 1  # the second poll reused the in-flight task, no second ffmpeg
        release.set()
        await _drain_inflight()


# ---- real ffmpeg end to end -----------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
async def test_real_transcode_then_range_request(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    clip.write_bytes(FIXTURE_CLIP.read_bytes())
    app = _make_app({"r1": _worker(test_file=(str(clip), 2.0))})
    async with _client(app) as client:
        first = (await client.get("/api/admin/listen/r1")).json()
        assert first == {"available": False, "offset_s": 2.0, "url": None}
        await _drain_inflight()

        ready = (await client.get("/api/admin/listen/r1")).json()
        assert ready["available"] is True
        assert ready["url"] == "/api/admin/listen/r1/audio"
        assert ready["offset_s"] == 2.0

        whole = await client.get(ready["url"])
        assert whole.status_code == 200
        assert whole.headers["content-type"] == "audio/mpeg"
        assert whole.headers.get("accept-ranges") == "bytes"
        body = whole.content
        assert len(body) > 100  # a real, non-trivial mp3

        partial = await client.get(ready["url"], headers={"Range": "bytes=0-9"})
        assert partial.status_code == 206
        assert len(partial.content) == 10
        assert partial.headers["content-range"] == f"bytes 0-9/{len(body)}"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
async def test_a_repeated_test_audio_of_the_same_clip_reuses_the_cached_mp3(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    clip.write_bytes(FIXTURE_CLIP.read_bytes())
    app = _make_app({"r1": _worker(test_file=(str(clip), 0.0))})
    async with _client(app) as client:
        await client.get("/api/admin/listen/r1")
        await _drain_inflight()
        files_after_first = list(admin_listen.LISTEN_CACHE_DIR.glob("*.mp3"))
        assert len(files_after_first) == 1

        second = (await client.get("/api/admin/listen/r1")).json()
        assert second["available"] is True  # no new transcode needed
        files_after_second = list(admin_listen.LISTEN_CACHE_DIR.glob("*.mp3"))
        assert files_after_second == files_after_first
