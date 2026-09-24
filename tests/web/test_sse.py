"""Tests for sse_response: formats a CaptionMsg async iterator as an SSE
stream (id:/data: frames, periodic ping) and, wired into a minimal FastAPI
app around CaptionBus, resumes correctly from a Last-Event-ID header.

httpx's ASGITransport buffers the whole response body, so an endpoint
backed by CaptionBus.subscribe (which never ends on its own) would hang
the test forever. Each test below wraps the bus iterator in `_bounded` so
the stream terminates after a fixed, small number of events.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request

from glosa.captions.bus import CaptionBus
from glosa.clock import FakeClock
from glosa.web.sse import sse_response


async def _bounded(aiter, n: int):
    """Yield at most n items from aiter, then close it (finite SSE stream)."""
    try:
        count = 0
        async for item in aiter:
            yield item
            count += 1
            if count >= n:
                break
    finally:
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            await aclose()


def _make_app(bus: CaptionBus, events_per_request: int) -> FastAPI:
    app = FastAPI()

    @app.get("/rooms/{room_id}/captions/{lang}")
    async def captions(room_id: str, lang: str, request: Request):
        raw_last_id = request.headers.get("Last-Event-ID")
        last_event_id = int(raw_last_id) if raw_last_id is not None else None
        source = bus.subscribe(room_id, lang, last_event_id)
        return sse_response(_bounded(source, events_per_request))

    return app


async def _get_sse_text(app: FastAPI, path: str, headers: dict | None = None) -> str:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=headers or {})
    assert response.status_code == 200
    return response.text


async def test_sse_stream_contains_id_and_data_lines() -> None:
    clock = FakeClock(start=0.0)
    bus = CaptionBus(clock=clock)
    bus.publish("room1", "es", "append", seg=0, text="Hola")
    bus.publish("room1", "es", "append", seg=0, text=" mundo")
    app = _make_app(bus, events_per_request=2)

    body = await _get_sse_text(app, "/rooms/room1/captions/es")

    assert "id: 1\n" in body
    assert "id: 2\n" in body
    data_lines = [line for line in body.splitlines() if line.startswith("data: ")]
    assert len(data_lines) == 2
    first_payload = json.loads(data_lines[0][len("data: ") :])
    assert first_payload == {
        "id": 1,
        "type": "append",
        "seg": 0,
        "text": "Hola",
        "data": None,
        "ts": clock.wall().timestamp(),
    }


async def test_sse_resumes_from_last_event_id_header() -> None:
    bus = CaptionBus()
    for i in range(1, 6):
        bus.publish("room1", "es", "append", seg=0, text=f"word{i}")
    app = _make_app(bus, events_per_request=2)

    body = await _get_sse_text(
        app, "/rooms/room1/captions/es", headers={"Last-Event-ID": "3"}
    )

    assert "id: 4\n" in body
    assert "id: 5\n" in body
    assert "id: 1\n" not in body
    assert "id: 2\n" not in body
    assert "id: 3\n" not in body


async def test_sse_sends_ping_when_no_messages_arrive() -> None:
    async def never_yields():
        if False:
            yield  # pragma: no cover - makes this an async generator
        await asyncio.sleep(3600)

    response = sse_response(never_yields(), ping_interval=0.01)
    body_iter = response.body_iterator

    first_chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=1.0)

    assert first_chunk == ": ping\n\n"
    await body_iter.aclose()
