"""tests/load/test_fanout.py: small, in-process caption fan-out smoke test
(Task 15a). The CI-friendly sibling of ``bench/load_test.py`` (50 rooms /
500 clients / 60 s against a real ``python -m glosa.web.app`` subprocess --
see ``bench/load-results.md`` for those numbers): 3 rooms / 20 clients / 5 s,
in-process (``create_app`` + a real uvicorn server on a free port, same
pattern as ``tests/web/test_public_api.py``'s ``server`` fixture; SSE
responses never end, so httpx's ``ASGITransport`` -- which buffers the whole
response -- cannot be used).

Asserts what the plan's "no losses" criterion means (glosa/captions/bus.py):
every client subscribed to the same (room, lang) track receives the exact
same, gap-free sequence of message ids. Not a timing/CPU benchmark -- that
is bench/load_test.py's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from glosa.config import RoomCfg, Settings
from glosa.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]
EN_CLIP = ROOT / "samples" / "en_clip.opus"

ROOMS = 3
CLIENTS = 20
DURATION_S = 5.0


def _fast_fixture(path: Path) -> Path:
    """A recording that talks continuously for well past DURATION_S, so a
    short-lived test still sees plenty of append/close events on every
    track (the bundled samples/fixtures/lt_en.jsonl has gaps up to 9.2 s,
    too sparse for a 5 s window)."""
    with path.open("w", encoding="utf-8") as f:
        t = 0.1
        i = 0
        while t < DURATION_S + 3:
            end = "." if i % 4 == 3 else ""
            f.write(json.dumps({"t": round(t, 3), "kind": "source_delta", "text": f" word{i}{end}"}) + "\n")
            f.write(json.dumps({"t": round(t + 0.03, 3), "kind": "target_delta", "text": f" palabra{i}{end}"}) + "\n")
            t += 0.15
            i += 1
    return path


def _settings(tmp_path: Path) -> Settings:
    fixture = _fast_fixture(tmp_path / "fast.jsonl")
    rooms = [
        RoomCfg(
            id=f"room-{i}",
            name=f"Load room {i}",
            source_type="file",
            source_url=str(EN_CLIP),
            language="en",
            default_targets=["es"],
        )
        for i in range(ROOMS)
    ]
    return Settings(
        gemini_api_key="unused-in-fake-mode",
        admin_password="test-admin-password",
        engine_mode="fake",
        fake_fixture=str(fixture),
        db_path=str(tmp_path / "data" / "glosa.db"),
        rooms=rooms,
    )


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


async def _read_track(base_url: str, slug: str, lang: str, duration: float) -> list[int]:
    """Every id received on this (room, lang) track's SSE stream, in
    delivery order, for `duration` seconds."""
    ids: list[int] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        async with client.stream("GET", f"/api/stream/{slug}/{lang}") as resp:
            assert resp.status_code == 200

            async def _reader() -> None:
                ev_id: str | None = None
                async for line in resp.aiter_lines():
                    if line.startswith(":"):
                        continue
                    if line.startswith("id:"):
                        ev_id = line[3:].strip()
                    elif line == "":
                        if ev_id is not None:
                            ids.append(int(ev_id))
                        ev_id = None

            reader = asyncio.create_task(_reader())
            done, pending = await asyncio.wait({reader}, timeout=duration)
            if reader in pending:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            else:
                exc = reader.exception()
                if exc is not None:
                    raise exc
    return ids


async def test_fanout_no_loss_across_clients(server: str) -> None:
    """3 rooms / 20 clients / 5 s: no client of a track is missing, sees
    out-of-order, or disagrees with another client of the same track on
    the message id sequence (the plan's "no losses" criterion)."""
    async with httpx.AsyncClient(base_url=server, timeout=10, trust_env=False) as client:
        rooms = (await client.get("/api/rooms")).json()
    assert len(rooms) == ROOMS

    tracks = [(r["slug"], lang) for r in rooms for lang in r["langs"]]
    assignments = [tracks[i % len(tracks)] for i in range(CLIENTS)]

    results = await asyncio.gather(*(_read_track(server, slug, lang, DURATION_S) for slug, lang in assignments))

    by_track: dict[tuple[str, str], list[list[int]]] = {}
    for (slug, lang), ids in zip(assignments, results):
        by_track.setdefault((slug, lang), []).append(ids)

    assert len(by_track) == len(tracks)
    for track, client_ids in by_track.items():
        for ids in client_ids:
            assert ids, f"{track}: a client received no messages at all"
            assert ids == sorted(ids), f"{track}: ids out of order: {ids}"
            assert all(b - a == 1 for a, b in zip(ids, ids[1:])), f"{track}: gap in ids: {ids}"
        longest = max(client_ids, key=len)
        for ids in client_ids:
            n = len(ids)
            assert ids == longest[:n], f"{track}: clients disagree on the id sequence"
