"""TranscribeLiveEngine: the speech-to-text half of the "glossary" engine,
Gemini ``gemini-3.5-transcribe-live``. Translation is done
downstream on the text (glosa.text.translator).

One instance is one Live API session, configured as
``inputAudioTranscription{mode: VERBATIM, languageCodes: [source_lang],
customVocabulary: <up to 100 terms of cfg.vocabulary>}``: stripped, and
deduped ignoring case before the cap. VERBATIM because SMART mode ignores
the custom vocabulary. No response modality is needed.

What the server sends (probe of samples/es_clip.opus, 2026-09-24; the
recording is samples/fixtures/tr_es.jsonl):

- ``interimInputTranscription``: the whole text of the open segment so far
  (cumulative, it rewrites; it is not a delta), about every 0.5 s. No
  languageCode.
- ``inputTranscription``: the segment's final text, then
  ``generationComplete``. It arrives 0.4-0.5 s after we send
  ``audio_stream_end`` (0.8-0.9 s after the end of speech, counting the
  VAD's 400 ms), and the session keeps taking audio afterwards.
- ``voiceActivity`` (server VAD start/end): ignored. No ``usageMetadata``.

The server's own VAD freezes on monologues (finals every 12-18 s, interims
stuck for 20-38 s, measured before the vibeathon), so the client ends the
utterances: ``end_utterance()`` sends ``audio_stream_end``, and the caller
invokes it on each VAD ``pause`` (hybrid VAD).

Events:

- an interim -> ``source_delta`` with ``meta["interim"] = True`` whose text
  is the open segment's whole text: it replaces the open line, it is not
  appended. An interim identical to the previous one is not emitted again
  (it is no progress, and must not feed the stall watchdog).
- a final -> ``source_final``: the segment's final text; it closes the open
  segment. An empty final closes an open segment with ``""`` (the interim
  was not speech after all); with nothing open it is dropped.
- the closed segment's text is cut off the next interims. In the live runs
  of 2026-09-24 (T10-wiring, 60 s of es_clip.opus through a RoomWorker,
  twice), 0.3-0.5 s after 3 of the 8 finals the next segment's first
  interim was the closed segment's last interim again, alone or with the
  new words after it. Taken as is, it flashed the old text on screen, got
  the whole utterance translated twice, and pushed the translation lane's
  committed prefix (counted in words) past the new segment's own words, so
  its end went untranslated ("y en Azure."). So within
  ``STALE_INTERIM_S`` of a final, until an interim comes that does not
  start with it: an interim that is only (a start of) the closed
  segment's last interim or final is dropped, and one that starts with all
  of it (``STALE_MIN_WORDS`` or more words; compared word by word,
  ignoring case and punctuation) loses that start. A new segment that
  really starts with the words of the one before shows up with its next
  interim (~0.5 s) or after the window.
- ``go_away`` with ``meta["time_left_s"]``, like LiveTranslateEngine.

Same contract as LiveTranslateEngine for the relay: ``connect()`` never
raises (a failure comes out of ``events()`` as ``error`` then ``closed``);
``events()`` always ends with exactly one ``closed``; errors are classified
by the same shared policy (``glosa.engines._gemini_live.classify_error``:
402/payment stop, 1007/1008 hard unless the 1008 reason mentions GoAway,
429/5xx/network retryable) and nothing is an error after our own
``close()`` or on a normal close (1000). ``close()`` is idempotent
and safe during the handshake; call it after ``closed``.

Cost: priced at ``price_per_min`` per minute of audio actually sent (the
server reports no usage). ``meta["usd"]`` on an event is the increment since
the previous event (it rides on the next event, ``closed`` at the latest);
``usd_total`` is this session's running total.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from google import genai
from google.genai import types
from websockets.exceptions import ConnectionClosed

from glosa.clock import Clock
from glosa.engines._gemini_live import AUDIO_MIME, NORMAL_CLOSE, classify_error, duration_s, error_code, vocabulary
from glosa.models import AudioChunk, EngineConfig, EngineEvent

log = logging.getLogger(__name__)

MODEL = "gemini-3.5-transcribe-live"
MAX_VOCABULARY = 100  # spec: customVocabulary gets at most 100 terms
STALE_INTERIM_S = 1.5  # after a final, the closed segment's text in an interim is stale (see above)
STALE_MIN_WORDS = 3  # shorter closed segments are never cut off an interim: "Sí." then "Sí, claro"
_BYTES_PER_S = 16000 * 2  # PCM16 mono @ 16 kHz


class TranscribeLiveEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        api_key: str,
        clock: Clock,
        price_per_min: float = 0.009,
        client: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self._price_per_min = price_per_min
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any | None = None
        self._connect_error: Exception | None = None
        self._closing = False
        self._ended = False  # events() is over: the session takes no more audio
        self._audio_since_end = False  # audio sent since the last audio_stream_end
        self._open_text: str | None = None  # last interim of the open segment
        self._just_closed: tuple[str, str, float] | None = None  # (last interim, final, t) of the closed one
        self.usd_total = 0.0
        self._usd_unreported = 0.0

    def _vocabulary(self) -> list[str] | None:
        terms = vocabulary(self.cfg.vocabulary)
        if len(terms) > MAX_VOCABULARY:
            log.warning(
                "transcribe: %d vocabulary terms, only the first %d are sent", len(terms), MAX_VOCABULARY
            )
            terms = terms[:MAX_VOCABULARY]
        return terms or None

    def _live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            input_audio_transcription=types.AudioTranscriptionConfig(
                mode=types.AudioTranscriptionConfigMode.VERBATIM,
                language_codes=[self.cfg.source_lang],
                custom_vocabulary=self._vocabulary(),
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

    def _can_send(self) -> bool:
        return self._session is not None and not self._closing and not self._ended

    async def send_audio(self, chunk: AudioChunk) -> None:
        if not self._can_send():
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=chunk.pcm, mime_type=AUDIO_MIME)
            )
        except ConnectionClosed:
            return  # the receive loop reports why the session ended
        self._audio_since_end = True
        usd = len(chunk.pcm) / _BYTES_PER_S / 60 * self._price_per_min
        self.usd_total += usd
        self._usd_unreported += usd

    async def end_utterance(self) -> None:
        if not self._can_send() or not self._audio_since_end:
            return
        try:
            await self._session.send_realtime_input(audio_stream_end=True)
        except ConnectionClosed:
            return
        self._audio_since_end = False

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
            raise RuntimeError("TranscribeLiveEngine.events() called before connect()")
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
        self._ended = True
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
        if sc is not None:
            # Interim before final: if one message ever carried both, a lost
            # interim costs nothing (the next one repeats the whole segment),
            # but a segment left open after its final would linger.
            interim = sc.interim_input_transcription
            text = self._unstale(interim.text, now) if interim is not None and interim.text else ""
            if text and text != self._open_text:
                self._open_text = text
                lang = interim.language_code or self.cfg.source_lang
                events.append(EngineEvent(kind="source_delta", text=text, lang=lang, t_recv=now, meta={"interim": True}))
            final = sc.input_transcription
            if final is not None and (final.text or self._open_text is not None):
                self._just_closed = (self._open_text or "", final.text or "", now)
                self._open_text = None
                lang = final.language_code or self.cfg.source_lang
                events.append(EngineEvent(kind="source_final", text=final.text or "", lang=lang, t_recv=now))
        if msg.go_away is not None:
            time_left_s = duration_s(msg.go_away.time_left)
            events.append(EngineEvent(kind="go_away", t_recv=now, meta={"time_left_s": time_left_s}))
        if events:
            self._with_usage(events[0])
        return events

    def _unstale(self, text: str, now: float) -> str:
        """``text`` without the closed segment's text at its start, or ""
        if that is all it is (see the module docstring)."""
        closed = self._just_closed
        if closed is None:
            return text
        if now - closed[2] > STALE_INTERIM_S:
            self._just_closed = None
            return text
        words = text.split()
        key = [_norm(word) for word in words]
        cut = 0
        for prev in closed[:2]:
            old = [_norm(word) for word in prev.split()]
            if not old:
                continue
            if len(key) <= len(old) and key == old[: len(key)]:
                return ""
            if len(old) >= STALE_MIN_WORDS and key[: len(old)] == old:
                cut = max(cut, len(old))
        if not cut:
            self._just_closed = None  # a clean interim: the server moved on
            return text
        return " ".join(words[cut:])

    def _classify_error(self, exc: BaseException) -> EngineEvent:
        """glosa.engines._gemini_live.classify_error, carrying the usage."""
        return self._with_usage(classify_error(exc, self._clock.now()))

    def _with_usage(self, event: EngineEvent) -> EngineEvent:
        if self._usd_unreported > 0:
            event.meta["usd"] = self._usd_unreported
            self._usd_unreported = 0.0
        return event


def _norm(word: str) -> str:
    """A word for comparing interims: lower case, no punctuation."""
    return "".join(ch for ch in word.casefold() if ch.isalnum())
