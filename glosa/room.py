"""RoomWorker: the live pipeline of one room.

::

    AudioIngest -> EnergyVad -> SessionRelay (engine "fast" in P0)
      -> CaptionAssembler per (engine session, language)
      -> CaptionBus.publish -> db.save_segment when a segment closes

Per 100 ms chunk (Ruling 23): ``vad.process(chunk)``, then
``relay.feed(chunk, voiced=vad.in_speech)`` (the watchdog needs ``voiced``),
then ``relay.on_vad(ev)`` for each VAD event. A separate task consumes
``relay.events()``: ``source_delta`` goes to the talk's language,
``target_delta`` to the translation language (routed by kind, not by
``ev.lang``), and every ``meta["usd"]`` increment is added to the room's cost.

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

from glosa.audio.ingest import CHUNK_BYTES, CHUNK_S, AudioIngest
from glosa.audio.vad import EnergyVad
from glosa.captions.assembler import CaptionAssembler
from glosa.captions.bus import CaptionBus
from glosa.clock import Clock
from glosa.config import Settings
from glosa.db import Database
from glosa.engines.base import EngineFactory
from glosa.engines.relay import SessionRelay
from glosa.metrics import LatencyTracker, RoomHealth
from glosa.models import AudioChunk, EngineConfig, EngineEvent, Room, RoomStatus, Talk

log = logging.getLogger(__name__)

TICK_S = 0.5  # housekeeping period: idle close, status, events, cost
IDLE_CLOSE_S = 2.5  # see the module docstring
TAIL_S = 5.0  # silence fed after a source ends, for the engine's last output
COST_FLUSH_S = 10.0  # usd increments are batched into one costs row this often
LEVEL_WINDOW_CHUNKS = 50  # health uses the loudest chunk of the last 5 s
RECENT_RECONNECT_S = 60.0
CONSUMER_GRACE_S = 10.0
FREE_SESSION_TITLE = "Sesión libre"
FREE_SESSION_HOURS = 12
MIN_LEVEL_DB = -96.0
SILENCE = bytes(CHUNK_BYTES)
COST_COMPONENT = "live_translate"


class Ingest(Protocol):
    restarts: int
    last_error: str | None

    def chunks(self) -> Any: ...


IngestFactory = Callable[[str, str, bool, Clock], Ingest]


def target_lang(language: str, targets: list[str]) -> str:
    """The translation language: the first target that is not the spoken
    language (Live Translate takes one target per session). With none,
    Spanish, or English for a Spanish talk."""
    for code in targets:
        if code != language:
            return code
    return "en" if language == "es" else "es"


@dataclass
class _OpenSeg:
    seg: int  # published id
    local: int  # the assembler's own seg number
    t_start: float
    last_at: float
    text: str = ""
    t_end: float | None = None  # set when it closes


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
class _Run:
    """The pipeline of the talk being captioned."""

    talk: Talk
    target: str
    relay: SessionRelay
    vad: EnergyVad
    tracks: dict[str, _Track]
    t0: float
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
    cost_pending: float = 0.0
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
            run.audio = self._spawn(self._audio_loop(run, "file", path, True), "audio")
            await self._log("info", "source_change", f"playing file {path}")

    def langs(self) -> list[str]:
        if self.talk is not None:
            return [self.talk.language, target_lang(self.talk.language, self.talk.targets)]
        return [self.language, target_lang(self.language, self.room.default_targets)]

    def stream_langs(self) -> set[str]:
        """Every language this room may publish captions in: its own and its
        default targets, and the current talk's."""
        langs = {self.language, *self.room.default_targets, *self.langs()}
        if self.talk is not None:
            langs |= {self.talk.language, *self.talk.targets}
        return langs

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
        return {"slug": self.room.slug, "name": self.room.name, "langs": self.langs(), "now": now, "next": None}

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
                state, detail = "red", "engine halted: non-retryable error, waiting for a reconnect"
        return RoomStatus(
            state=state,
            level_db=level,
            latency_p50_s=latency,
            quality=None,
            cost_usd=self._cost_usd,
            talk_id=talk_id,
            detail=detail,
        )

    def free_talk(self) -> Talk:
        """A new free session (Ruling 27: one id per run)."""
        now = self._clock.wall().astimezone(self._tz)
        base = f"free-{self.room.id}-{now:%Y%m%dT%H%M%S}"
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
            engine="fast",
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
        self, talk: Talk | None, source_type: str, source_url: str | None, realtime: bool
    ) -> None:
        if not source_url:
            raise ValueError(f"room {self.room.id!r} has no audio source")
        if self._run is not None:
            await self._teardown(self._run)
        if self.talk is not None and (talk is None or talk.id != self.talk.id):
            await self._end_talk()
        talk = talk or self.talk or self.free_talk()
        wall = self._clock.wall()
        if talk.actual_start is None:
            talk.actual_start = wall
        talk.status = "live"
        await self._db_call(self._db.insert_talks([talk]))
        await self._db_call(self._db.update_talk(talk.id, status="live", actual_start=talk.actual_start))
        self.talk = talk
        self._source_down = None

        target = target_lang(talk.language, talk.targets)
        cfg = EngineConfig(kind="fast", source_lang=talk.language, target_lang=target)
        relay = SessionRelay(self._engine_factory, cfg, self._clock, **self._settings.relay.model_dump())
        now = self._clock.now()
        # Segment times count from the talk's actual start, also when the
        # same talk restarts (a source that came back, play_file()).
        elapsed = max((wall - talk.actual_start).total_seconds(), 0.0)
        run = _Run(
            talk=talk,
            target=target,
            relay=relay,
            vad=EnergyVad(self._settings.vad.pause_ms, self._settings.vad.min_speech_s),
            tracks={talk.language: _Track(talk.language, "source"), target: _Track(target, "translation")},
            t0=now - elapsed,
            last_cost_flush=now,
        )
        self._run = run
        self._publish_all(run.tracks, "talk", data=_talk_data(talk))
        await relay.start()
        run.consumer = self._spawn(self._consume(run), "events")
        run.ticker = self._spawn(self._tick_loop(run), "ticker")
        run.audio = self._spawn(self._audio_loop(run, source_type, source_url, realtime), "audio")
        log.info("room %s: talk %s started (%s -> %s)", self.room.id, talk.id, talk.language, target)
        await self._log("info", "talk_start", f"{talk.id}: {talk.title} ({talk.language} -> {target})")

    async def _teardown(self, run: _Run) -> None:
        """Stop the pipeline of ``run``: audio, relay, event consumer, ticker.
        Closes every open segment and records the pending cost."""
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
        try:
            await run.relay.stop()  # its events() ends with `closed`
        except Exception:
            log.exception("room %s: relay stop failed", self.room.id)
        if run.consumer is not None:
            _, late = await asyncio.wait({run.consumer}, timeout=CONSUMER_GRACE_S)
            for task in late:
                log.error("room %s: event consumer still busy after %s s", self.room.id, CONSUMER_GRACE_S)
                task.cancel()
            self._report(await asyncio.gather(run.consumer, return_exceptions=True), "event consumer")
        now = self._clock.now()
        closed: _Closed = []
        for track in run.tracks.values():
            for session in track.sessions():
                closed += self._close_session(run, track, session, now)
        await self._save(run, closed)
        await self._flush_cost(run, now)

    async def _end_talk(self) -> None:
        talk, self.talk = self.talk, None
        if talk is None:
            return
        talk.status = "done"
        talk.actual_end = self._clock.wall()
        await self._db_call(self._db.update_talk(talk.id, status="done", actual_end=talk.actual_end))
        langs = [talk.language, target_lang(talk.language, talk.targets)]
        ended = {"talk_id": None, "title": None, "speakers": [], "language": None}
        for lang in langs:
            self._bus.publish(self.room.id, lang, "talk", data=ended)
        log.info("room %s: talk %s ended", self.room.id, talk.id)
        await self._log("info", "talk_end", talk.id)

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
        self._spawn_aux(self._source_ended(run, asyncio.current_task(), error))

    async def _feed(self, run: _Run, chunk: AudioChunk) -> None:
        events = run.vad.process(chunk)
        await run.relay.feed(chunk, voiced=run.vad.in_speech)
        for ev in events:
            run.relay.on_vad(ev)
            if ev.kind == "pause":
                run.latency.on_pause(self._clock.now())
        run.levels.append(run.vad.level_db)
        run.t_next = round(chunk.t + CHUNK_S, 3)
        self.audio_s = round(self.audio_s + CHUNK_S, 3)

    # ------------------------------------------------------------ engine events

    async def _consume(self, run: _Run) -> None:
        async for ev in run.relay.events():
            try:
                await self._on_event(run, ev)
            except Exception:
                log.exception("room %s: failed to handle %s", self.room.id, ev.kind)

    async def _on_event(self, run: _Run, ev: EngineEvent) -> None:
        usd = float(ev.meta.get("usd") or 0.0)
        if usd:
            self._cost_usd += usd
            run.cost_pending += usd
        session = int(ev.meta.get("session") or 0)
        now = self._clock.now()
        if ev.kind in ("source_delta", "target_delta"):
            if not ev.text:
                return
            lang = run.talk.language if ev.kind == "source_delta" else run.target
            track = run.tracks[lang]
            run.newest_session = max(run.newest_session, session)
            if ev.kind == "target_delta":
                run.latency.on_output(now)
            assembler = track.assemblers.setdefault(session, CaptionAssembler())
            await self._save(run, self._apply(run, track, session, assembler.on_delta(ev.text, now), now))
        elif ev.kind == "error":
            closed: _Closed = []
            for track in run.tracks.values():  # that session is gone
                closed += self._close_session(run, track, session, now)
            await self._save(run, closed)
            code = int(ev.meta.get("code") or 0)
            fatal = bool(ev.meta.get("payment")) or code == 402 or not ev.meta.get("retryable", True)
            await self._log(
                "error" if fatal else "warning", "engine_error", f"session {session}: error {code}: {ev.text}"
            )
        elif ev.kind == "go_away":
            left = float(ev.meta.get("time_left_s") or 0.0)
            await self._log("info", "go_away", f"session {session}: {left:.0f} s left")

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
                if seg is not None and now - seg.last_at >= IDLE_CLOSE_S - 1e-9:
                    closed += self._close_session(run, track, session, now)
                elif seg is None and session < run.newest_session:
                    track.assemblers.pop(session, None)  # an older session with nothing open
        await self._save(run, closed)
        run.latency.tick(now)

        stats = run.relay.stats
        if stats["rotations"] > run.rotations:
            run.rotations = stats["rotations"]
            await self._log("info", "rotation", f"session handover #{run.rotations}")
        if stats["reconnects"] > run.reconnects:
            run.reconnects = stats["reconnects"]
            run.last_reconnect_at = now
            await self._log("warning", "reconnect", f"engine reconnect #{run.reconnects}")
        restarts = getattr(run.ingest, "restarts", 0) if run.ingest is not None else 0
        if restarts > run.ingest_restarts:
            run.ingest_restarts = restarts
            await self._log("warning", "source_restart", f"ffmpeg restart #{restarts}: {run.ingest.last_error}")

        if now - run.last_cost_flush >= COST_FLUSH_S:
            await self._flush_cost(run, now)

        state = self.status().state
        if state != run.published_state:
            run.published_state = state
            self._publish_all(run.tracks, "status", data={"state": state})

    async def _flush_cost(self, run: _Run, now: float) -> None:
        usd, run.cost_pending = run.cost_pending, 0.0
        run.last_cost_flush = now
        if usd <= 0:
            return
        price = self._settings.prices.lt_per_min
        minutes = usd / price if price > 0 else 0.0
        await self._db_call(self._db.add_cost(self.room.id, COST_COMPONENT, minutes, usd))

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

    def _spawn_aux(self, coro: Coroutine[Any, Any, None]) -> None:
        task = self._spawn(coro, "source-ended")
        self._aux.add(task)
        task.add_done_callback(self._aux.discard)

    async def _wait_aux(self) -> None:
        current = asyncio.current_task()
        pending = [t for t in self._aux if t is not current]
        if pending:
            self._report(await asyncio.gather(*pending, return_exceptions=True), "source-ended handler")


def _talk_data(talk: Talk) -> dict:
    return {"talk_id": talk.id, "title": talk.title, "speakers": list(talk.speakers), "language": talk.language}
