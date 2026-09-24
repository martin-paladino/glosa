"""create_app(): lifespan (rooms start from config.yaml), /api/rooms, the SSE
caption stream, /healthz, and the audience pages and static files mounted
on the same app.

The rooms play the bundled clips (ffmpeg, real time) with engine_mode
"fake": FakeEngine replays a short scripted recording, so no API is called.
SSE is read from a real uvicorn server on a free port, because httpx's
ASGITransport buffers the whole (endless) response.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from glosa.clock import RealClock
from glosa.config import RoomCfg, Settings
from glosa.engines.fake import FakeEngine
from glosa.engines.live_translate import LiveTranslateEngine
from glosa.models import EngineConfig
from glosa.web.app import create_app, make_engine_factory

ROOT = Path(__file__).resolve().parents[2]
EN_CLIP = ROOT / "samples" / "en_clip.opus"
ES_CLIP = ROOT / "samples" / "es_clip.opus"


def _fixture(path: Path) -> Path:
    """A recording that talks right away: a closed phrase every 0.9 s."""
    with path.open("w", encoding="utf-8") as f:
        for i in range(12):
            end = "." if i % 3 == 2 else ""
            t = 0.2 + 0.3 * i
            f.write(json.dumps({"t": t, "kind": "source_delta", "text": f" word{i}{end}"}) + "\n")
            f.write(json.dumps({"t": t + 0.05, "kind": "target_delta", "text": f" palabra{i}{end}"}) + "\n")
    return path


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        gemini_api_key="unused-in-fake-mode",
        admin_password="test-password",
        event_name="Nerdearla 2026",
        engine_mode="fake",
        fake_fixture=str(_fixture(tmp_path / "fast.jsonl")),
        db_path=str(tmp_path / "data" / "glosa.db"),
        rooms=[
            RoomCfg(id="r1", name="Sala Uno", source_type="file", source_url=str(EN_CLIP),
                    language="en", default_targets=["es"]),
            RoomCfg(id="r2", name="Sala Dos", source_type="file", source_url=str(ES_CLIP),
                    language="es", default_targets=["en"]),
        ],
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[str]:
    app = create_app(_settings(tmp_path))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws="none", lifespan="on")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(500):
        if srv.started or task.done():
            break
        await asyncio.sleep(0.01)
    assert srv.started, "uvicorn did not start"
    port = srv.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, timeout=20)


def _client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False)


async def _read_sse(base_url: str, path: str, until: set[str], headers: dict | None = None) -> list[dict]:
    """Read SSE messages from path until every type in `until` was seen."""
    msgs: list[dict] = []
    async with _client(base_url) as client:
        async with client.stream("GET", path, headers=headers or {}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    msgs.append(json.loads(line[len("data: "):]))
                    if until <= {m["type"] for m in msgs}:
                        break
    return msgs


async def test_rooms_api_lists_both_rooms_with_their_talk(server: str) -> None:  # 5.4
    async with _client(server) as client:
        health = await client.get("/healthz")
        rooms = (await client.get("/api/rooms")).json()

    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert [r["slug"] for r in rooms] == ["r1", "r2"]
    r1, r2 = rooms
    assert r1["name"] == "Sala Uno" and r1["langs"] == ["en", "es"]
    assert r2["langs"] == ["es", "en"]
    assert r1["now"] == {"talk_id": "free-r1", "title": "Sesión libre", "speakers": [], "language": "en"}
    assert r2["now"]["language"] == "es"
    assert r1["next"] is None
    assert r1["status"]["talk_id"] == "free-r1"
    assert r1["status"]["state"] in ("green", "yellow")
    assert "public_token" not in json.dumps(rooms)  # qr_only tokens stay secret


async def test_stream_delivers_captions_for_both_rooms_at_once(server: str) -> None:  # 5.4
    r1, r2 = await asyncio.wait_for(
        asyncio.gather(
            _read_sse(server, "/api/stream/r1/es", {"talk", "append", "close"}),
            _read_sse(server, "/api/stream/r2/es", {"talk", "append", "close"}),
        ),
        timeout=15,
    )

    assert r1[0]["type"] == "talk" and r1[0]["data"]["talk_id"] == "free-r1"
    assert any(m["type"] == "append" and "palabra" in m["text"] for m in r1)  # r1: EN talk, ES translation
    assert r2[0]["data"]["talk_id"] == "free-r2"
    assert any(m["type"] == "append" and "word" in m["text"] for m in r2)  # r2: ES talk, its source track
    assert all(isinstance(m["id"], int) and m["ts"] for m in r1 + r2)


async def test_stream_resumes_after_last_event_id(server: str) -> None:
    first = await asyncio.wait_for(_read_sse(server, "/api/stream/r1/es", {"append", "close"}), timeout=15)
    last_id = first[-1]["id"]

    by_header = await asyncio.wait_for(
        _read_sse(server, "/api/stream/r1/es", {"append"}, headers={"Last-Event-ID": str(last_id)}), timeout=15
    )
    by_query = await asyncio.wait_for(
        _read_sse(server, f"/api/stream/r1/es?lastEventId={last_id}", {"append"}), timeout=15
    )

    assert by_header[0]["id"] == last_id + 1
    assert by_query[0]["id"] == last_id + 1


async def test_unknown_rooms_and_bad_languages_are_404(server: str) -> None:
    async with _client(server) as client:
        assert (await client.get("/api/stream/nope/es")).status_code == 404
        assert (await client.get("/api/stream/r1/not a lang")).status_code == 404


async def test_pages_and_static_files_are_served(server: str) -> None:
    async with _client(server) as client:
        index = await client.get("/", headers={"Accept-Language": "es"})
        room = await client.get("/s/r1", headers={"Accept-Language": "es"})
        css = await client.get("/static/css/glosa.css")

    assert index.status_code == 200 and "Sala Uno" in index.text and "Sala Dos" in index.text
    assert "Nerdearla 2026" in index.text  # branding from Settings
    assert room.status_code == 200 and "/api/stream/r1/" in room.text
    assert css.status_code == 200


async def test_rooms_without_a_source_are_listed_idle(tmp_path: Path) -> None:
    settings = _settings(tmp_path, rooms=[RoomCfg(id="quiet", name="Sala Quieta", default_targets=["es"])])
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rooms = (await client.get("/api/rooms")).json()

    assert [(r["slug"], r["now"], r["status"]["state"]) for r in rooms] == [("quiet", None, "idle")]
    assert rooms[0]["langs"] == ["en", "es"]


async def test_room_tokens_survive_a_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path, rooms=[RoomCfg(id="quiet", name="Sala Quieta")])

    tokens = []
    for _ in range(2):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            rooms = await app.state.db.get_rooms()
        tokens.append(rooms[0].public_token)

    assert tokens[0] == tokens[1] and len(tokens[0]) >= 16


def test_engine_factory_by_engine_mode(tmp_path: Path) -> None:
    clock = RealClock()
    cfg = EngineConfig(kind="fast", source_lang="en", target_lang="es")

    fake = make_engine_factory(_settings(tmp_path), clock)(cfg)
    live = make_engine_factory(_settings(tmp_path, engine_mode="live"), clock)(cfg)

    assert isinstance(fake, FakeEngine) and fake.cfg.fixture_path == str(tmp_path / "fast.jsonl")
    assert isinstance(live, LiveTranslateEngine)
    assert live._price_per_min == 0.0368  # Ruling 5: the price comes from Settings.prices


def test_main_serves_with_a_graceful_shutdown_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open SSE streams never end on their own: without a timeout uvicorn
    waits for them forever on SIGTERM and the rooms are never stopped."""
    from glosa.web import app as app_module

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("PORT", "8123")

    app_module.main()

    (args, kwargs), = calls
    assert args == ("glosa.web.app:app_from_env",) and kwargs["factory"] is True
    assert kwargs["port"] == 8123
    assert 0 < kwargs["timeout_graceful_shutdown"] <= 5
