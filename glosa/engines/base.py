"""Engine protocol: the common shape of every captioning/translation backend
(Live Translate, transcribe-live, the fake JSONL-replay engine, ...).

Concrete engines live in sibling modules (live_translate.py, transcribe.py,
fake.py) and are constructed by an EngineFactory from an EngineConfig.
"""

from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol

from glosa.models import AudioChunk, EngineConfig, EngineEvent


class Engine(Protocol):
    """One engine session.

    Lifecycle contract:
      - connect() opens the session. API/network failures do not raise: they
        come out of events() as an "error" event (meta {"code", "retryable"})
        followed by "closed".
      - events() always ends with exactly one "closed" event.
      - The consumer must call close() after observing "closed" (or when
        retiring the engine for any other reason) to release the connection.
        close() is safe at any time, even while connect() is in flight.
      - meta["usd"] on any event is a cost INCREMENT since the previous event;
        consumers sum it.
    """

    async def connect(self) -> None:
        """Open the underlying session (e.g. the Live API websocket)."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Push one ~100ms PCM chunk into the session."""
        ...

    async def end_utterance(self) -> None:
        """Signal the end of the current utterance (e.g. audio_stream_end)."""
        ...

    async def close(self) -> None:
        """Tear down the session and release the connection. Idempotent; call
        it after observing "closed" too."""
        ...

    def events(self) -> AsyncIterator[EngineEvent]:
        """Async stream of EngineEvent produced by this session; ends with
        exactly one "closed". meta["usd"] values are increments (sum them)."""
        ...


EngineFactory = Callable[[EngineConfig], Engine]
