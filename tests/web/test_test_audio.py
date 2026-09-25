"""Tests for glosa.web.test_audio (Task 14b, plan case 14.3): "Probar con
audio" -- POST /api/admin/rooms/{room}/test-audio, ``sample=en|es`` (the
repo's own clips) or an uploaded file, calling RoomWorker.play_file().

A minimal FastAPI app around glosa.web.test_audio's own api_router, mirroring
tests/web/test_station.py's pattern: mocked RoomWorkers (play_file is an
AsyncMock; only the attributes the router reads), a real admin cookie.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glosa.web import test_audio
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

ADMIN_PASSWORD = "s3cr3t-pw"
CSRF = {"X-Glosa-Admin": "1"}


def _worker(room_id: str = "r1") -> MagicMock:
    worker = MagicMock()
    worker.room.id = room_id
    worker.play_file = AsyncMock()
    return worker


def _make_app(workers: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = MagicMock(admin_password=ADMIN_PASSWORD)
    app.state.workers = workers if workers is not None else {"r1": _worker()}
    app.state.admin_secret = new_admin_secret()
    app.state.session_epoch = 0
    app.include_router(test_audio.api_router)
    return app


def _client(app: FastAPI, *, authenticated: bool = True) -> TestClient:
    client = TestClient(app)
    if authenticated:
        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
    return client


@pytest.fixture(autouse=True)
def _uploads_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "uploads"
    monkeypatch.setattr(test_audio, "UPLOADS_DIR", directory)
    return directory


# ---- sample=en|es -------------------------------------------------------------


def test_sample_en_plays_the_repo_clip() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/test-audio", data={"sample": "en"}, headers=CSRF)

    assert response.status_code == 200
    worker.play_file.assert_awaited_once()
    (path,), _ = worker.play_file.call_args
    assert Path(path).name == "en_clip.opus"
    assert Path(path).is_file()


def test_sample_es_plays_the_other_repo_clip() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/test-audio", data={"sample": "es"}, headers=CSRF)

    assert response.status_code == 200
    (path,), _ = worker.play_file.call_args
    assert Path(path).name == "es_clip.opus"


def test_an_unknown_sample_is_rejected() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/test-audio", data={"sample": "fr"}, headers=CSRF)

    assert response.status_code == 422
    worker.play_file.assert_not_awaited()


# ---- an uploaded file ----------------------------------------------------------


def test_an_uploaded_file_is_saved_and_played(_uploads_dir: Path) -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post(
        "/api/admin/rooms/r1/test-audio",
        files={"file": ("clip.wav", io.BytesIO(b"RIFF...fake audio bytes"), "audio/wav")},
        headers=CSRF,
    )

    assert response.status_code == 200
    worker.play_file.assert_awaited_once()
    (path,), _ = worker.play_file.call_args
    saved = Path(path)
    assert saved.is_file()
    assert saved.parent == _uploads_dir
    assert saved.read_bytes() == b"RIFF...fake audio bytes"


def test_an_uploaded_file_over_50mb_is_413(_uploads_dir: Path) -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)
    oversized = io.BytesIO(b"0" * (test_audio.MAX_UPLOAD_BYTES + 1))

    response = client.post(
        "/api/admin/rooms/r1/test-audio",
        files={"file": ("big.wav", oversized, "audio/wav")},
        headers=CSRF,
    )

    assert response.status_code == 413
    worker.play_file.assert_not_awaited()
    assert list(_uploads_dir.glob("*")) == []  # never left a partial file behind


def test_neither_sample_nor_file_is_422() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/test-audio", data={}, headers=CSRF)

    assert response.status_code == 422
    worker.play_file.assert_not_awaited()


def test_both_sample_and_file_is_422() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post(
        "/api/admin/rooms/r1/test-audio",
        data={"sample": "en"},
        files={"file": ("clip.wav", io.BytesIO(b"x"), "audio/wav")},
        headers=CSRF,
    )

    assert response.status_code == 422
    worker.play_file.assert_not_awaited()


# ---- room lookup, auth, CSRF ----------------------------------------------------


def test_unknown_room_is_404() -> None:
    app = _make_app({})
    client = _client(app)

    response = client.post("/api/admin/rooms/nope/test-audio", data={"sample": "en"}, headers=CSRF)

    assert response.status_code == 404


def test_without_a_session_is_401() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app, authenticated=False)

    response = client.post("/api/admin/rooms/r1/test-audio", data={"sample": "en"}, headers=CSRF)

    assert response.status_code == 401
    worker.play_file.assert_not_awaited()


def test_without_the_csrf_header_is_403() -> None:
    worker = _worker()
    app = _make_app({"r1": worker})
    client = _client(app)

    response = client.post("/api/admin/rooms/r1/test-audio", data={"sample": "en"})

    assert response.status_code == 403
    worker.play_file.assert_not_awaited()
