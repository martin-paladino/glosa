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
    # The pending read is a task that survives a ping: timing out with
    # wait_for() would cancel it, and cancelling an async generator's
    # __anext__() finishes the generator (CaptionBus.subscribe would end and
    # unsubscribe after the first quiet stretch).
    aiter = messages.__aiter__()

    async def next_message() -> CaptionMsg:
        return await aiter.__anext__()

    pending: asyncio.Task[CaptionMsg] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(next_message())
            done, _ = await asyncio.wait({pending}, timeout=ping_interval)
            if not done:
                yield ": ping\n\n"
                continue
            task, pending = pending, None
            try:
                msg = task.result()
            except StopAsyncIteration:
                return
            yield _format_event(msg)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            await aclose()


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
