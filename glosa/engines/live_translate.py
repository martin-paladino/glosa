"""LiveTranslateEngine: the "fast" engine, Gemini Live Translate
(``gemini-3.5-live-translate-preview``, spec §3.2).

One instance is one Live API session. Config verified live in T0.5
(scripts/verify_live.py): AUDIO response modality, input and output audio
transcription on, ``translationConfig{targetLanguageCode, echoTargetLanguage:
true}``. The translated speech audio is discarded; only the transcriptions
are used, and they arrive as incremental deltas.

Contract for the relay (T3):

- ``connect()`` does not raise on API/network failures. The failure comes out
  of ``events()`` as an ``error`` event followed by ``closed``, like any other
  session failure.
- ``events()`` maps server messages to EngineEvent (``source_delta``,
  ``target_delta``, ``go_away`` with ``meta["time_left_s"]``) and always ends
  with exactly one ``closed``. An exception ends the stream with ``error``
  (``meta = {"code", "retryable"}``, plus ``"payment": True`` for credit
  exhaustion; the policy is ``glosa.engines._gemini_live.classify_error``,
  shared with the transcribe engine) and then ``closed``. Nothing is
  reported as an error after our own ``close()`` or on a normal websocket
  close (1000).
- Call ``close()`` after observing ``closed`` (or when retiring the engine)
  to release the connection. ``close()`` is safe at any time, including
  while ``connect()`` is still in its handshake.
- Cost: ``usage_metadata`` is priced at ``price_per_min`` per minute of input
  audio. ``meta["usd"]`` on an event is the cost *increment* accrued since the
  previous event (sum them, e.g. CostTracker.add). ``usd_total`` is the
  running total for this session.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from google import genai
from google.genai import types
from websockets.exceptions import ConnectionClosed

from glosa.clock import Clock
from glosa.engines._gemini_live import AUDIO_MIME, NORMAL_CLOSE, classify_error, duration_s, error_code
from glosa.models import AudioChunk, EngineConfig, EngineEvent

MODEL = "gemini-3.5-live-translate-preview"
# Measured in T0.5: 975 prompt tokens for 40 s of audio, 3225 for 130 s.
AUDIO_TOKENS_PER_S = 25.0


class LiveTranslateEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        api_key: str,
        clock: Clock,
        price_per_min: float = 0.0368,
        client: Any | None = None,
    ) -> None:
        if not cfg.target_lang:
            raise ValueError("Live Translate needs cfg.target_lang")
        self.cfg = cfg
        self._clock = clock
        self._price_per_min = price_per_min
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any | None = None
        self._connect_error: Exception | None = None
        self._closing = False
        self.usd_total = 0.0
        self._usd_unreported = 0.0

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=self.cfg.target_lang,
                echo_target_language=True,
            ),
        )

    async def connect(self) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                self._client.aio.live.connect(model=MODEL, config=self._live_config())
            )
        except Exception as exc:  # reported by events(), see module docstring
            self._connect_error = exc
            return
        if self._closing:  # close() ran during the handshake: don't leak the websocket
            with contextlib.suppress(Exception):
                await stack.aclose()
            return
        self._session = session
        self._stack = stack

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._session is None or self._closing:
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=chunk.pcm, mime_type=AUDIO_MIME)
            )
        except ConnectionClosed:
            pass  # the receive loop reports why the session ended

    async def end_utterance(self) -> None:
        if self._session is None or self._closing:
            return
        try:
            await self._session.send_realtime_input(audio_stream_end=True)
        except ConnectionClosed:
            pass

    async def close(self) -> None:
        self._closing = True
        stack, self._stack = self._stack, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def events(self) -> AsyncIterator[EngineEvent]:
        if self._connect_error is not None:
            yield self._classify_error(self._connect_error)
        elif self._session is None and not self._closing:
            raise RuntimeError("LiveTranslateEngine.events() called before connect()")
        else:
            try:
                while not self._closing:
                    # receive() stops at each turn_complete: keep re-entering it.
                    async for msg in self._session.receive():
                        for event in self._map_message(msg):
                            yield event
            except Exception as exc:
                if not self._closing and error_code(exc) != NORMAL_CLOSE:
                    yield self._classify_error(exc)
        yield self._with_usage(EngineEvent(kind="closed", t_recv=self._clock.now()))

    def _map_message(self, raw: types.LiveServerMessage | dict) -> list[EngineEvent]:
        msg = (
            raw
            if isinstance(raw, types.LiveServerMessage)
            else types.LiveServerMessage.model_validate(raw)
        )
        now = self._clock.now()
        events: list[EngineEvent] = []
        sc = msg.server_content
        if sc is not None:  # sc.model_turn (translated speech audio) is discarded
            src = sc.input_transcription
            if src is not None and src.text:
                lang = src.language_code or self.cfg.source_lang
                events.append(EngineEvent(kind="source_delta", text=src.text, lang=lang, t_recv=now))
            tgt = sc.output_transcription
            if tgt is not None and tgt.text:
                lang = tgt.language_code or self.cfg.target_lang
                events.append(EngineEvent(kind="target_delta", text=tgt.text, lang=lang, t_recv=now))
        if msg.go_away is not None:
            time_left_s = duration_s(msg.go_away.time_left)
            events.append(EngineEvent(kind="go_away", t_recv=now, meta={"time_left_s": time_left_s}))
        if msg.usage_metadata is not None and msg.usage_metadata.prompt_token_count:
            usd = msg.usage_metadata.prompt_token_count / AUDIO_TOKENS_PER_S / 60 * self._price_per_min
            self.usd_total += usd
            self._usd_unreported += usd
        if events:
            self._with_usage(events[0])
        return events

    def _classify_error(self, exc: BaseException) -> EngineEvent:
        """glosa.engines._gemini_live.classify_error, carrying the usage."""
        return self._with_usage(classify_error(exc, self._clock.now()))

    def _with_usage(self, event: EngineEvent) -> EngineEvent:
        if self._usd_unreported > 0:
            event.meta["usd"] = self._usd_unreported
            self._usd_unreported = 0.0
        return event

