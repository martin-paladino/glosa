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
- the closed segment's text is cut off the next segment. In the live runs
  of 2026-09-24 (T10-wiring, 60 s of es_clip.opus through a RoomWorker,
  twice), 0.3-0.5 s after 3 of the 8 finals the next segment's first
  interim was the closed segment's last interim again, alone or with the
  new words after it. Taken as is, it flashed the old text on screen, got
  the whole utterance translated twice, and pushed the translation lane's
  committed prefix (counted in words) past the new segment's own words, so
  its end went untranslated ("y en Azure."). So after a final, until the
  first clean interim (one that does not start with it) or the next final
  (no timer: a stale repeat can come late):

  - an interim that is only (a start of) the closed segment's last
    interim or final is dropped;
  - one that starts with the closed segment's text (word by word, each
    word the one at that position in the last interim or the final, at
    least as far as the shorter of the two and ``STALE_MIN_WORDS`` words:
    the server may repeat an interim newer than the last one it sent us)
    loses that start, and so does the final of that segment if it starts
    the same way;
  - a final that only repeats the closed segment (``STALE_MIN_WORDS`` or
    more words), with nothing of a new segment shown, is dropped.

  Words are compared ignoring case and punctuation; a token that is only
  punctuation ("—", "¿") is skipped on both sides, and a repeat may stop
  in the middle of a word. The third live run showed the server often
  glues the old text to the new words ("…de teams,the labels",
  "cluster?O dentro", "Azure.que tienen"), so the cut reads the raw text:
  the closed segment's words may end at punctuation followed by a letter.
  Every drop or cut is logged at INFO. A new segment that really starts
  with the words of the one before shows up with its next interim (~0.5 s).
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
        # (last interim, final) of the segment just closed, while its text may come back (see above)
        self._just_closed: tuple[str, str] | None = None
        self._cut_open = False  # stale words were cut off the open segment's interims
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
            text = self._unstale(interim.text) if interim is not None and interim.text else ""
            if text and text != self._open_text:
                self._open_text = text
                lang = interim.language_code or self.cfg.source_lang
                events.append(EngineEvent(kind="source_delta", text=text, lang=lang, t_recv=now, meta={"interim": True}))
            final = sc.input_transcription
            text = self._unstale_final(final.text or "") if final is not None else None
            if text is not None and (text or self._open_text is not None):
                self._just_closed = (self._open_text or "", text)
                self._open_text = None
                self._cut_open = False
                lang = final.language_code or self.cfg.source_lang
                events.append(EngineEvent(kind="source_final", text=text, lang=lang, t_recv=now))
        if msg.go_away is not None:
            time_left_s = duration_s(msg.go_away.time_left)
            events.append(EngineEvent(kind="go_away", t_recv=now, meta={"time_left_s": time_left_s}))
        if events:
            self._with_usage(events[0])
        return events

    def _unstale(self, text: str) -> str:
        """An interim without the closed segment's text at its start, or ""
        if that is all it is (see the module docstring)."""
        if self._just_closed is None:
            return text
        cut = _stale_cut(text, self._just_closed)
        if cut is None:
            self._just_closed = None  # a clean interim: the server moved on
            return text
        if cut >= len(text):
            log.info("transcribe: dropped a stale interim (the closed segment's text): %r", text)
            return ""
        log.info("transcribe: cut %d stale words off an interim: %r", _count_words(text[:cut]), text)
        self._cut_open = True
        return text[cut:]

    def _unstale_final(self, text: str) -> str | None:
        """A final without the closed segment's text at its start, if the
        interims of its segment lost it too; None for a final that only
        repeats the closed segment."""
        if self._just_closed is None or not text:
            return text
        cut = _stale_cut(text, self._just_closed)
        if cut is None:
            return text
        if cut >= len(text):
            if self._open_text is None and _count_words(text) >= STALE_MIN_WORDS:
                log.info("transcribe: dropped a final that repeats the closed segment: %r", text)
                return None
            return text
        if not self._cut_open:
            return text
        log.info("transcribe: cut %d stale words off a final: %r", _count_words(text[:cut]), text)
        return text[cut:]

    def _classify_error(self, exc: BaseException) -> EngineEvent:
        """glosa.engines._gemini_live.classify_error, carrying the usage."""
        return self._with_usage(classify_error(exc, self._clock.now()))

    def _with_usage(self, event: EngineEvent) -> EngineEvent:
        if self._usd_unreported > 0:
            event.meta["usd"] = self._usd_unreported
            self._usd_unreported = 0.0
        return event


def _norm(word: str) -> str:
    """A word for comparing interims: lower case, no punctuation ("" for a
    token that is only punctuation)."""
    return "".join(ch for ch in word.casefold() if ch.isalnum())


def _count_words(text: str) -> int:
    return sum(1 for word in text.split() if _norm(word))


def _stale_cut(text: str, closed: tuple[str, str]) -> int | None:
    """Where the closed segment's text ends at the start of ``text``: None
    if ``text`` does not start with it; ``len(text)`` if ``text`` is only (a
    start of) its last interim or final, possibly ending mid-word; else the
    index where the new words start, past any punctuation.

    ``text`` starts with the closed segment's text when, word by word, each
    of its words is the word at that position in the last interim or in the
    final, at least as far as the shorter of the two, and at least
    ``STALE_MIN_WORDS`` words: a stale text can be an interim between the
    last one the engine sent and the final. The match reads the raw text,
    since the server glues the old text to the new words ("…de
    teams,the labels", "cluster?O")."""
    norms = [n for n in map(_norm, text.split()) if n]
    if not norms:
        return len(text)  # only punctuation
    refs = [ref for ref in ([n for n in map(_norm, prev.split()) if n] for prev in closed) if ref]
    if not refs:
        return None
    k = len(norms)
    for old in refs:
        if k <= len(old) and norms[: k - 1] == old[: k - 1] and old[k - 1].startswith(norms[-1]):
            return len(text)
    full = max(min(len(ref) for ref in refs), STALE_MIN_WORDS)
    i, matched, end = 0, 0, None
    while True:
        nxt = None
        for word in dict.fromkeys(ref[matched] for ref in refs if matched < len(ref)):
            nxt = _consume_word(text, i, word)
            if nxt is not None:
                break
        if nxt is None:
            break
        i, matched = nxt, matched + 1
        if matched >= full:
            end = i
    if end is None:
        return None
    while end < len(text) and not text[end].isalnum():
        end += 1
    return end


def _consume_word(text: str, i: int, word: str) -> int | None:
    """The index in ``text`` right after ``word`` (``_norm``ed) read from
    ``i``: punctuation before it or inside it ("k8s.io") and case are
    skipped; a space inside it or a longer word ("clustering" for
    "cluster") is no match (None)."""
    n = len(text)
    while i < n and not text[i].isalnum():
        i += 1
    j = 0
    while j < len(word):
        if i >= n or text[i].isspace():
            return None
        if text[i].isalnum():
            folded = text[i].casefold()
            if not word.startswith(folded, j):
                return None
            j += len(folded)
        i += 1
    if i < n and text[i].isalnum():
        return None
    return i
