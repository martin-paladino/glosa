"""sse_response: format an async iterator of CaptionMsg as a Server-Sent
Events stream (id:/data: frames), with a periodic comment ping so idle
connections (and any intermediate proxy) stay alive.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import AsyncIterator

from fastapi.responses import StreamingResponse

from glosa.models import CaptionMsg

DEFAULT_PING_INTERVAL_S = 15.0


def _format_event(msg: CaptionMsg) -> str:
    payload = json.dumps(asdict(msg))
    return f"id: {msg.id}\ndata: {payload}\n\n"


async def _event_stream(
    messages: AsyncIterator[CaptionMsg], ping_interval: float
) -> AsyncIterator[str]:
    aiter = messages.__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(aiter.__anext__(), timeout=ping_interval)
        except asyncio.TimeoutError:
            yield ": ping\n\n"
            continue
        except StopAsyncIteration:
            return
        yield _format_event(msg)


def sse_response(
    messages: AsyncIterator[CaptionMsg],
    ping_interval: float = DEFAULT_PING_INTERVAL_S,
) -> StreamingResponse:
    """Wrap an async iterator of CaptionMsg as an SSE StreamingResponse.

    ping_interval is injectable so tests don't have to wait 15s for a ping.
    """
    return StreamingResponse(
        _event_stream(messages, ping_interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
