"""Engine protocol: the common shape of every captioning/translation backend
(Live Translate, transcribe-live, the fake JSONL-replay engine, ...).

Concrete engines live in sibling modules (live_translate.py, transcribe.py,
fake.py) and are constructed by an EngineFactory from an EngineConfig.
"""

from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol

from glosa.models import AudioChunk, EngineConfig, EngineEvent


class Engine(Protocol):
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
        """Tear down the session."""
        ...

    def events(self) -> AsyncIterator[EngineEvent]:
        """Async stream of EngineEvent produced by this session."""
        ...


EngineFactory = Callable[[EngineConfig], Engine]
