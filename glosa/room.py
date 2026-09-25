"""RoomWorker: the live pipeline of one room.

::

    AudioIngest -> EnergyVad -> SessionRelay (the talk's engine)
      -> source text: CaptionAssembler per (engine session, language), or "set"
      -> translations: Live Translate's own, and/or the translation lane
      -> CaptionBus.publish -> db.save_segment when a segment closes

Per 100 ms chunk (Ruling 23): ``vad.process(chunk)``, then
``relay.feed(chunk, voiced=vad.in_speech)`` (the watchdog needs ``voiced``),
then ``relay.on_vad(ev)`` for each VAD event, then ``lane.tick()``. A
separate task consumes ``relay.events()``: ``source_delta``/``source_final``
go to the talk's language, ``target_delta`` to the first translation
language (routed by kind, not by ``ev.lang``), and every ``meta["usd"]``
increment is added to the room's cost.

Engines (glosa/room_text.py)
    Each talk runs its own ``Talk.engine``; a free session, the default for
    its language (``es`` glossary, else ``Settings.default_engine_en``).

    - ``fast``: Live Translate transcribes and translates into the first
      target. If the talk has more targets, the translation lane translates
      the source deltas into the rest (Task 11's extra languages); each VAD
      pause closes the lane's open utterance. At a session rotation, the
      draining session's late source text is still shown but does not reach
      the lane once the new session has spoken (the lane follows one
      session at a time): those words miss the extra languages.
    - ``glossary``: transcribe-live (verbatim, the talk's glossary as its
      vocabulary, up to 100 terms) gives the source text; the lane
      translates it into every target. Each VAD pause calls
      ``relay.end_utterance()`` (the hybrid VAD: transcribe-live's own
      freezes on monologues), which brings the utterance's final.

"set" segments (glossary engine)
    An interim ``source_delta`` holds the open utterance's whole text: it
    is published as ``set`` (it replaces the open segment on screen), never
    twice with the same text, and its final is published as ``set`` (if it
    changed) and ``close``; the segment is stored with the final text. Such
    a segment has no idle close: it waits for its final. Only the text of
    the newest session that spoke is used: when a newer session (a
    rotation, a reconnect) speaks, the older one's open segment closes with
    the text it shows, the lane closes that utterance too, and the older
    session's late text is dropped.

Fallback to the glossary engine (case 10.5)
    A fast talk switches to the glossary engine when its Live Translate
    fails 3 times within 2 min (errors, failed connects, stalls, sessions
    that died; not the admin's "Reconectar" nor a 402:
    ``room_text.FlapDetector``) or halts on a non-retryable error (Ruling
    49), both checked on each tick. Not on a halt for a refused key (a 401,
    or an error that says "API key" or "API_KEY_INVALID": Gemini reports a
    bad key as a 400): the glossary engine would be refused too; the room
    goes red with an ``engine_auth`` error event instead (Ruling 49a: a 403
    or "permission denied" alone can be a preview model out of reach, so it
    does fall back). A 402 only blocks (payment).

    The switch is hot (Ruling 48, ``_swap_engine``): the audio loop, the
    VAD and the talk go on untouched (no ingest restart, no new "talk"
    message, nothing captioned again); only the engine side (relay, lane,
    Translator) is replaced. The new relay gets the audio the halted one
    was holding and takes the rest at once (it holds 2 s while it
    connects), so no chunk is lost; it numbers its sessions after the old
    one's; the old relay's late events only count for the cost. If the new
    side cannot be built, the old one stays and the admin log gets an error,
    "fallback_failed". On success ``talk.engine`` is saved (a failure to
    save is logged), so it never goes back to fast by itself, and the admin
    log gets a warning, "fallback: glossary engine".

Translation lane
    A LivePipeline (glosa/room_text.TranslationLane) with a Translator of the
    run's own, closed with it (FakeTranslator with ``engine_mode: fake``). Each segment it delivers
    becomes one ``append`` + ``close`` in its language, in order, stored
    with the times of its source's cut; a failed one (no text) is not shown.
    The source is always published before it is fed to the lane, so a
    translation never shows up ahead of its source. At the end of a run the
    lane is drained (up to 10 s: the last words get translated) and closed:
    no pipeline task outlives its run.

Segments
    The relay interleaves the active and the draining session (each event
    carries ``meta["session"]``). Each (session, language) gets its own
    CaptionAssembler, so the tail of one session and the start of the next
    never end up in the same segment; the assemblers' seg numbers are
    remapped to one counter per language that lives as long as the worker
    (a restarted pipeline never reuses a seg id). An assembler's ops are
    applied without awaiting, so the ticker never sees half of them;
    closed segments are saved afterwards. A
    segment closes on terminal punctuation (the assembler), on
    ``max_chars``, when its session reports an error, when the talk ends,
    or after ``IDLE_CLOSE_S`` without new text in it. The last one is the
    "pause" that solidifies a phrase: the engine's output trails the voice
    by 2-4 s, so a VAD pause would cut phrases before their tail arrives.
    2.5 s comes from the 10-min Live Translate recording
    (samples/fixtures/lt_en.jsonl): 1 of the 586 gaps between translated
    deltas that do not end a sentence is longer (the 9.2 s warm-up hole).
    The relay does not surface a session's own ``closed``, so an assembler
    of an older session is dropped once its segment is closed.

    Closed segments are stored with ``t_start``/``t_end`` in seconds since
    the talk started on this worker (the first append and the close).

Costs
    Engine usd (``live_translate`` or ``transcribe``, units: minutes) and
    the Translator's (``translate``, units: segments) add to the room's
    cost and are written as one costs row per component every
    ``COST_FLUSH_S`` and at the end of a run.

Sources
    ``start(talk)`` plays the room's configured source; ``play_file(path)``
    replaces the source of the running talk (or starts the free session).
    When a source ends by itself, the pipeline stops:
      - a clean end (a file that finished) first feeds ``tail_s`` s of
        silence so the engine can finish the last phrase, then ends the
        talk: the room goes idle;
      - a source that died (AudioIngest gave up after its restarts, ~31 s
        without audio, so the engine is long done) keeps the talk on and
        turns the room red ("source is down") until ``play_file()``,
        ``start()`` or ``stop()``.

Free session (MVP, no agenda)
    ``start(None)`` opens a synthetic talk titled "Sesión libre", in the
    room's ``language`` (config.yaml), translated to its first
    ``default_targets`` entry, engine "fast". Each run gets its own id,
    ``free-<room id>-<YYYYmmddTHHMMSS>`` in the event timezone (Ruling 27),
    so exports never mix runs; its ``start`` is in that timezone too.

Talk end hook
    ``on_talk_end(talk)`` (optional) runs as a background task each time a
    talk ends, whatever ended it: ``stop()``, a ``start()`` that replaces
    it, or its source finishing. The talk comes ``done``, with its
    ``actual_end``. A failing hook is logged and changes nothing; ``stop()``
    does not wait for it (``drain_hooks()`` does, for shutdown).

Clock
    Timers (idle close, status, cost batching) run on ``clock.sleep``, so
    the clock must really wait: RealClock, or a test clock the test drives.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glosa.audio.ingest import CHUNK_BYTES, CHUNK_S, AudioIngest, StationHub
from glosa.audio.vad import EnergyVad
from glosa.captions.assembler import CaptionAssembler
from glosa.captions.bus import CaptionBus
from glosa.clock import Clock
from glosa.config import Settings
from glosa.db import FREE_TALK_PREFIX, Database
from glosa.engines._gemini_live import vocabulary
from glosa.engines.base import EngineFactory
from glosa.engines.relay import SessionRelay
from glosa.engines.transcribe import MAX_VOCABULARY
from glosa.metrics import LatencyTracker, RoomHealth
from glosa.models import AudioChunk, EngineConfig, EngineEvent, Room, RoomStatus, Talk
from glosa.room_text import (
    EngineKind,
    FlapDetector,
    TranslationLane,
    default_engine,
    engine_of,
    target_lang,
    translation_langs,
)
from glosa.text.pipeline import TranslatedSegment, TranslateFn
from glosa.text.translator import FakeTranslator, Translator

log = logging.getLogger(__name__)

TICK_S = 0.5  # housekeeping period: idle close, status, events, cost
IDLE_CLOSE_S = 2.5  # see the module docstring
TAIL_S = 5.0  # silence fed after a source ends, for the engine's last output
COST_FLUSH_S = 10.0  # usd increments are batched into one costs row this often
LEVEL_WINDOW_CHUNKS = 50  # health uses the loudest chunk of the last 5 s
RECENT_RECONNECT_S = 60.0
CONSUMER_GRACE_S = 10.0
FREE_SESSION_TITLE = "Sesión libre"
FREE_SESSION_PREFIX = FREE_TALK_PREFIX  # free session ids: free-<room id>-<YYYYmmddTHHMMSS>
FREE_SESSION_HOURS = 12
MIN_LEVEL_DB = -96.0
SILENCE = bytes(CHUNK_BYTES)
# A Talk's agenda fields (the admin can edit them); the rest is runtime state.
AGENDA_FIELDS = ("title", "speakers", "language", "targets", "engine", "start", "end", "abstract", "tags", "glossary")
COST_ENGINE = {"fast": "live_translate", "glossary": "transcribe"}  # costs.component, units: minutes
COST_TRANSLATE = "translate"  # units: translated segments
# A refused API key: no fallback, the glossary engine uses the same key (Ruling 49a).
# Gemini says a bad key with a 400; a 403 or "permission denied" alone can be
# a preview model the key cannot reach, where the glossary engine helps.
AUTH_CODES = (401,)
AUTH_HINTS = ("api key", "api_key_invalid")


class Ingest(Protocol):
    restarts: int
    last_error: str | None

    def chunks(self) -> Any: ...


IngestFactory = Callable[[str, str, bool, Clock], Ingest]
# Called, as a background task, with each talk that ends (Task 11: exports).
TalkEndHook = Callable[[Talk], Awaitable[None]]


def is_free_talk(talk_id: str) -> bool:
    """Whether ``talk_id`` is a free session's (``RoomWorker.free_talk()``),
    not an agenda talk: the agenda and the autopilot ignore those."""
    return talk_id.startswith(FREE_SESSION_PREFIX)


__all__ = ["RoomWorker", "is_free_talk", "target_lang"]


@dataclass
class _OpenSeg:
    seg: int  # published id
    local: int  # the assembler's own seg number (-1 for a "set" segment)
    t_start: float
    last_at: float
    text: str = ""
    t_end: float | None = None  # set when it closes
    replaces: bool = False  # published with "set" (whole text), not "append"


class _Track:
    """One language of the talk: the assembler of each engine session."""

    def __init__(self, lang: str, kind: str) -> None:
        self.lang = lang
        self.kind = kind  # "source" | "translation"
        self.assemblers: dict[int, CaptionAssembler] = {}
        self.open: dict[int, _OpenSeg] = {}  # session -> its open segment

    def sessions(self) -> set[int]:
        return set(self.assemblers) | set(self.open)


_Closed = list[tuple[_Track, _OpenSeg]]  # closed segments, still to be saved


@dataclass(eq=False)
class _EngineSide:
    """What a run swaps when it changes engines (RoomWorker._build_engine)."""

    kind: EngineKind
    relay: SessionRelay
    lane: TranslationLane | None = None
    translator: Translator | None = None


@dataclass(eq=False)
class _Run:
    """The pipeline of the talk being captioned."""

    talk: Talk
    target: str  # the first translation language (Live Translate's, with "fast")
    source: tuple[str, str, bool]  # what the audio loop plays: (source_type, source_url, realtime)
    vad: EnergyVad
    tracks: dict[str, _Track]
    t0: float
    # The engine side, set by RoomWorker._apply_engine (again on a hot swap):
    engine: str = "fast"  # "fast" | "glossary"
    relay: SessionRelay = None  # type: ignore[assignment]
    lane: TranslationLane | None = None  # translations the engine does not make itself
    translator: Translator | None = None  # the lane's, when the run made its own (closed with it)
    text_session: int = 0  # the engine session whose source text is in use (the newest that spoke)
    flaps: FlapDetector = field(default_factory=FlapDetector)
    manual_reconnects: int = 0  # the admin's, which the fallback rule ignores
    falling_back: bool = False
    halt_code: int | None = None  # code of the last non-retryable error (why the relay halted)
    halt_auth: bool = False  # that error was a refused API key
    auth_reported: bool = False
    latency: LatencyTracker = field(default_factory=LatencyTracker)
    levels: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LEVEL_WINDOW_CHUNKS))
    audio: asyncio.Task | None = None
    consumer: asyncio.Task | None = None
    ticker: asyncio.Task | None = None
    tick: asyncio.Future | None = None  # the tick in progress, if any
    ingest: Any = None
    ingest_restarts: int = 0
    t_next: float = 0.0  # audio clock of the next chunk, continuous across sources
    newest_session: int = 0
    cost_pending: dict[str, list[float]] = field(default_factory=dict)  # component -> [usd, units]
    last_cost_flush: float = 0.0
    rotations: int = 0
    reconnects: int = 0
    last_reconnect_at: float | None = None
    published_state: str | None = None


class RoomWorker:
    def __init__(
        self,
        room: Room,
        settings: Settings,
        bus: CaptionBus,
        db: Database,
        clock: Clock,
        engine_factory: EngineFactory,
        *,
        ingest_factory: IngestFactory = AudioIngest,
        realtime: bool = True,
        tail_s: float = TAIL_S,
        on_talk_end: TalkEndHook | None = None,
        translate: TranslateFn | None = None,
        station_hub: StationHub | None = None,
    ) -> None:
        self.room = room
        self._settings = settings
        self._bus = bus
        self._db = db
        self._clock = clock
        self._engine_factory = engine_factory
        self._ingest_factory = ingest_factory
        self._realtime = realtime
        self._tail_s = tail_s
        # Task 14a: only meaningful for source_type == "emitter" (status()
        # reads the connected station's info into the raw `detail`, Ruling
        # 29 -- never the public API); None for every other source type.
        self._station_hub = station_hub
        cfg = next((r for r in settings.rooms if r.id == room.id), None)
        self.language = cfg.language if cfg is not None else "en"
        try:
            self._tz: timezone | ZoneInfo = ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("unknown timezone %r: free sessions use UTC", settings.timezone)
            self._tz = timezone.utc
        self._free_ids: set[str] = set()
        self._next_seg: dict[str, int] = {}  # per language, for the worker's lifetime

        self.talk: Talk | None = None
        self.audio_s = 0.0  # seconds of audio fed to the pipeline (all sources)
        self._run: _Run | None = None
        self._source_down: str | None = None
        self._cost_usd = 0.0
        self._lock = asyncio.Lock()
        self._aux: set[asyncio.Task] = set()
        self._on_talk_end = on_talk_end
        self._hooks: set[asyncio.Task] = set()
        # task-11r-brief.md item 7: the room's next agenda talk, cached here
        # and refreshed by Autopilot's tick (glosa/scheduler.py _tick_room,
        # which already computes it via Autopilot.next_talk) so view()
        # never blocks the event loop on a DB read of its own.
        self._next_talk: Talk | None = None
        # The lane's translate function; None: a Translator per run (closed
        # with it), or FakeTranslator with engine_mode fake (no API, no key).
        self._translate = translate

    # ------------------------------------------------------------ public API

    @property
    def has_source(self) -> bool:
        return bool(self.room.source_url)

    async def start(self, talk: Talk | None = None) -> None:
        """Caption ``talk`` (the free session if None) from the room's
        configured source. A talk already running is ended first."""
        async with self._lock:
            await self._start_locked(talk, self.room.source_type, self.room.source_url, self._realtime)

    async def stop(self) -> None:
        """End the talk and stop the pipeline. No task is left running."""
        async with self._lock:
            if self._run is not None:
                await self._teardown(self._run)
            if self.talk is not None:
                await self._end_talk()
            self._source_down = None
        await self._wait_aux()

    async def play_file(self, path: str) -> None:
        """"Probar con audio": play ``path`` at real-time speed as the room's
        audio. A running talk keeps its engine session and just changes
        source; otherwise the current talk (or the free session) starts.
        Raises FileNotFoundError at once for a missing file."""
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        async with self._lock:
            run = self._run
            if run is None:
                await self._start_locked(self.talk, "file", path, True)
                return
            if run.audio is not None:
                run.audio.cancel()
                self._report(await asyncio.gather(run.audio, return_exceptions=True), "audio")
            self._source_down = None
            run.source = ("file", path, True)
            run.audio = self._spawn(self._audio_loop(run, "file", path, True), "audio")
            await self._log("info", "source_change", f"playing file {path}")

    async def reconnect(self, reason: str) -> None:
        """The admin's "Reconectar": replace the running engine session now
        (``SessionRelay.reconnect(reason)``; also lifts a halted relay). A
        no-op when no talk is running."""
        run = self._run
        if run is not None:
            run.manual_reconnects += 1
            await run.relay.reconnect(reason)

    async def drain_hooks(self, timeout: float | None = None) -> None:
        """Wait for the ``on_talk_end`` hooks still running; after
        ``timeout`` s, cancel the rest. For shutdown: ``stop()`` itself never
        waits for them."""
        pending = set(self._hooks)
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=timeout)
        for task in late:
            log.error("room %s: talk-end hook still running after %s s: cancelled", self.room.id, timeout)
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def langs(self) -> list[str]:
        """The spoken language, then every translation language."""
        if self.talk is not None:
            return [self.talk.language, *translation_langs(self.talk.language, self.talk.targets)]
        return [self.language, *translation_langs(self.language, self.room.default_targets)]

    def stream_langs(self) -> set[str]:
        """Every language this room may publish captions in: its own and its
        default targets, and the current talk's."""
        langs = {self.language, *self.room.default_targets, *self.langs()}
        if self.talk is not None:
            langs |= {self.talk.language, *self.talk.targets}
        return langs

    def set_next_talk(self, talk: Talk | None) -> None:
        """Autopilot's tick calls this each pass (glosa/scheduler.py
        _tick_room, via Autopilot.next_talk) to refresh view()["next"]
        without view() itself ever touching the DB."""
        self._next_talk = talk

    def view(self) -> dict:
        """The room as the audience pages see it (task-6 contract)."""
        talk = self.talk
        now = None
        if talk is not None:
            now = {
                "talk_id": talk.id,
                "title": talk.title,
                "speakers": list(talk.speakers),
                "language": talk.language,
            }
        nxt = None
        if self._next_talk is not None:
            nxt = {
                "talk_id": self._next_talk.id,
                "title": self._next_talk.title,
                "speakers": list(self._next_talk.speakers),
                "language": self._next_talk.language,
                "start": self._next_talk.start.astimezone(self._tz).strftime("%H:%M"),
            }
        return {"slug": self.room.slug, "name": self.room.name, "langs": self.langs(), "now": now, "next": nxt}

    def status(self) -> RoomStatus:
        run = self._run
        level = run.vad.level_db if run is not None else MIN_LEVEL_DB
        latency = run.latency.p50() if run is not None else None
        talk_id = self.talk.id if self.talk is not None else None
        if self.talk is None:
            state, detail = "idle", "no talk in progress"
        else:
            now = self._clock.now()
            peak = max(run.levels) if run is not None and run.levels else level
            state, detail = RoomHealth.evaluate(
                latency_p50=latency,
                quality_avg=None,
                level_db=peak,
                stall_active=False,  # the relay reconnects a stall at once (T10: flapping)
                source_down=self._source_down is not None,
                payment_blocked=run is not None and run.relay.payment_blocked,
                talk_active=True,
                recent_reconnect=run is not None
                and run.last_reconnect_at is not None
                and now - run.last_reconnect_at < RECENT_RECONNECT_S,
            )
            if self._source_down is not None and state == "red":
                detail = f"source is down: {self._source_down}"
            elif run is not None and run.relay.halted and state != "red":
                if run.halt_auth:
                    state, detail = "red", f"engine halted: the API key was refused ({run.halt_code})"
                else:
                    state, detail = "red", "engine halted: non-retryable error, waiting for a reconnect"
        if self.room.source_type == "emitter" and self._station_hub is not None:
            detail = f"{detail} | {self._station_summary()}"
        return RoomStatus(
            state=state,
            level_db=level,
            latency_p50_s=latency,
            quality=None,
            cost_usd=self._cost_usd,
            talk_id=talk_id,
            detail=detail,
        )

    def latency_p50(self, min_samples: int = 10) -> float | None:
        """The room's CURRENT run's caption latency p50 (seconds), or None
        with no talk running or fewer than min_samples closed samples.
        task-11r-brief.md Ruling 2: the export route's shift_s uses this (a
        room-wide estimate, not one recomputed per historical talk -- a
        finished talk's own run and LatencyTracker are long gone) when
        there's enough signal, else falls back to
        Settings.default_export_shift_s."""
        run = self._run
        if run is None or len(run.latency.samples()) < min_samples:
            return None
        return run.latency.p50()

    def _station_summary(self) -> str:
        """The connected station's info (Task 14a), for status()'s raw
        `detail` (admin-only, Ruling 29): connected or not, device, last
        level in dB, how long since its last audio."""
        assert self._station_hub is not None
        info = self._station_hub.info(self.room.id)
        if not info.connected:
            return "station: not connected"
        device = info.device or "unknown device"
        level = "no level yet" if info.level_db is None else f"{info.level_db:.1f} dBFS"
        age = "no audio yet" if info.last_audio_age_s is None else f"last audio {info.last_audio_age_s:.1f}s ago"
        return f"station: {device}, {level}, {age}"

    def free_talk(self) -> Talk:
        """A new free session (Ruling 27: one id per run)."""
        now = self._clock.wall().astimezone(self._tz)
        base = f"{FREE_SESSION_PREFIX}{self.room.id}-{now:%Y%m%dT%H%M%S}"
        talk_id, n = base, 2
        while talk_id in self._free_ids:  # two runs within one second
            talk_id, n = f"{base}-{n}", n + 1
        self._free_ids.add(talk_id)
        return Talk(
            id=talk_id,
            room_id=self.room.id,
            title=FREE_SESSION_TITLE,
            speakers=[],
            language=self.language,
            targets=list(self.room.default_targets),
            engine=default_engine(self.language, self._settings),
            start=now,
            end=now + timedelta(hours=FREE_SESSION_HOURS),
            abstract="",
            tags=[],
            glossary=[],
            status="live",
            actual_start=now,
            actual_end=None,
        )

    # ------------------------------------------------------------ lifecycle

    async def _start_locked(
        self,
        talk: Talk | None,
        source_type: str,
        source_url: str | None,
        realtime: bool,
    ) -> None:
        """Start ``talk`` (or the free session) on ``source_url``."""
        if not source_url:
            raise ValueError(f"room {self.room.id!r} has no audio source")
        if self._run is not None:
            await self._teardown(self._run)
        if self.talk is not None and (talk is None or talk.id != self.talk.id):
            await self._end_talk()
        talk = talk or self.talk or self.free_talk()
        if talk is self.talk:  # the running talk again: an admin may have edited it since
            await self._reload_agenda_fields(talk)
        wall = self._clock.wall()
        if talk.actual_start is None:
            talk.actual_start = wall
        talk.status = "live"
        await self._db_call(self._db.insert_talks([talk]))
        await self._db_call(self._db.update_talk(talk.id, status="live", actual_start=talk.actual_start))
        self.talk = talk
        self._source_down = None

        kind = engine_of(talk.engine)
        langs = translation_langs(talk.language, talk.targets)
        now = self._clock.now()
        # Segment times count from the talk's actual start, also when the
        # same talk restarts (a source that came back, play_file()).
        elapsed = max((wall - talk.actual_start).total_seconds(), 0.0)
        tracks = {talk.language: _Track(talk.language, "source")}
        tracks |= {lang: _Track(lang, "translation") for lang in langs}
        run = _Run(
            talk=talk,
            target=langs[0],
            source=(source_type, source_url, realtime),
            vad=EnergyVad(self._settings.vad.pause_ms, self._settings.vad.min_speech_s),
            tracks=tracks,
            t0=now - elapsed,
            last_cost_flush=now,
        )
        self._apply_engine(run, await self._build_engine(run, kind))
        self._run = run
        self._publish_all(run.tracks, "talk", data=_talk_data(talk))
        await run.relay.start()
        run.consumer = self._spawn(self._consume(run, run.relay, kind), "events")
        run.ticker = self._spawn(self._tick_loop(run), "ticker")
        run.audio = self._spawn(self._audio_loop(run, source_type, source_url, realtime), "audio")
        direction = f"{talk.language} -> {', '.join(langs)}, {kind} engine"
        log.info("room %s: talk %s started (%s)", self.room.id, talk.id, direction)
        await self._log("info", "talk_start", f"{talk.id}: {talk.title} ({direction})")

    async def _build_engine(self, run: _Run, kind: EngineKind, first_seq: int = 1) -> _EngineSide:
        """The engine side of ``run`` for ``kind``: the relay (sessions
        numbered from ``first_seq``), the translation lane and its
        Translator. ``run`` is not changed (see ``_apply_engine``); nothing
        is started."""
        talk = run.talk
        langs = translation_langs(talk.language, talk.targets)
        if kind == "glossary":  # transcribe-live: the lane translates every target
            terms = vocabulary(term.term for term in talk.glossary)
            if len(terms) > MAX_VOCABULARY:
                message = f"{len(terms)} glossary terms: only the first {MAX_VOCABULARY} go to transcribe-live"
                log.warning("room %s: %s", self.room.id, message)
                await self._log("warning", "vocabulary", f"{talk.id}: {message}")
                terms = terms[:MAX_VOCABULARY]
            cfg = EngineConfig(kind="glossary", source_lang=talk.language, target_lang=None, vocabulary=terms)
            lane_targets = langs
        else:  # Live Translate covers the first target; the lane, the others
            cfg = EngineConfig(kind="fast", source_lang=talk.language, target_lang=langs[0])
            lane_targets = langs[1:]
        side = _EngineSide(
            kind,
            SessionRelay(
                self._engine_factory, cfg, self._clock, first_seq=first_seq, **self._settings.relay.model_dump()
            ),
        )
        if lane_targets:
            translate = self._translate
            if translate is None and self._settings.engine_mode == "fake":
                translate = FakeTranslator().translate
            elif translate is None:
                prices = self._settings.prices
                side.translator = Translator(
                    self._settings.gemini_api_key,
                    price_in_per_m=prices.flash_lite_in_per_m,
                    price_out_per_m=prices.flash_lite_out_per_m,
                    clock=self._clock,
                )
                translate = side.translator.translate
            side.lane = TranslationLane(
                targets=lane_targets,
                translate=translate,
                glossary=lambda: run.talk.glossary,
                clock=self._clock,
                segmenter=self._settings.segmenter,
                on_segment=lambda seg: self._on_translation(run, seg),
            )
        return side

    @staticmethod
    def _apply_engine(run: _Run, side: _EngineSide) -> None:
        """Put ``side`` on ``run``, with the per-engine counters at zero."""
        run.engine, run.relay, run.lane, run.translator = side.kind, side.relay, side.lane, side.translator
        run.text_session = run.newest_session = 0
        run.rotations = run.reconnects = run.manual_reconnects = 0
        run.flaps = FlapDetector()
        run.falling_back = run.auth_reported = run.halt_auth = False
        run.halt_code = None

    async def _swap_engine(self, run: _Run, kind: EngineKind) -> None:
        """Ruling 48, the fallback: ``run`` goes on with ``kind``, hot. The
        audio loop (ingest, VAD) and the talk are left alone. The new side
        is built first; if that fails, the old one stays as it was (the
        caller logs it). The chunks the old relay holds (a halted relay
        keeps the last 2 s) are handed to the new relay, which is in place
        before the old one stops, so no audio is lost; it holds 2 s more
        while it connects. Its sessions are numbered after the old relay's,
        and the old relay's late events only count for the cost
        (``_consume``). Then the old side is retired: relay, its consumer,
        its sessions' open segments (closed and saved), lane (drained),
        Translator. Caller holds the lock."""
        if run.ticker is not None:
            run.ticker.cancel()
            self._report(await asyncio.gather(run.ticker, return_exceptions=True), "ticker")
        if run.tick is not None:
            self._report(await asyncio.gather(run.tick, return_exceptions=True), "tick")
        try:
            old = _EngineSide(run.engine, run.relay, run.lane, run.translator)
            old_consumer = run.consumer
            first_seq = old.relay.last_seq + 1
            side = await self._build_engine(run, kind, first_seq)
            side.relay.preload(old.relay.take_pending())
            self._apply_engine(run, side)
            await run.relay.start()
            run.consumer = self._spawn(self._consume(run, run.relay, kind), "events")
            await self._retire_engine(
                run, old.relay, old_consumer, old.lane, old.translator, sessions_below=first_seq
            )
        finally:
            if self._run is run:
                run.ticker = self._spawn(self._tick_loop(run), "ticker")

    async def _reload_agenda_fields(self, talk: Talk) -> None:
        """Take ``talk``'s agenda fields from the database, so the insert
        that (re)starts it never writes an out-of-date copy back over an
        admin's edit (glosa/web/admin_api.py PUT /api/admin/talks)."""
        stored = await self._db_call(self._db.get_talk(talk.id))
        if stored is None:
            return
        for name in AGENDA_FIELDS:
            setattr(talk, name, getattr(stored, name))

    async def _teardown(self, run: _Run) -> None:
        """Stop the pipeline of ``run``: audio, relay, event consumer, ticker,
        translation lane. Closes every open segment, publishes the pending
        translations (up to DRAIN_S) and records the pending cost."""
        if self._run is run:
            self._run = None
        if run.ticker is not None:
            run.ticker.cancel()
            self._report(await asyncio.gather(run.ticker, return_exceptions=True), "ticker")
        if run.tick is not None:  # let a tick that is writing finish its writes
            self._report(await asyncio.gather(run.tick, return_exceptions=True), "tick")
        if run.audio is not None:
            run.audio.cancel()
            self._report(await asyncio.gather(run.audio, return_exceptions=True), "audio")
        await self._retire_engine(run, run.relay, run.consumer, run.lane, run.translator)

    async def _retire_engine(
        self,
        run: _Run,
        relay: SessionRelay,
        consumer: asyncio.Task | None,
        lane: TranslationLane | None,
        translator: Translator | None,
        *,
        sessions_below: int | None = None,
    ) -> None:
        """Stop one engine side of ``run``: the relay, then its consumer;
        close and save the open segments of its sessions (all of them, or
        those numbered below ``sessions_below`` on a hot swap); drain and
        close the lane; close the Translator; record the pending cost."""
        try:
            await relay.stop()  # its events() ends with `closed`
        except Exception:
            log.exception("room %s: relay stop failed", self.room.id)
        if consumer is not None:
            _, late = await asyncio.wait({consumer}, timeout=CONSUMER_GRACE_S)
            for task in late:
                log.error("room %s: event consumer still busy after %s s", self.room.id, CONSUMER_GRACE_S)
                task.cancel()
            self._report(await asyncio.gather(consumer, return_exceptions=True), "event consumer")
        now = self._clock.now()
        closed: _Closed = []
        for track in run.tracks.values():
            for session in track.sessions():
                if sessions_below is None or session < sessions_below:
                    closed += self._close_session(run, track, session, now)
        await self._save(run, closed)
        if lane is not None:  # after the source closed: its last words get translated too
            try:
                await lane.close()
            except Exception:
                log.exception("room %s: translation lane close failed", self.room.id)
        if translator is not None:  # its HTTP connections
            try:
                await translator.aclose()
            except Exception:
                log.exception("room %s: translator close failed", self.room.id)
        await self._flush_cost(run, self._clock.now())

    async def _end_talk(self) -> None:
        talk, self.talk = self.talk, None
        if talk is None:
            return
        talk.status = "done"
        talk.actual_end = self._clock.wall()
        await self._db_call(self._db.update_talk(talk.id, status="done", actual_end=talk.actual_end))
        langs = [talk.language, *translation_langs(talk.language, talk.targets)]
        ended = {"talk_id": None, "title": None, "speakers": [], "language": None}
        for lang in langs:
            self._bus.publish(self.room.id, lang, "talk", data=ended)
        log.info("room %s: talk %s ended", self.room.id, talk.id)
        await self._log("info", "talk_end", talk.id)
        if self._on_talk_end is not None:
            task = self._spawn(self._run_hook(self._on_talk_end, talk), "talk-end")
            self._hooks.add(task)
            task.add_done_callback(self._hooks.discard)

    async def _run_hook(self, hook: TalkEndHook, talk: Talk) -> None:
        try:
            await hook(talk)
        except Exception:
            log.exception("room %s: talk-end hook failed for %s", self.room.id, talk.id)

    async def _fall_back(self, run: _Run) -> None:
        """Case 10.5 (see the module docstring): the same talk goes on with
        the glossary engine."""
        async with self._lock:
            if self._run is not run:
                return  # stopped or restarted meanwhile
            talk = run.talk
            log.warning(
                "room %s: Live Translate failed (%s): %s goes on with the glossary engine",
                self.room.id,
                "halted" if run.relay.halted else "3 failures in 2 min",
                talk.id,
            )
            try:
                await self._swap_engine(run, "glossary")
            except Exception as exc:
                log.exception("room %s: the fallback to the glossary engine failed", self.room.id)
                await self._log("error", "fallback_failed", repr(exc))
                return
            await self._log("warning", "fallback", "fallback: glossary engine")
            talk.engine = "glossary"
            try:
                await self._db.update_talk(talk.id, engine="glossary")
            except Exception:
                log.warning(
                    "room %s: could not save engine=glossary for %s: after a restart it runs fast again",
                    self.room.id,
                    talk.id,
                    exc_info=True,
                )

    async def _source_ended(self, run: _Run, audio: asyncio.Task | None, error: str | None) -> None:
        async with self._lock:
            if self._run is not run or run.audio is not audio:
                return  # stopped, restarted or given another source meanwhile
            await self._teardown(run)
            if error:
                self._source_down = error
                log.error("room %s: source is down: %s", self.room.id, error)
                await self._log("error", "source_down", error)
            else:
                await self._end_talk()

    # ------------------------------------------------------------ audio

    async def _audio_loop(self, run: _Run, source_type: str, source_url: str, realtime: bool) -> None:
        ingest = self._ingest_factory(source_type, source_url, realtime, self._clock)
        run.ingest = ingest
        run.ingest_restarts = getattr(ingest, "restarts", 0)
        base = run.t_next
        error: str | None = None
        restarts_seen = run.ingest_restarts
        try:
            # aclosing: on cancellation (stop, play_file) the generator is
            # closed right away, which kills ffmpeg, instead of whenever the
            # garbage collector gets to it.
            async with contextlib.aclosing(ingest.chunks()) as chunks:
                async for chunk in chunks:
                    await self._feed(run, AudioChunk(pcm=chunk.pcm, t=round(base + chunk.t, 3)))
                    restarts_seen = getattr(ingest, "restarts", 0)
            # AudioIngest keeps last_error after a restart that recovered: the
            # source died for good only if it kept failing after its last chunk.
            if getattr(ingest, "restarts", 0) > restarts_seen:
                error = getattr(ingest, "last_error", None) or "the source stopped"
            else:  # a clean end: let the engine finish the last phrase
                for _ in range(round(self._tail_s / CHUNK_S)):
                    await self._clock.sleep(CHUNK_S)
                    await self._feed(run, AudioChunk(pcm=SILENCE, t=run.t_next))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("room %s: audio pipeline failed", self.room.id)
            error = f"audio pipeline failed: {exc!r}"
        self._spawn_aux(self._source_ended(run, asyncio.current_task(), error), "source-ended")

    async def _feed(self, run: _Run, chunk: AudioChunk) -> None:
        events = run.vad.process(chunk)
        await run.relay.feed(chunk, voiced=run.vad.in_speech)
        now = self._clock.now()
        for ev in events:
            run.relay.on_vad(ev)
            if ev.kind == "pause":
                run.latency.on_pause(now)
                if run.engine == "glossary":  # hybrid VAD: we end transcribe-live's utterances
                    await run.relay.end_utterance()
                elif run.lane is not None:  # extra languages: the pause closes the utterance
                    run.lane.final(None, now)
        if run.lane is not None:  # every 100 ms: time cuts of the open segment
            run.lane.tick(now)
        run.levels.append(run.vad.level_db)
        run.t_next = round(chunk.t + CHUNK_S, 3)
        self.audio_s = round(self.audio_s + CHUNK_S, 3)

    # ------------------------------------------------------------ engine events

    async def _consume(self, run: _Run, relay: SessionRelay, engine: str) -> None:
        """The events of ``relay``. Once it has been swapped out (the
        fallback), its late events only count for the cost: their text
        would land on the new engine's state."""
        async for ev in relay.events():
            try:
                if relay is run.relay:
                    await self._on_event(run, ev)
                else:
                    self._engine_cost(run, engine, ev)
            except Exception:
                log.exception("room %s: failed to handle %s", self.room.id, ev.kind)

    def _engine_cost(self, run: _Run, engine: str, ev: EngineEvent) -> None:
        usd = float(ev.meta.get("usd") or 0.0)
        if usd:
            prices = self._settings.prices
            price = prices.lt_per_min if engine == "fast" else prices.transcribe_per_min
            self._add_cost(run, COST_ENGINE.get(engine, engine), usd, usd / price if price > 0 else 0.0)

    async def _on_event(self, run: _Run, ev: EngineEvent) -> None:
        self._engine_cost(run, run.engine, ev)
        session = int(ev.meta.get("session") or 0)
        now = self._clock.now()
        if ev.kind in ("source_delta", "source_final"):
            await self._on_source(run, session, ev, now)
        elif ev.kind == "target_delta":
            if not ev.text or run.engine != "fast":
                return  # the glossary engine's translations come from the lane
            track = run.tracks[run.target]
            run.newest_session = max(run.newest_session, session)
            run.latency.on_output(now)
            assembler = track.assemblers.setdefault(session, CaptionAssembler())
            await self._save(run, self._apply(run, track, session, assembler.on_delta(ev.text, now), now))
        elif ev.kind == "error":
            closed: _Closed = []
            for track in run.tracks.values():  # that session is gone
                closed += self._close_session(run, track, session, now)
            if run.lane is not None and session == run.text_session:
                run.lane.final(None, now)  # its open utterance will get no final
            await self._save(run, closed)
            code = int(ev.meta.get("code") or 0)
            payment = bool(ev.meta.get("payment")) or code == 402
            fatal = payment or not ev.meta.get("retryable", True)
            if fatal and not payment:
                run.halt_code = code  # the relay halts on it (payment only blocks)
                reason = ev.text.lower()
                run.halt_auth = code in AUTH_CODES or any(hint in reason for hint in AUTH_HINTS)
            await self._log(
                "error" if fatal else "warning", "engine_error", f"session {session}: error {code}: {ev.text}"
            )
        elif ev.kind == "go_away":
            left = float(ev.meta.get("time_left_s") or 0.0)
            await self._log("info", "go_away", f"session {session}: {left:.0f} s left")

    async def _on_source(self, run: _Run, session: int, ev: EngineEvent, now: float) -> None:
        """Source text. Live Translate appends it (the assembler, one per
        session); transcribe-live's interims and finals replace the open
        segment ("set", ``_set_source``). Only the text of the newest session
        that spoke is used for "set" and for translation (``_text_from``)."""
        final = ev.kind == "source_final"
        replaces = final or bool(ev.meta.get("interim"))
        if not ev.text and not final:
            return
        run.newest_session = max(run.newest_session, session)
        closed: _Closed = []
        current = True
        if replaces or run.lane is not None:
            current = self._text_from(run, session, now, closed)
        if replaces:
            if not current:
                return  # a draining session's late text: the newer session took over
            closed += self._set_source(run, session, ev.text, now, final=final)
            if run.lane is not None:  # after the source is on screen: it is what gets translated
                if final:
                    run.lane.final(ev.text, now)
                else:
                    run.lane.interim(ev.text, now)
        else:
            track = run.tracks[run.talk.language]
            assembler = track.assemblers.setdefault(session, CaptionAssembler())
            closed += self._apply(run, track, session, assembler.on_delta(ev.text, now), now)
            if current and run.lane is not None:
                run.lane.delta(ev.text, now)
        await self._save(run, closed)

    def _text_from(self, run: _Run, session: int, now: float, closed: _Closed) -> bool:
        """Whether ``session``'s source text is the one in use. A newer
        session takes over: the older one's utterance ends where it was (its
        "set" segment closes with the text it shows, the lane cuts what it
        has), and its later text is dropped."""
        if session < run.text_session:
            return False
        if session > run.text_session:
            older = run.text_session
            run.text_session = session
            if older:
                track = run.tracks[run.talk.language]
                seg = track.open.get(older)
                if seg is not None and seg.replaces:
                    closed.append(self._close(run, track, older, now))
                if run.lane is not None:
                    run.lane.final(None, now)
        return True

    def _set_source(self, run: _Run, session: int, text: str, now: float, *, final: bool) -> _Closed:
        """Publish ``text`` as the whole open source segment ("set"; not
        again if it did not change) and close it on a final. An empty final
        with nothing open says nothing."""
        track = run.tracks[run.talk.language]
        closed: _Closed = []
        current = track.open.get(session)
        if current is not None and not current.replaces:  # appended text of this session is open
            closed += self._close_session(run, track, session, now)
            current = None
        if current is None:
            if final and not text:
                return closed
            seg = self._next_seg.get(track.lang, 0)
            self._next_seg[track.lang] = seg + 1
            current = _OpenSeg(seg=seg, local=-1, t_start=now - run.t0, last_at=now, replaces=True)
            track.open[session] = current
        if text != current.text:
            current.text = text
            current.last_at = now
            self._bus.publish(self.room.id, track.lang, "set", seg=current.seg, text=text)
        if final:
            closed.append(self._close(run, track, session, now))
        return closed

    async def _on_translation(self, run: _Run, seg: TranslatedSegment) -> None:
        """The lane's segments, in order per language: one closed caption
        segment each, stored with the times of its source's cut. A failed
        translation (None) is not shown; its cost counts anyway."""
        self._add_cost(run, COST_TRANSLATE, seg.usd, 1.0)
        if not seg.text:
            return
        n = self._next_seg.get(seg.target, 0)
        self._next_seg[seg.target] = n + 1
        self._bus.publish(self.room.id, seg.target, "append", seg=n, text=seg.text)
        self._bus.publish(self.room.id, seg.target, "close", seg=n)
        if run.engine == "glossary" and seg.target == run.target:
            run.latency.on_output(self._clock.now())
        t_start = max(seg.t_start - run.t0, 0.0)
        t_end = max(seg.t_end - run.t0, t_start)
        await self._db_call(
            self._db.save_segment(
                run.talk.id, self.room.id, seg.target, "translation", "live", seg.text, t_start, t_end
            )
        )

    def _apply(self, run: _Run, track: _Track, session: int, ops: list[tuple[str, dict]], now: float) -> _Closed:
        """Publish an assembler's ops, all at once (no await), and return the
        segments they closed, for ``_save``."""
        closed: _Closed = []
        for op, payload in ops:
            current = track.open.get(session)
            if op == "append":
                if current is not None and current.local != payload["seg"]:
                    closed.append(self._close(run, track, session, now))  # moved on without a close
                    current = None
                if current is None:
                    seg = self._next_seg.get(track.lang, 0)
                    self._next_seg[track.lang] = seg + 1
                    current = _OpenSeg(seg=seg, local=payload["seg"], t_start=now - run.t0, last_at=now)
                    track.open[session] = current
                current.text += payload["text"]
                current.last_at = now
                self._bus.publish(self.room.id, track.lang, "append", seg=current.seg, text=payload["text"])
            elif op == "close" and current is not None and current.local == payload["seg"]:
                closed.append(self._close(run, track, session, now))
        return closed

    def _close(self, run: _Run, track: _Track, session: int, now: float) -> tuple[_Track, _OpenSeg]:
        seg = track.open.pop(session)
        seg.t_end = max(now - run.t0, seg.t_start)
        self._bus.publish(self.room.id, track.lang, "close", seg=seg.seg)
        return track, seg

    def _close_session(self, run: _Run, track: _Track, session: int, now: float) -> _Closed:
        """Close the session's open segment in this track and drop its assembler."""
        closed: _Closed = []
        assembler = track.assemblers.pop(session, None)
        if assembler is not None:
            closed += self._apply(run, track, session, assembler.on_pause(now), now)
        if session in track.open:  # an open segment the assembler did not know about
            closed.append(self._close(run, track, session, now))
        return closed

    async def _save(self, run: _Run, closed: _Closed) -> None:
        for track, seg in closed:
            text = seg.text.strip()
            if text:
                await self._db_call(
                    self._db.save_segment(
                        run.talk.id, self.room.id, track.lang, track.kind, "live", text, seg.t_start, seg.t_end
                    )
                )

    # ------------------------------------------------------------ housekeeping

    async def _tick_loop(self, run: _Run) -> None:
        # Cancelled only while sleeping: a tick runs shielded, so its
        # database writes are never cut halfway (teardown waits for it).
        while True:
            await self._clock.sleep(TICK_S)
            run.tick = asyncio.ensure_future(self._safe_tick(run))
            await asyncio.shield(run.tick)

    async def _safe_tick(self, run: _Run) -> None:
        try:
            await self._tick(run, self._clock.now())
        except Exception:
            log.exception("room %s: tick failed", self.room.id)

    async def _tick(self, run: _Run, now: float) -> None:
        closed: _Closed = []
        for track in run.tracks.values():
            for session in track.sessions():
                seg = track.open.get(session)
                # a "set" segment waits for its final (the engine's own pause)
                if seg is not None and not seg.replaces and now - seg.last_at >= IDLE_CLOSE_S - 1e-9:
                    closed += self._close_session(run, track, session, now)
                elif seg is None and session < run.newest_session:
                    track.assemblers.pop(session, None)  # an older session with nothing open
        await self._save(run, closed)
        if run.lane is not None:  # also while no audio flows
            run.lane.tick(now)
        run.latency.tick(now)

        stats = run.relay.stats
        if stats["rotations"] > run.rotations:
            run.rotations = stats["rotations"]
            await self._log("info", "rotation", f"session handover #{run.rotations}")
        if stats["reconnects"] > run.reconnects:
            run.reconnects = stats["reconnects"]
            run.last_reconnect_at = now
            await self._log("warning", "reconnect", f"engine reconnect #{run.reconnects}")
        flapping = run.flaps.update(stats, run.manual_reconnects, now)
        # Ruling 49: halted (a non-retryable error) falls back at once, unless
        # the key was refused (glossary would be too); the halt's code comes
        # with its error event, so a halt waits for that event to be handled.
        if not run.relay.halted:  # a reconnect lifted it (or the error came from a draining session)
            run.halt_code, run.halt_auth, run.auth_reported = None, False, False
        halted = run.relay.halted and run.halt_code is not None
        if run.engine == "fast" and not run.falling_back:
            if halted and run.halt_auth:
                if not run.auth_reported:
                    run.auth_reported = True
                    message = f"Live Translate refused the API key ({run.halt_code}): check GEMINI_API_KEY"
                    log.error("room %s: %s", self.room.id, message)
                    await self._log("error", "engine_auth", message)
            elif flapping or halted:
                run.falling_back = True
                self._spawn_aux(self._fall_back(run), "fallback")
        restarts = getattr(run.ingest, "restarts", 0) if run.ingest is not None else 0
        if restarts > run.ingest_restarts:
            run.ingest_restarts = restarts
            await self._log("warning", "source_restart", f"ffmpeg restart #{restarts}: {run.ingest.last_error}")

        # Task 14a: EmitterIngest.chunks() never ends on a quiet station (so
        # a reconnect resumes the same talk with no operator action), so it
        # can't report a dead source the way AudioIngest does (chunks()
        # ending). It exposes `stale()` instead (not part of the Ingest
        # protocol AudioIngest satisfies -- checked with getattr), polled
        # here to set/clear the same `_source_down` a dying AudioIngest sets.
        stale = getattr(run.ingest, "stale", None) if run.ingest is not None else None
        if stale is not None:
            reason = stale()
            if reason != self._source_down:
                self._source_down = reason
                if reason:
                    await self._log("error", "source_down", reason)
                else:
                    await self._log("info", "source_recovered", "station reconnected")

        if now - run.last_cost_flush >= COST_FLUSH_S:
            await self._flush_cost(run, now)

        state = self.status().state
        if state != run.published_state:
            run.published_state = state
            self._publish_all(run.tracks, "status", data={"state": state})

    def _add_cost(self, run: _Run, component: str, usd: float, units: float) -> None:
        if usd <= 0:
            return
        self._cost_usd += usd
        pending = run.cost_pending.setdefault(component, [0.0, 0.0])
        pending[0] += usd
        pending[1] += units

    async def _flush_cost(self, run: _Run, now: float) -> None:
        """One costs row per component with spend since the last flush."""
        pending, run.cost_pending = run.cost_pending, {}
        run.last_cost_flush = now
        for component, (usd, units) in pending.items():
            await self._db_call(self._db.add_cost(self.room.id, component, units, usd))

    # ------------------------------------------------------------ helpers

    def _publish_all(self, tracks: dict[str, _Track], type: str, **payload: Any) -> None:
        for lang in tracks:
            self._bus.publish(self.room.id, lang, type, **payload)

    def _report(self, results: list[Any], what: str) -> None:
        """Log what a gather(return_exceptions=True) caught, but a cancellation."""
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                log.error("room %s: %s failed", self.room.id, what, exc_info=result)

    async def _log(self, level: str, type: str, message: str) -> None:
        await self._db_call(self._db.log_event(self.room.id, level, type, message))

    async def _db_call(self, call: Awaitable[Any]) -> Any:
        """A failing database must not stop the captions."""
        try:
            return await call
        except Exception:
            log.exception("room %s: database write failed", self.room.id)
            return None

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(coro, name=f"room-{self.room.id}-{name}")

    def _spawn_aux(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """A task that takes the lock on its own (stop() waits for it)."""
        task = self._spawn(coro, name)
        self._aux.add(task)
        task.add_done_callback(self._aux.discard)

    async def _wait_aux(self) -> None:
        current = asyncio.current_task()
        pending = [t for t in self._aux if t is not current]
        if pending:
            self._report(await asyncio.gather(*pending, return_exceptions=True), "source-ended handler")


def _talk_data(talk: Talk) -> dict:
    return {"talk_id": talk.id, "title": talk.title, "speakers": list(talk.speakers), "language": talk.language}
