"""RoomWorker: one room's pipeline, AudioIngest -> EnergyVad -> SessionRelay ->
CaptionAssembler (one per engine session and language) -> CaptionBus -> SQLite.

Everything runs on simulated time. DrivenClock is a FakeClock whose sleep()
waits for the test to advance time (like the one in tests/engines/
test_relay.py): the audio source, the replaying engines and the worker's
ticker then share one coherent timeline. FakeIngest stands in for ffmpeg with
100 ms chunks of a square wave (voice) or zeros (silence), paced by that clock;
one test feeds the real samples/en_clip.opus through the real AudioIngest.
FakeTranslate stands in for the Translator of the glossary engine and of the
extra languages (no API is ever called).
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
import os
import struct
import subprocess
import sys
import types
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from glosa.audio.ingest import STATION_TIMEOUT_S, AudioIngest, EmitterIngest, StationHub
from glosa.captions.bus import CaptionBus
from glosa.clock import FakeClock, RealClock
from glosa.config import RelayCfg, RoomCfg, Settings
from glosa.db import init_db
from glosa.engines.fake import FakeEngine
from glosa.models import AudioChunk, CaptionMsg, EngineConfig, GlossaryTerm, Room, Talk
from glosa.room import RoomWorker
from glosa.text.translator import Translation

ROOT = Path(__file__).resolve().parents[1]
FAKE_LT = ROOT / "tests" / "fixtures" / "fake_lt.jsonl"
TR_ES = ROOT / "samples" / "fixtures" / "tr_es.jsonl"
EN_CLIP = ROOT / "samples" / "en_clip.opus"

TONE = struct.pack("<1600h", *([8000, -8000] * 800))  # 100 ms, about -12 dBFS
SILENCE = bytes(3200)


# --------------------------------------------------------------------- harness


class DrivenClock(FakeClock):
    """FakeClock whose sleep() suspends until the test advances time."""

    def __init__(self) -> None:
        super().__init__()
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def advance(self, s: float) -> None:
        super().advance(s)
        now = self.now()
        due = [f for t, f in self._sleepers if t <= now + 1e-9]
        self._sleepers = [(t, f) for t, f in self._sleepers if t > now + 1e-9]
        for fut in due:
            if not fut.done():
                fut.set_result(None)

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now() + s, fut))
        await fut


class _SleepUntilClosed:
    """Clock proxy for one fake engine: sleep() also returns once the engine
    is closed (FakeEngine.close() does not interrupt a pending sleep, so a
    replay waiting for its next record would hold stop() for the relay's
    real-time grace)."""

    def __init__(self, clock: DrivenClock, closing: asyncio.Event) -> None:
        self._clock = clock
        self._closing = closing

    def now(self) -> float:
        return self._clock.now()

    def wall(self):
        return self._clock.wall()

    async def sleep(self, s: float) -> None:
        if self._closing.is_set():
            await asyncio.sleep(0)
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(s))
        closer = asyncio.ensure_future(self._closing.wait())
        _, pending = await asyncio.wait({sleeper, closer}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class QuickFakeEngine(FakeEngine):
    def __init__(
        self, cfg: EngineConfig, clock: DrivenClock, usd: float = 0.0, fail_after_s: float | None = None
    ) -> None:
        self._closing = asyncio.Event()
        self._usd = usd
        self.end_utterances = 0
        self.sent: list[float] = []  # audio clock of each chunk it got
        super().__init__(cfg, _SleepUntilClosed(clock, self._closing), fail_after_s=fail_after_s)

    @property
    def closed(self) -> bool:
        return self._closing.is_set()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk.t)
        await super().send_audio(chunk)

    async def end_utterance(self) -> None:
        self.end_utterances += 1
        await super().end_utterance()

    async def close(self) -> None:
        self._closing.set()
        await super().close()

    async def events(self):  # type: ignore[override]
        async for ev in super().events():
            if self._usd:
                ev.meta["usd"] = self._usd
            yield ev


class Factory:
    """EngineFactory: the n-th session replays fixtures[n] (the last one repeats)."""

    def __init__(
        self, clock: DrivenClock, *fixtures: Path, usd: float = 0.0, fail_after_s: float | None = None
    ) -> None:
        self.clock = clock
        self.fixtures = fixtures
        self.usd = usd
        self.fail_after_s = fail_after_s
        self.configs: list[EngineConfig] = []
        self.engines: list[QuickFakeEngine] = []

    def __call__(self, cfg: EngineConfig) -> QuickFakeEngine:
        self.configs.append(cfg)
        fixture = self.fixtures[min(len(self.configs), len(self.fixtures)) - 1]
        engine = QuickFakeEngine(replace(cfg, fixture_path=str(fixture)), self.clock, self.usd, self.fail_after_s)
        self.engines.append(engine)
        return engine


class RecordingTranslator:
    """Stands in for glosa.room.Translator (monkeypatched): records every
    instance a room makes and whether it was closed."""

    made: list[RecordingTranslator] = []

    def __init__(self, api_key: str, **kwargs) -> None:
        self.api_key = api_key
        self.closed = 0
        type(self).made.append(self)

    async def translate(self, segment, target, glossary, context) -> Translation:
        await asyncio.sleep(0)
        return Translation(text=f"[{target}] {segment}", latency_s=0.0, usd=0.0)

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def translators(monkeypatch: pytest.MonkeyPatch) -> list[RecordingTranslator]:
    """The Translators rooms make (no FakeTranslate injected), in order."""
    made: list[RecordingTranslator] = []
    monkeypatch.setattr(RecordingTranslator, "made", made)
    monkeypatch.setattr("glosa.room.Translator", RecordingTranslator)
    return made


class RecordingLocalTranslator:
    """Stands in for glosa.room.LocalTranslator (Task 16, monkeypatched):
    records every instance a room makes (and the source_lang it was built
    with) and whether it was closed. glosa/engines/local.py has its own
    unit tests for LocalParakeetEngine itself; this is only about
    RoomWorker's wiring (kind selection + which translator the lane gets)."""

    made: list["RecordingLocalTranslator"] = []

    def __init__(self, *, source_lang: str, **kwargs) -> None:
        self.source_lang = source_lang
        self.closed = 0
        type(self).made.append(self)

    async def translate(self, segment, target, glossary, context) -> Translation:
        await asyncio.sleep(0)
        return Translation(text=f"[{target}] {segment}", latency_s=0.0, usd=0.0)

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def local_translators(monkeypatch: pytest.MonkeyPatch) -> list[RecordingLocalTranslator]:
    made: list[RecordingLocalTranslator] = []
    monkeypatch.setattr(RecordingLocalTranslator, "made", made)
    monkeypatch.setattr("glosa.room.LocalTranslator", RecordingLocalTranslator)
    return made


class FakeTranslate:
    """Translator.translate stand-in: "[<target>] <segment>" at once, or ""
    for the segments in `empty`."""

    def __init__(self, usd: float = 0.0, empty: tuple[str, ...] = ()) -> None:
        self.usd = usd
        self.empty = empty
        self.calls: list[tuple[str, str, list[GlossaryTerm], list[str]]] = []

    async def __call__(
        self, segment: str, target: str, glossary: list[GlossaryTerm], context: list[str]
    ) -> Translation:
        self.calls.append((segment, target, list(glossary), list(context)))
        await asyncio.sleep(0)
        text = "" if segment in self.empty else f"[{target}] {segment}"
        return Translation(text=text, latency_s=0.0, usd=self.usd)


class FakeQualityMeter:
    """glosa.room_quality.MeterLike stand-in for RoomWorker-level tests: the
    QualityFeed/QualityMeter internals (pairing, rate limits, en<->es
    mapping) are unit-tested against a fake in tests/test_room_quality.py;
    here we only check that RoomWorker wires pairing, per-talk reset and
    status().quality through to a meter built by quality_factory."""

    def __init__(self, result: float | None = 0.9) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []
        self.scores: list[float] = []
        self.reset_calls = 0
        self.closed = 0

    async def score(self, src: str, tgt: str) -> float | None:
        self.calls.append((src, tgt))
        await asyncio.sleep(0)
        # The real QualityMeter's closed HTTP client raises, which it
        # swallows into None (final-review-A I1).
        return None if self.closed else self.result

    def add(self, p: float | None) -> None:
        if p is not None:
            self.scores.append(p)

    def avg(self) -> float | None:
        return sum(self.scores) / len(self.scores) if self.scores else None

    def reset(self) -> None:
        self.reset_calls += 1
        self.scores.clear()

    async def aclose(self) -> None:
        self.closed += 1


class FakeIngest:
    """AudioIngest stand-in: 2 s of voice, 0.6 s of silence, repeating."""

    def __init__(self, source_type, source_url, realtime, clock, seconds=None, error=None, blip_at=None) -> None:
        self.args = (source_type, source_url, realtime)
        self.clock = clock
        self.seconds = seconds
        self.error = error
        self.blip_at = blip_at  # ffmpeg died once here and came back
        self.restarts = 0
        self.last_error: str | None = None
        self.yielded = 0
        self.closed = False  # the chunks() generator was finalized

    async def chunks(self):
        n_max = None if self.seconds is None else round(self.seconds / 0.1)
        n = 0
        try:
            while n_max is None or n < n_max:
                await self.clock.sleep(0.1)
                t = round(n * 0.1, 2)
                if self.blip_at is not None and t == self.blip_at:
                    self.restarts, self.last_error = 1, "ffmpeg: connection reset"
                voiced = (t % 2.6) < 2.0 - 1e-9
                yield AudioChunk(pcm=TONE if voiced else SILENCE, t=t)
                self.yielded += 1
                n += 1
            if self.error:  # AudioIngest gives up after 5 failed restarts
                self.restarts = 5
                self.last_error = self.error
        finally:
            self.closed = True


class IngestFactory:
    def __init__(self, **opts) -> None:
        self.opts = opts
        self.made: list[FakeIngest] = []

    def __call__(self, source_type, source_url, realtime, clock) -> FakeIngest:
        ingest = FakeIngest(source_type, source_url, realtime, clock, **self.opts)
        self.made.append(ingest)
        return ingest


class SilenceThenVoiceIngest:
    """Voice for ``lead_s``, then continuous silence for ``silence_s``, then
    voice again (indefinitely, until stop()) -- for the silence-gate tests
    (task-19): FakeIngest's own 2.0 s voice / 0.6 s silence pattern never
    reaches a realistic gate threshold."""

    def __init__(self, source_type, source_url, realtime, clock, lead_s: float, silence_s: float) -> None:
        self.args = (source_type, source_url, realtime)
        self.clock = clock
        self.lead_s = lead_s
        self.silence_s = silence_s
        self.restarts = 0
        self.last_error: str | None = None

    async def chunks(self):
        n = 0
        for _ in range(round(self.lead_s / 0.1)):
            await self.clock.sleep(0.1)
            yield AudioChunk(pcm=TONE, t=round(n * 0.1, 2))
            n += 1
        for _ in range(round(self.silence_s / 0.1)):
            await self.clock.sleep(0.1)
            yield AudioChunk(pcm=SILENCE, t=round(n * 0.1, 2))
            n += 1
        while True:
            await self.clock.sleep(0.1)
            yield AudioChunk(pcm=TONE, t=round(n * 0.1, 2))
            n += 1


class SilenceIngestFactory:
    def __init__(self, lead_s: float, silence_s: float) -> None:
        self.lead_s = lead_s
        self.silence_s = silence_s
        self.made: list[SilenceThenVoiceIngest] = []

    def __call__(self, source_type, source_url, realtime, clock) -> SilenceThenVoiceIngest:
        ingest = SilenceThenVoiceIngest(source_type, source_url, realtime, clock, self.lead_s, self.silence_s)
        self.made.append(ingest)
        return ingest


def _steady_fixture(tmp_path: Path) -> Path:
    """A FakeEngine recording that answers once promptly and then keeps the
    session open (its last record is far in the future), so long-silence /
    silence-gate tests aren't contaminated by the fixture running out and
    FakeEngine self-closing mid-test (that would count as an unplanned
    reconnect, unrelated to gating)."""
    return _script(
        tmp_path / "steady.jsonl",
        [(0.3, "source_final", "hello"), (0.4, "target_delta", "hola"), (500.0, "source_final", "still here")],
    )


async def settle(max_rounds: int = 60) -> None:
    """Run the loop until nothing is ready, letting SQLite writes (worker
    threads) land, without moving simulated time."""
    loop = asyncio.get_running_loop()
    ready = getattr(loop, "_ready", None)
    for _ in range(3):
        for _ in range(max_rounds):
            await asyncio.sleep(0)
            if ready is not None and not ready:
                break
        await asyncio.sleep(0.002)


async def run_for(clock: DrivenClock, seconds: float, step: float = 0.1) -> None:
    for _ in range(round(seconds / step)):
        clock.advance(step)
        await settle(max_rounds=60)


def _room(room_id: str = "r1", targets: list[str] | None = None) -> Room:
    return Room(
        id=room_id,
        slug=room_id,
        name=f"Sala {room_id}",
        source_type="file",
        source_url=f"fake://{room_id}",
        mode="auto",
        public_token=f"tok-{room_id}",
        default_targets=["es"] if targets is None else targets,
    )


def _settings(
    *rooms: tuple[str, str], relay: RelayCfg | None = None, timezone: str = "UTC", **extra: object
) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        admin_password="test-password",
        timezone=timezone,
        rooms=[
            RoomCfg(id=rid, name=f"Sala {rid}", source_type="file", source_url=f"fake://{rid}", language=lang)
            for rid, lang in (rooms or (("r1", "en"),))
        ],
        relay=relay or RelayCfg(),
        **extra,
    )


def _script(path: Path, records: list[tuple[float, str, str]]) -> Path:
    """Write a FakeEngine recording: (t, kind, text) per record."""
    with path.open("w", encoding="utf-8") as f:
        for t, kind, text in records:
            f.write(json.dumps({"t": t, "kind": kind, "text": text}) + "\n")
    return path


def _transcript(path: Path, records: list[tuple[float, str, str]]) -> Path:
    """A transcribe-live recording: (t, "interim" | "final", text) per record."""
    with path.open("w", encoding="utf-8") as f:
        for t, kind, text in records:
            if kind == "interim":
                rec = {"t": t, "kind": "source_delta", "text": text, "meta": {"interim": True}}
            else:
                rec = {"t": t, "kind": "source_final", "text": text}
            f.write(json.dumps(rec) + "\n")
    return path


def _talk(
    talk_id: str,
    *,
    language: str = "es",
    targets: tuple[str, ...] = ("en",),
    engine: str = "glossary",
    glossary: tuple[GlossaryTerm, ...] = (),
    room_id: str = "r1",
) -> Talk:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Talk(
        id=talk_id, room_id=room_id, title=f"Talk {talk_id}", speakers=[], language=language,
        targets=list(targets), engine=engine, start=start, end=start + timedelta(hours=1),  # type: ignore[arg-type]
        abstract="", tags=[], glossary=list(glossary), status="scheduled", actual_start=None, actual_end=None,
    )


def _clip(tmp_path: Path) -> str:
    """A file play_file() accepts (FakeIngest never reads it)."""
    path = tmp_path / "clip.opus"
    path.write_bytes(b"")
    return str(path)


def _free_id(room_id: str) -> str:
    """The free session of a worker started at t=0: FakeClock's wall clock
    starts at 2026-01-01 00:00:00 UTC (Settings.timezone defaults to UTC)."""
    return f"free-{room_id}-20260101T000000"


FREE_R1 = _free_id("r1")


def _history(bus: CaptionBus, room_id: str, lang: str) -> list[CaptionMsg]:
    return list(bus.history(room_id, lang, _free_id(room_id)))


def _all(bus: CaptionBus, room_id: str, lang: str) -> list[CaptionMsg]:
    """Every buffered message of the track: the free session's and the ones
    published after it ended (tagged with no talk)."""
    msgs = bus.history(room_id, lang, _free_id(room_id)) + bus.history(room_id, lang, None)
    return sorted(msgs, key=lambda m: m.id)


def _segments(msgs: list[CaptionMsg]) -> dict[int, str]:
    """Global seg id -> the text appended to it."""
    out: dict[int, str] = {}
    for m in msgs:
        if m.type == "append":
            out[m.seg] = out.get(m.seg, "") + (m.text or "")
    return out


def _live_tasks() -> list[str]:
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and t.get_name().startswith(("room-", "relay-", "glosa-pipeline"))
    ]


@pytest.fixture
def db(tmp_path: Path):
    database = init_db(tmp_path / "glosa.db")
    yield database
    database.close()


@pytest.fixture
def fake_typesafe_sdk():
    """A minimal stub of typesafe_sdk (Task 13w's optional "jev" extra,
    deliberately not installed in this dev environment), so glosa.quality --
    which imports it at module level -- can actually be imported here. Only
    needed by tests that hand RoomWorker a quality_factory of their own
    (bypassing glosa.room_quality.build_quality_meter's own lazy import):
    QualityFeed.on_target still reaches for glosa.quality.find_matching_source
    lazily, and that needs the real module to import cleanly. No network:
    QualityMeter itself is never used here, always a FakeQualityMeter."""
    stub = types.ModuleType("typesafe_sdk")

    class AsyncTypeSafeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class Noul:
        def __init__(self, *a, **kw) -> None:
            pass

    stub.AsyncTypeSafeClient = AsyncTypeSafeClient
    stub.Noul = Noul
    sys.modules["typesafe_sdk"] = stub
    try:
        yield
    finally:
        sys.modules.pop("typesafe_sdk", None)
        sys.modules.pop("glosa.quality", None)


def _worker(room, settings, bus, db, clock, factory, ingests, **kw) -> RoomWorker:
    kw.setdefault("translate", FakeTranslate())  # never the real Translator
    return RoomWorker(room, settings, bus, db, clock, factory, ingest_factory=ingests, realtime=False, **kw)


# ---------------------------------------------------------------------- tests


async def test_fake_engine_captions_flow_to_the_bus_and_the_db_in_10_s(db) -> None:  # 5.2
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests)

    await worker.start(None)
    await run_for(clock, 10.0)

    en, es = _history(bus, "r1", "en"), _history(bus, "r1", "es")
    assert en[0].type == "talk" and en[0].data["talk_id"] == FREE_R1
    assert es[0].type == "talk" and es[0].data["language"] == "en"
    for msgs in (en, es):
        kinds = {m.type for m in msgs}
        assert {"append", "close"} <= kinds, [m.type for m in msgs]
    assert _segments(en)[0] == "Great starting scenario, for sure."
    assert _segments(es)[0] == "Un gran escenario de inicio, sin duda."

    saved_en = await db.get_segments(FREE_R1, "en", "live")
    saved_es = await db.get_segments(FREE_R1, "es", "live")
    assert saved_en[0].text == "Great starting scenario, for sure."
    assert saved_en[0].kind == "source" and saved_en[0].room_id == "r1"
    assert 4.4 <= saved_en[0].t_start <= 4.8 and 5.1 <= saved_en[0].t_end <= 5.4
    assert saved_es[0].text == "Un gran escenario de inicio, sin duda."
    assert saved_es[0].kind == "translation"

    await worker.stop()
    assert not _live_tasks()


async def test_two_rooms_at_once_keep_their_streams_apart(tmp_path: Path, db) -> None:  # 5.3
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    settings = _settings(("r1", "en"), ("r2", "es"))

    def words(prefix: str, n: int) -> list[tuple[float, str, str]]:
        recs = []
        for i in range(n):
            end = "." if i % 4 == 3 else ""
            recs.append((0.5 + 0.5 * i, "source_delta", f" {prefix}src{i}{end}"))
            recs.append((0.6 + 0.5 * i, "target_delta", f" {prefix}tgt{i}{end}"))
        return recs

    w1 = _worker(
        _room("r1"), settings, bus, db, clock,
        Factory(clock, _script(tmp_path / "a.jsonl", words("alpha", 16))), IngestFactory(),
    )
    w2 = _worker(
        _room("r2", targets=["en"]), settings, bus, db, clock,
        Factory(clock, _script(tmp_path / "b.jsonl", words("beta", 16))), IngestFactory(),
    )

    await w1.start(None)
    await w2.start(None)
    await run_for(clock, 10.0)
    await w1.stop()
    await w2.stop()

    r1 = {lang: _history(bus, "r1", lang) for lang in ("en", "es")}
    r2 = {lang: _history(bus, "r2", lang) for lang in ("es", "en")}
    for msgs in (*r1.values(), *r2.values()):
        assert {"append", "close"} <= {m.type for m in msgs}
    r1_text = " ".join(m.text or "" for msgs in r1.values() for m in msgs)
    r2_text = " ".join(m.text or "" for msgs in r2.values() for m in msgs)
    assert "alpha" in r1_text and "beta" not in r1_text
    assert "beta" in r2_text and "alpha" not in r2_text
    # r2 speaks Spanish: its source deltas feed "es"; its free session runs
    # the glossary engine, so "en" is translated from them (FakeTranslate)
    assert "betasrc0" in _segments(r2["es"])[0] and "[en] betasrc0" in _segments(r2["en"])[0]
    # each track numbers its own segments
    assert min(_segments(r1["en"])) == 0 and min(_segments(r2["es"])) == 0

    for room_id, word in (("r1", "alpha"), ("r2", "beta")):
        talk_id = _free_id(room_id)
        saved = [s for lang in ("en", "es") for s in await db.get_segments(talk_id, lang, "live")]
        assert saved and all(word in s.text and s.room_id == room_id for s in saved)


async def test_interleaved_sessions_never_share_a_segment(tmp_path: Path, db) -> None:
    """Rotation: the draining session keeps talking while the new one starts.
    Each (session, language) has its own assembler and the worker renumbers
    their segments into one id space per (room, language)."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    relay = RelayCfg(standby_at=3.0, force_at=4.0, stall_timeout=8.0)

    def stream(n: int) -> Path:  # words tagged with the session: " s2w7"
        recs = [(0.3 * i, "target_delta", f" s{n}w{i}") for i in range(1, 60)]
        return _script(tmp_path / f"s{n}.jsonl", recs)

    factory = Factory(clock, *(stream(n) for n in range(1, 7)))
    worker = _worker(_room(), _settings(relay=relay), bus, db, clock, factory, IngestFactory())

    await worker.start(None)
    await run_for(clock, 12.0)
    await worker.stop()

    es = _all(bus, "r1", "es")
    segs = _segments(es)
    owner = {}
    for seg, text in segs.items():
        sessions = {word.split("w")[0] for word in text.split()}
        assert len(sessions) == 1, text  # never two sessions in one segment
        owner[seg] = sessions.pop()
    assert len(set(owner.values())) >= 2  # there was a rotation
    # a draining session's segment kept growing after the next session's began
    appends = [m.seg for m in es if m.type == "append"]
    first_of = {}
    for i, seg in enumerate(appends):
        first_of.setdefault(owner[seg], i)
    s1_last = max(i for i, seg in enumerate(appends) if owner[seg] == "s1")
    assert s1_last > first_of["s2"]
    # every segment is closed exactly once and ids are never reused
    closes = [m.seg for m in es if m.type == "close"]
    assert sorted(closes) == sorted(set(closes)) == sorted(segs)


async def test_segments_close_after_a_quiet_stretch(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    fixture = _script(
        tmp_path / "f.jsonl",
        [
            (1.0, "target_delta", " sin puntuación"),
            (1.2, "source_delta", " no period"),
            (60.0, "session_resumption_update", ""),  # keeps the session open, silent
        ],
    )
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, fixture), IngestFactory())

    await worker.start(None)
    await run_for(clock, 2.0)
    assert "close" not in {m.type for m in _history(bus, "r1", "es")}
    await run_for(clock, 2.0)  # 3 s without output
    assert "close" in {m.type for m in _history(bus, "r1", "es")}
    assert "close" in {m.type for m in _history(bus, "r1", "en")}
    await worker.stop()

    assert [s.text for s in await db.get_segments(FREE_R1, "es", "live")] == ["sin puntuación"]


async def test_the_free_session_and_the_engine_config(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, FAKE_LT)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(("r1", "en")), bus, db, clock, factory, ingests)

    await worker.start(None)
    await run_for(clock, 0.5)

    assert worker.talk is not None
    assert (worker.talk.id, worker.talk.title, worker.talk.language, worker.talk.engine) == (
        FREE_R1, "Sesión libre", "en", "fast",
    )
    assert factory.configs[0].kind == "fast"
    assert (factory.configs[0].source_lang, factory.configs[0].target_lang) == ("en", "es")
    assert ingests.made[0].args == ("file", "fake://r1", False)
    view = worker.view()
    now = view.pop("now")
    assert view == {"slug": "r1", "name": "Sala r1", "langs": ["en", "es"], "next": None}
    assert {k: now[k] for k in ("talk_id", "title", "speakers", "language", "abstract", "free")} == {
        "talk_id": FREE_R1, "title": "Sesión libre", "speakers": [], "language": "en", "abstract": "", "free": True,
    }
    assert now["start"] == now["starts_at"][11:16] and now["end"] == now["ends_at"][11:16]
    stored = await db.get_talk(FREE_R1)
    assert stored is not None and stored.status == "live" and stored.actual_start is not None

    await worker.stop()

    stored = await db.get_talk(FREE_R1)
    assert stored is not None and stored.status == "done" and stored.actual_end is not None
    assert worker.talk is None and worker.view()["now"] is None
    assert worker.status().state == "idle"
    last = _all(bus, "r1", "es")[-1]
    assert last.type == "talk" and last.data["talk_id"] is None  # the audience goes idle


# ------------------------------------------ next talk / latency_p50 (task-11r-brief.md item 7, Ruling 2) --


async def test_view_next_reflects_set_next_talk(db) -> None:
    """set_next_talk (called by Autopilot's tick, glosa/scheduler.py) is the
    only thing that changes view()["next"]; view() itself never touches the
    DB."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests)

    assert worker.view()["next"] is None

    nxt = _talk("n1", language="en", targets=("es",))
    nxt.start = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc)
    worker.set_next_talk(nxt)

    nxt.end = datetime(2026, 1, 1, 16, 10, tzinfo=timezone.utc)
    nxt.abstract = "What the talk is about."
    worker.set_next_talk(nxt)
    assert worker.view()["next"] == {
        "talk_id": "n1", "title": "Talk n1", "speakers": [], "language": "en", "start": "15:30",
        "end": "16:10", "starts_at": "2026-01-01T15:30:00+00:00", "ends_at": "2026-01-01T16:10:00+00:00",
        "abstract": "What the talk is about.", "free": False,
    }

    worker.set_next_talk(None)
    assert worker.view()["next"] is None


async def test_latency_p50_is_none_when_idle_or_under_the_sample_floor(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests)

    assert worker.latency_p50() is None  # idle: no run at all

    await worker.start(None)
    await run_for(clock, 0.1)
    assert worker.latency_p50() is None  # a run exists, but no closed samples yet

    run = worker._run
    for i in range(9):  # 9 closed samples: alternating on_pause/on_output closes the PREVIOUS stretch
        run.latency.on_pause(float(2 * i))
        run.latency.on_output(float(2 * i + 1))
    assert len(run.latency.samples()) == 8
    assert worker.latency_p50() is None  # below the default floor (10)

    run.latency.on_pause(float(2 * 9))
    run.latency.on_output(float(2 * 9 + 1))
    assert len(run.latency.samples()) == 9
    assert worker.latency_p50(min_samples=9) == run.latency.p50()

    await worker.stop()
    assert worker.latency_p50() is None  # the run is gone once the talk ends


async def test_status_cost_and_events_while_running(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT, usd=0.001), IngestFactory())

    assert worker.status().state == "idle"
    await worker.start(None)
    await run_for(clock, 6.0)

    status = worker.status()
    assert status.state in ("green", "yellow")
    assert status.talk_id == FREE_R1
    assert status.level_db > -30  # the chunk at t=5.9 s is voice
    texts = [r for r in map(json.loads, FAKE_LT.read_text().splitlines()) if r["kind"].endswith("_delta")]
    assert status.cost_usd == pytest.approx(0.001 * sum(r["t"] <= 6.0 for r in texts))

    await worker.stop()

    assert await db.total_cost() == pytest.approx(worker.status().cost_usd)
    types = [e.type for e in await db.recent_events(20)]
    assert "talk_start" in types and "talk_end" in types


async def test_add_external_cost_joins_the_running_run_and_is_a_noop_when_idle(db) -> None:
    """Task 17: glosa/summary.py's SummaryScheduler calls this to join the
    room's own cost accounting (component "summary")."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory())

    worker.add_external_cost("summary", 0.01, 1.0)  # idle: no run yet, silently dropped
    assert worker.status().cost_usd == 0.0

    await worker.start(None)
    worker.add_external_cost("summary", 0.02, 1.0)
    assert worker.status().cost_usd == pytest.approx(0.02)

    await worker.stop()
    assert await db.total_cost() == pytest.approx(0.02)  # flushed at teardown


async def test_engine_that_cannot_be_built_turns_the_room_red(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)

    def broken(cfg: EngineConfig):
        raise RuntimeError("no engine for you")

    worker = _worker(_room(), _settings(), bus, db, clock, broken, IngestFactory())
    await worker.start(None)
    await run_for(clock, 1.0)

    assert worker.status().state == "red"
    await worker.stop()
    assert not _live_tasks()


async def test_play_file_starts_the_free_session_when_idle(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests)
    clip = _clip(tmp_path)

    await worker.play_file(clip)
    await run_for(clock, 6.0)

    assert ingests.made[0].args == ("file", clip, True)
    assert worker.talk is not None and worker.talk.id == FREE_R1
    assert "append" in {m.type for m in _history(bus, "r1", "es")}
    await worker.stop()


async def test_play_file_swaps_the_source_of_a_running_room(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory()
    factory = Factory(clock, FAKE_LT)
    worker = _worker(_room(), _settings(), bus, db, clock, factory, ingests)

    await worker.start(None)
    await run_for(clock, 2.0)
    talk = worker.talk
    await worker.play_file(_clip(tmp_path))
    await run_for(clock, 4.0)

    assert [i.args[:2] for i in ingests.made] == [("file", "fake://r1"), ("file", _clip(tmp_path))]
    assert ingests.made[0].closed  # the old source was shut (ffmpeg killed), not left to the GC
    assert ingests.made[1].yielded >= 30  # the new source is being fed
    assert worker.talk is talk  # same talk, same relay session: no new engine
    assert len(factory.configs) == 1
    assert "append" in {m.type for m in _history(bus, "r1", "es")}
    await worker.stop()


async def test_a_file_that_ends_ends_the_session(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory(seconds=5.0)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests, tail_s=1.0)

    await worker.start(None)
    await run_for(clock, 8.0)

    assert worker.talk is None
    assert worker.status().state == "idle"
    stored = await db.get_talk(FREE_R1)
    assert stored is not None and stored.status == "done"
    es = _all(bus, "r1", "es")
    assert es[-1].type == "talk" and es[-1].data["talk_id"] is None
    opened = {m.seg for m in es if m.type == "append"}
    closed = {m.seg for m in es if m.type == "close"}
    assert opened and opened == closed  # nothing left open
    assert not _live_tasks()
    await worker.stop()  # already stopped: a no-op


async def test_a_source_that_dies_turns_the_room_red(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory(seconds=1.0, error="ffmpeg: connection refused")
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)

    await worker.start(None)
    await run_for(clock, 3.0)

    status = worker.status()
    assert status.state == "red"
    assert "source" in status.detail and "connection refused" in status.detail
    assert worker.talk is not None  # the talk is still on; the audio is not
    assert "source_down" in [e.type for e in await db.recent_events(10)]

    await worker.play_file(_clip(tmp_path))  # the operator plays something
    await run_for(clock, 0.5)
    assert worker.status().state != "red"
    await worker.stop()
    assert not _live_tasks()


async def test_a_source_that_recovered_then_ended_ends_the_talk(db) -> None:
    """AudioIngest keeps last_error after a restart that worked: a clean end
    later on is still a clean end."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory(seconds=3.0, blip_at=1.0)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)

    await worker.start(None)
    await run_for(clock, 5.0)

    assert worker.talk is None and worker.status().state == "idle"
    assert "source_down" not in [e.type for e in await db.recent_events(10)]


def _loop_settings() -> Settings:
    return Settings(
        gemini_api_key="test-key", admin_password="test-password",
        rooms=[RoomCfg(id="r1", name="Sala r1", source_type="file", source_url="fake://r1", loop=True)],
    )


async def test_a_looping_file_restarts_seamlessly_during_a_free_session(db) -> None:  # the demo loop
    """``loop: true`` (config.demo-fake.yaml): a free session whose file ends
    plays it again instead of ending -- on a continuous audio clock, with a
    fresh engine session (FakeEngine replays in step with the audio), and
    no "recent reconnect" nor fallback incident for it."""
    clock = DrivenClock()
    ingests = IngestFactory(seconds=3.0)
    factory = Factory(clock, FAKE_LT)
    worker = _worker(_room(), _loop_settings(), CaptionBus(clock=clock), db, clock, factory, ingests, tail_s=1.0)
    await worker.start(None)
    free = worker.talk

    await run_for(clock, 7.5)

    assert worker.talk is free and worker.status().state != "idle"
    assert [i.args for i in ingests.made] == [("file", "fake://r1", False)] * 3
    sent = [t for engine in factory.engines for t in engine.sent]
    assert sent == sorted(sent) and len(set(sent)) == len(sent)  # one continuous audio clock
    assert max(sent) == pytest.approx(7.4, abs=0.15)  # no tail silence between loops
    assert len(factory.configs) == 3  # a fresh engine session per loop
    assert "reconnect" not in worker.status().detail
    assert (await db.get_talk(free.id)).status == "live"
    await worker.stop()


async def test_a_looping_file_still_ends_an_agenda_talk(db) -> None:
    clock = DrivenClock()
    ingests = IngestFactory(seconds=3.0)
    worker = _worker(_room(), _loop_settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), ingests,
                     tail_s=0.5)
    await worker.start(_agenda_talk("a"))

    await run_for(clock, 5.0)

    assert worker.talk is None and len(ingests.made) == 1
    assert (await db.get_talk("a")).status == "done"


async def test_play_file_rejects_a_missing_file(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), IngestFactory())

    with pytest.raises(FileNotFoundError):
        await worker.play_file(str(tmp_path / "missing.opus"))
    assert worker.talk is None


class _ClipIngests(IngestFactory):
    """The room's own source (fake://...) runs forever; a test clip lasts
    ``clip_s`` s and ends cleanly."""

    def __init__(self, clip_s: float) -> None:
        super().__init__()
        self.clip_s = clip_s

    def __call__(self, source_type, source_url, realtime, clock) -> FakeIngest:
        seconds = None if source_url.startswith("fake://") else self.clip_s
        ingest = FakeIngest(source_type, source_url, realtime, clock, seconds=seconds)
        self.made.append(ingest)
        return ingest


async def test_play_file_refuses_an_open_agenda_talk(tmp_path: Path, db) -> None:  # C1, Ruling 60
    """Test audio must never end, replace or pollute an agenda talk: not a
    live one, nor one whose source is down."""
    from glosa.room import SoundCheckRefused

    clock = DrivenClock()
    ingests = IngestFactory(seconds=1.0, error="ffmpeg: connection refused")
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)
    await worker.start(_agenda_talk("a"))
    await run_for(clock, 0.5)

    with pytest.raises(SoundCheckRefused, match="Talk a"):
        await worker.play_file(_clip(tmp_path))
    assert worker.talk.id == "a" and len(ingests.made) == 1 and not worker.testing

    await run_for(clock, 2.0)
    assert worker.status().state == "red" and worker.talk.id == "a"  # source down, talk on
    with pytest.raises(SoundCheckRefused):
        await worker.play_file(_clip(tmp_path))
    assert worker.talk.id == "a" and len(ingests.made) == 1
    await worker.stop()


async def test_a_test_clip_over_a_free_session_switches_back_to_the_rooms_source(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    ingests = _ClipIngests(clip_s=2.0)
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)
    await worker.start(None)
    await run_for(clock, 1.0)
    free = worker.talk

    await worker.play_file(_clip(tmp_path))
    await run_for(clock, 1.0)
    assert worker.testing and worker.talk is free

    await run_for(clock, 3.0)  # the clip and its tail end

    assert worker.talk is free and not worker.testing  # the free session goes on...
    assert [i.args[1] for i in ingests.made] == ["fake://r1", _clip(tmp_path), "fake://r1"]  # ...on its own source
    assert ingests.made[2].yielded > 0
    await worker.stop()


async def test_a_test_clip_in_an_idle_room_ends_its_own_session(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    ingests = _ClipIngests(clip_s=2.0)
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)

    await worker.play_file(_clip(tmp_path))
    await run_for(clock, 1.0)
    assert worker.testing and worker.talk.id == FREE_R1

    await run_for(clock, 3.0)

    assert worker.talk is None and not worker.testing and worker.status().state == "idle"
    assert len(ingests.made) == 1
    await worker.stop()


async def test_the_free_session_id_is_unique_per_run_in_the_event_timezone(db) -> None:  # Ruling 27
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    settings = _settings(timezone="America/Argentina/Buenos_Aires")
    worker = _worker(_room(), settings, bus, db, clock, Factory(clock, FAKE_LT), IngestFactory())

    await worker.start(None)  # 2026-01-01 00:00:00 UTC is 21:00 of Dec 31 in Buenos Aires
    first = worker.talk
    assert first is not None and first.id == "free-r1-20251231T210000"
    assert first.start.utcoffset() == timedelta(hours=-3)
    assert [t.id for t in await db.get_talks("r1", date(2025, 12, 31))] == [first.id]
    await run_for(clock, 1.0)
    await worker.stop()

    clock.advance(60.0)
    await worker.start(None)
    second = worker.talk.id
    await worker.stop()
    await worker.start(None)  # same second: still a run of its own
    third = worker.talk.id
    await worker.stop()

    assert second == "free-r1-20251231T210101"
    assert len({first.id, second, third}) == 3 and third.startswith(second)


async def test_segment_ids_keep_growing_when_the_source_comes_back(tmp_path: Path, db) -> None:
    """Segment numbers belong to the worker, not to one run of the pipeline:
    after a restart the audience must not see seg 0 again (room.js would
    append the new text to the old phrase)."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    ingests = IngestFactory(seconds=6.0, error="ffmpeg: connection refused")
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5)

    await worker.start(None)
    await run_for(clock, 7.0)  # captions from 4.5 s, then the source dies at 6 s
    assert worker.status().state == "red"
    before = [m for m in _all(bus, "r1", "es") if m.seg is not None]
    assert before

    await worker.play_file(_clip(tmp_path))  # same talk, new pipeline
    await run_for(clock, 7.0)
    await worker.stop()

    after = [m for m in _all(bus, "r1", "es") if m.seg is not None][len(before):]
    assert after
    assert min(m.seg for m in after) > max(m.seg for m in before)


class GatedDb:
    """A Database whose save of a segment containing `word` waits for `gate`."""

    def __init__(self, db, word: str) -> None:
        self._db = db
        self.word = word
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def save_segment(self, talk_id, room_id, lang, kind, version, text, t_start, t_end):
        if self.word in text and not self.gate.is_set():
            self.blocked.set()
            await self.gate.wait()
        return await self._db.save_segment(talk_id, room_id, lang, kind, version, text, t_start, t_end)


async def test_a_draining_session_segment_is_closed_and_saved_even_mid_save(tmp_path: Path, db) -> None:
    """One delta of a draining session closes a segment and opens the next
    (" fin. siguiente"). While the close is being saved, the ticker runs and
    the newer session has taken over: the segment it opened must still be
    closed and saved, not orphaned."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    relay = RelayCfg(standby_at=3.0, force_at=4.0, stall_timeout=8.0)
    s1 = _script(tmp_path / "s1.jsonl", [
        *((0.5 * i, "target_delta", f" a{i}") for i in range(1, 6)),
        (6.0, "target_delta", " fin. siguiente"),
    ])
    s2 = _script(tmp_path / "s2.jsonl", [(0.3 * i, "target_delta", f" b{i}") for i in range(1, 60)])
    gated = GatedDb(db, "fin")
    worker = _worker(_room(), _settings(relay=relay), bus, gated, clock, Factory(clock, s1, s2), IngestFactory())

    await worker.start(None)
    await run_for(clock, 6.0)
    assert gated.blocked.is_set()  # the consumer is saving "fin." right now
    await run_for(clock, 1.0)  # the ticker sweeps meanwhile
    gated.gate.set()
    await run_for(clock, 5.0)
    await worker.stop()

    es = _all(bus, "r1", "es")
    segs = _segments(es)
    following = next(seg for seg, text in segs.items() if "siguiente" in text)
    assert following in {m.seg for m in es if m.type == "close"}
    assert "siguiente" in [s.text for s in await db.get_segments(FREE_R1, "es", "live")]


async def test_real_audio_through_the_whole_pipeline(db) -> None:
    """samples/en_clip.opus through the real AudioIngest (ffmpeg, not paced)
    and EnergyVad, FakeEngine for the engine: no API."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = RoomWorker(
        replace(_room(), source_url=str(EN_CLIP)), _settings(), bus, db, clock, Factory(clock, FAKE_LT),
        ingest_factory=AudioIngest, realtime=False, tail_s=6.0,
    )

    await worker.start(None)
    for _ in range(500):  # ffmpeg decodes the 93 s clip in well under a second
        await asyncio.sleep(0.01)
        if worker.audio_s >= 92.0:
            break
    assert worker.audio_s >= 92.0  # the whole clip went through VAD and relay
    await run_for(clock, 8.0)  # the 6 s silent tail, then the session ends

    es = _all(bus, "r1", "es")
    assert {"append", "close"} <= {m.type for m in es}
    assert worker.talk is None
    assert [s.text for s in await db.get_segments(FREE_R1, "es", "live")][0] == (
        "Un gran escenario de inicio, sin duda."
    )
    await worker.stop()


# ------------------------------------------------------------------ live (billed)


@pytest.mark.live
async def test_live_translate_room_smoke(tmp_path: Path) -> None:
    """20 s of the EN clip through a real room with Live Translate (~$0.02)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set")
    from glosa.engines.live_translate import LiveTranslateEngine

    clip = tmp_path / "en_20s.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-t", "20", "-i", str(EN_CLIP), "-ac", "1", "-ar", "16000", str(clip)],
        check=True,
    )
    clock = RealClock()
    bus = CaptionBus(clock=clock)
    database = init_db(tmp_path / "live.db")
    settings = _settings()
    worker = RoomWorker(
        replace(_room(), source_url=str(clip)), settings, bus, database, clock,
        lambda cfg: LiveTranslateEngine(cfg, api_key, clock, price_per_min=settings.prices.lt_per_min),
    )

    talk_ids = []

    async def run() -> None:
        await worker.start(None)
        talk_ids.append(worker.talk.id)
        while worker.talk is not None:  # 20 s of audio + the tail
            await asyncio.sleep(0.5)

    try:
        await asyncio.wait_for(run(), timeout=60)
    finally:
        await worker.stop()

    es = bus.history("r1", "es", talk_ids[0])
    assert {"append", "close"} <= {m.type for m in es}, [m.type for m in es]
    saved = await database.get_segments(talk_ids[0], "es", "live")
    assert saved and all(s.text for s in saved)
    assert await database.total_cost() > 0
    print("\nLIVE es segments:", [s.text for s in saved])
    print("LIVE cost usd:", await database.total_cost())
    database.close()


ES_CLIP = ROOT / "samples" / "es_clip.opus"
LIVE_VOCAB = ["Kubernetes", "control plane", "namespaces", "labels", "workloads", "Grafana", "Loki", "AWS", "GCP", "Azure"]


def _live_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key and (ROOT / ".env").exists():
        key = Settings.load(env_path=str(ROOT / ".env"), config_path=str(ROOT / "config.yaml")).gemini_api_key
    if not key:
        pytest.skip("GEMINI_API_KEY not set")
    return key


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else float("nan")


@pytest.mark.live
async def test_live_glossary_room(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:  # 10.6, RoomWorker
    """60 s of the ES clip, real time, engine "glossary": transcribe-live +
    flash-lite into English (about US$0.01). Source latency: the "close" of
    each utterance (its final) after the end of the speech (the room's
    end_utterance() on the VAD pause, minus the VAD's 400 ms). Translation
    latency: from the cut to the answer (the lane's own measure).

    Also counts the stale-text symptoms of the first runs: a "set" that
    starts with any version of the segment closed before it (old text
    flashing back), and a translated source that repeats text already
    translated."""
    from glosa.engines.transcribe import TranscribeLiveEngine

    key = _live_key()
    clip = tmp_path / "es_60s.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-t", "60", "-i", str(ES_CLIP), "-ac", "1", "-ar", "16000", str(clip)], check=True
    )
    clock = RealClock()
    bus = CaptionBus(clock=clock)
    database = init_db(tmp_path / "live.db")
    settings = Settings(
        gemini_api_key=key, admin_password="test-password",
        rooms=[RoomCfg(id="r1", name="Sala r1", source_type="file", source_url=str(clip), language="es")],
    )
    engines: list[TranscribeLiveEngine] = []

    def factory(cfg: EngineConfig) -> TranscribeLiveEngine:
        engine = TranscribeLiveEngine(cfg, key, clock, price_per_min=settings.prices.transcribe_per_min)
        engines.append(engine)
        return engine

    worker = RoomWorker(replace(_room(targets=["en"]), source_url=str(clip)), settings, bus, database, clock, factory)
    pauses: list[float] = []  # when the room ended an utterance (a VAD pause)
    closes: list[float] = []  # when an es segment closed (its final)
    source_msgs: list[tuple[str, int | None, str | None]] = []
    publish = bus.publish

    def spy_publish(room_id, lang, type, **payload):
        if lang == "es" and type == "close":
            closes.append(clock.now())
        if lang == "es" and type in ("set", "close"):
            source_msgs.append((type, payload.get("seg"), payload.get("text")))
        return publish(room_id, lang, type, **payload)

    bus.publish = spy_publish  # type: ignore[method-assign]
    lanes = []
    translated: list[str] = []  # the source of each translated segment, in order
    on_translation = worker._on_translation

    async def spy_on_translation(run, seg) -> None:
        translated.append(seg.source)
        await on_translation(run, seg)

    worker._on_translation = spy_on_translation  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="glosa.engines.transcribe")

    async def run() -> None:
        await worker.start(_talk("live-g1", glossary=tuple(GlossaryTerm(t, True) for t in LIVE_VOCAB)))
        relay = worker._run.relay
        lanes.append(worker._run.lane)
        end_utterance = relay.end_utterance

        async def spy_end_utterance() -> None:
            pauses.append(clock.now())
            await end_utterance()

        relay.end_utterance = spy_end_utterance  # type: ignore[method-assign]
        while worker.talk is not None:  # 60 s of audio, then the 5 s tail
            await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(run(), timeout=100)
    finally:
        await worker.stop()

    def words(text: str) -> list[str]:
        return [w for w in ("".join(ch for ch in t.casefold() if ch.isalnum()) for t in text.split()) if w]

    flashbacks = []  # a set that starts with any whole version of the segment closed before it
    closed_versions: list[str] = []
    versions: dict[int | None, list[str]] = {}
    for kind, seg, text in source_msgs:
        if kind == "set":
            for version in closed_versions:
                if len(words(version)) >= 3 and (
                    (text or "").startswith(version) or words(text or "")[: len(words(version))] == words(version)
                ):
                    flashbacks.append(text)
                    break
            versions.setdefault(seg, []).append(text or "")
        else:
            closed_versions = versions.get(seg, [])
    repeats = []  # a translated source (4+ words) that is already in the ones before it
    for n, source in enumerate(translated):
        if len(words(source)) >= 4 and " ".join(words(source)) in " ".join(words(" ".join(translated[:n]))):
            repeats.append(source)

    source_lat: list[float] = []
    for n, paused in enumerate(pauses):
        following = pauses[n + 1] if n + 1 < len(pauses) else float("inf")
        close = next((c for c in closes if paused < c < following), None)
        if close is not None:
            source_lat.append(close - (paused - 0.4))
    stats = lanes[0].pipeline.stats
    es = [s.text for s in await database.get_segments("live-g1", "es", "live")]
    en = [s.text for s in await database.get_segments("live-g1", "en", "live")]
    cost = await database.total_cost()
    said = sum(len(words(text)) for text in es)
    stale_logs = [r.getMessage() for r in caplog.records if "stale" in r.getMessage() or "repeats" in r.getMessage()]
    print(
        f"\nLIVE glossary room: {len(pauses)} pauses, {len(closes)} es closes, {len(es)} es / {len(en)} en segments"
        f"\n  source after end of speech: p50={_pct(source_lat, 0.5):.3f}s p90={_pct(source_lat, 0.9):.3f}s"
        f" n={len(source_lat)} {[round(v, 2) for v in source_lat]}"
        f"\n  translation after the cut:  p50={_pct(stats['latencies_s'], 0.5):.3f}s"
        f" p90={_pct(stats['latencies_s'], 0.9):.3f}s n={stats['translated']} failed={stats['failed']}"
        f"\n  cost: total={cost:.5f} (transcribe {sum(e.usd_total for e in engines):.5f},"
        f" translate {stats['usd_total']:.5f})"
        f"\n  flashbacks of old text: {len(flashbacks)} {flashbacks}"
        f"\n  translated twice: {len(repeats)} {repeats}"
        f"\n  words translated / words said: {sum(len(words(t)) for t in translated)} / {said}"
        f"\n  engine stale cuts/drops: {len(stale_logs)}"
    )
    for message in stale_logs:
        print("  STALE:", message)
    for text in es:
        print("  ES:", text)
    for text in en:
        print("  EN:", text)

    assert es and en
    assert source_lat and _pct(source_lat, 0.5) < 1.5
    assert stats["translated"] >= 10 and stats["failed"] <= 2
    assert 0 < cost < 0.05
    database.close()


# ------------------------------------------------------- on_talk_end, reconnect (T9)


class HookRecorder:
    """on_talk_end: records each ended talk (id, status, actual_end)."""

    def __init__(self, fail: bool = False) -> None:
        self.ended: list[tuple[str, str, object]] = []
        self.fail = fail

    async def __call__(self, talk) -> None:
        await asyncio.sleep(0)
        self.ended.append((talk.id, talk.status, talk.actual_end))
        if self.fail:
            raise RuntimeError("export exploded")


def _agenda_talk(talk_id: str, room_id: str = "r1"):
    from datetime import datetime, timezone

    from glosa.models import Talk

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Talk(
        id=talk_id, room_id=room_id, title=f"Talk {talk_id}", speakers=[], language="en", targets=["es"],
        engine="fast", start=start, end=start + timedelta(hours=1), abstract="", tags=[], glossary=[],
        status="scheduled", actual_start=None, actual_end=None,
    )


async def test_on_talk_end_runs_for_every_way_a_talk_ends(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    hook = HookRecorder()
    ingests = IngestFactory(seconds=2.0)  # each source plays 2 s, then ends by itself
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), ingests, tail_s=0.5,
                     on_talk_end=hook)

    await worker.start(_agenda_talk("a"))
    await run_for(clock, 0.5)
    await worker.start(_agenda_talk("b"))  # replaces a
    await run_for(clock, 0.5)
    await worker.stop()  # ends b
    await worker.start(_agenda_talk("c"))
    await run_for(clock, 4.0)  # c's source ends: the talk ends by itself
    await worker.drain_hooks()

    assert [talk_id for talk_id, _, _ in hook.ended] == ["a", "b", "c"]
    assert all(status == "done" and actual_end is not None for _, status, actual_end in hook.ended)
    assert not _live_tasks()


async def test_a_failing_on_talk_end_is_logged_and_breaks_nothing(db, caplog: pytest.LogCaptureFixture) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    hook = HookRecorder(fail=True)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory(),
                     on_talk_end=hook)

    await worker.start(_agenda_talk("a"))
    await run_for(clock, 0.5)
    await worker.start(_agenda_talk("b"))
    await run_for(clock, 0.5)
    await worker.drain_hooks()

    assert [talk_id for talk_id, _, _ in hook.ended] == ["a"]
    assert worker.talk is not None and worker.talk.id == "b"  # the new talk runs anyway
    assert "export exploded" in caplog.text
    await worker.stop()
    await worker.drain_hooks()


async def test_the_hook_runs_in_the_background(db) -> None:
    """stop() does not wait for a slow hook (the autopilot's tick must not)."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(talk) -> None:
        started.set()
        await gate.wait()

    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory(), on_talk_end=slow)
    await worker.start(_agenda_talk("a"))
    await run_for(clock, 0.5)

    await asyncio.wait_for(worker.stop(), 5)
    await asyncio.wait_for(started.wait(), 1)
    gate.set()
    await asyncio.wait_for(worker.drain_hooks(), 1)


async def test_drain_hooks_cancels_a_hook_that_outlives_its_grace(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)

    async def forever(talk) -> None:
        await asyncio.Event().wait()

    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory(), on_talk_end=forever)
    await worker.start(None)
    await run_for(clock, 0.5)
    await worker.stop()

    await asyncio.wait_for(worker.drain_hooks(timeout=0.05), 2)
    assert not _live_tasks()


async def test_reconnect_asks_the_running_relay_for_a_new_session(db, monkeypatch: pytest.MonkeyPatch) -> None:  # 9.4
    from glosa.engines.relay import SessionRelay

    calls: list[str] = []
    real = SessionRelay.reconnect

    async def spy(self, reason: str) -> None:
        calls.append(reason)
        await real(self, reason)

    monkeypatch.setattr(SessionRelay, "reconnect", spy)
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, FAKE_LT)
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.reconnect("manual")  # nothing running: a no-op
    assert calls == []

    await worker.start(None)
    await run_for(clock, 1.0)
    await worker.reconnect("manual")
    await run_for(clock, 1.0)

    assert calls == ["manual"]
    assert len(factory.configs) == 2  # a new engine session was opened
    await worker.stop()


async def test_restarting_the_running_talk_takes_its_edited_fields_from_the_db(tmp_path: Path, db) -> None:
    """An admin edit of a live talk (targets, title...) is in the DB; a
    restart of that same talk (a new source after the old one died, or
    start(worker.talk)) must use it, not write the worker's stale copy back."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, FAKE_LT)
    ingests = IngestFactory(seconds=1.0, error="ffmpeg: connection refused")
    worker = _worker(_room(), _settings(), bus, db, clock, factory, ingests, tail_s=0.5)
    await worker.start(_agenda_talk("a"))
    await run_for(clock, 2.0)
    assert worker.status().state == "red" and worker.talk.id == "a"  # the source died, the talk is on
    began = worker.talk.actual_start

    await db.update_talk("a", targets=["en", "es"], title="Edited while live")
    await worker.start(worker.talk)  # same talk, new pipeline (play_file refuses an agenda talk: C1)
    await run_for(clock, 0.5)

    stored = await db.get_talk("a")
    assert stored.targets == ["en", "es"] and stored.title == "Edited while live"
    assert worker.talk.targets == ["en", "es"] and worker.talk.title == "Edited while live"
    assert stored.actual_start == began and stored.status == "live"
    await worker.stop()


# -------------------------------------------------------- glossary engine (T10)


def _finals(path: Path) -> list[str]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r["text"] for r in rows if r["kind"] == "source_final"]


def _track(bus: CaptionBus, lang: str, talk_id: str, room_id: str = "r1") -> list[CaptionMsg]:
    return sorted(bus.history(room_id, lang, talk_id) + bus.history(room_id, lang, None), key=lambda m: m.id)


def _closed_texts(msgs: list[CaptionMsg]) -> list[str]:
    """The text each segment had when it closed: its appends, or its last set."""
    text: dict[int, str] = {}
    out: list[str] = []
    for m in msgs:
        if m.type == "append":
            text[m.seg] = text.get(m.seg, "") + (m.text or "")
        elif m.type == "set":
            text[m.seg] = m.text or ""
        elif m.type == "close":
            out.append(text.get(m.seg, ""))
    return out


async def test_a_glossary_talk_sets_its_source_and_translates_every_segment(db) -> None:  # 10.4
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    translate = FakeTranslate(usd=0.0001)
    factory = Factory(clock, TR_ES, usd=0.0002)
    glossary = (
        GlossaryTerm("Kubernetes", True), GlossaryTerm("kubernetes ", True), GlossaryTerm("control plane", True),
        GlossaryTerm("Grafana Loki", True),
    )
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, factory, IngestFactory(),
                     translate=translate)

    await worker.start(_talk("g1", glossary=glossary))
    await run_for(clock, 62.0)
    await worker.stop()

    cfg = factory.configs[0]
    assert (cfg.kind, cfg.source_lang, cfg.target_lang) == ("glossary", "es", None)
    assert cfg.vocabulary == ["Kubernetes", "control plane", "Grafana Loki"]  # deduped ignoring case

    finals = _finals(TR_ES)
    es = _track(bus, "es", "g1")
    assert "append" not in {m.type for m in es}
    sets = [m for m in es if m.type == "set"]
    assert len(sets) > 3 * len(finals)  # the interims rewrite the open segment
    assert _closed_texts(es) == finals  # each utterance closes with its final text
    for a, b in zip(sets, sets[1:]):
        assert (a.seg, a.text) != (b.seg, b.text)  # a repeated text is never published
    assert [s.text for s in await db.get_segments("g1", "es", "live")] == finals

    en = _track(bus, "en", "g1")
    appends = [m for m in en if m.type == "append"]
    assert len(appends) >= len(finals)
    assert [m.type for m in en if m.type in ("append", "close")] == ["append", "close"] * len(appends)
    assert all(m.text.startswith("[en] ") for m in appends)
    assert {c[1] for c in translate.calls} == {"en"}
    assert translate.calls[0][2] == list(glossary)  # the talk's glossary
    said = " ".join(finals).lower().split()
    translated = " ".join(c[0] for c in translate.calls).lower().split()
    assert difflib.SequenceMatcher(a=said, b=translated, autojunk=False).ratio() >= 0.9

    saved_en = await db.get_segments("g1", "en", "live")
    assert [s.text for s in saved_en] == [m.text for m in appends]
    assert all(s.kind == "translation" and 0.8 <= s.t_start <= s.t_end <= 61.0 for s in saved_en)
    assert en[-1].type == "talk" and en[-1].data["talk_id"] is None  # after every translation

    engine_usd = 0.0002 * sum(1 for r in map(json.loads, TR_ES.read_text().splitlines())
                              if r["kind"] in ("source_delta", "source_final"))
    assert worker.status().cost_usd == pytest.approx(engine_usd + 0.0001 * len(translate.calls), rel=0.05)
    assert await db.total_cost() == pytest.approx(worker.status().cost_usd)
    assert not _live_tasks()


async def test_the_glossary_engine_ends_the_utterance_on_each_pause_and_the_fast_one_does_not(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    glossary_factory, fast_factory = Factory(clock, TR_ES), Factory(clock, FAKE_LT)
    glossary = _worker(_room("r1", targets=["en"]), _settings(("r1", "es"), ("r2", "en")), bus, db, clock,
                       glossary_factory, IngestFactory())
    fast = _worker(_room("r2"), _settings(("r1", "es"), ("r2", "en")), bus, db, clock, fast_factory, IngestFactory())

    await glossary.start(_talk("g1"))
    await fast.start(_talk("f1", language="en", targets=("es",), engine="fast", room_id="r2"))
    await run_for(clock, 8.0)  # FakeIngest: 2 s of voice, 0.6 s of silence: a pause every 2.6 s

    assert glossary_factory.engines[0].end_utterances >= 2
    assert fast_factory.engines[0].end_utterances == 0
    await glossary.stop()
    await fast.stop()


async def test_repeated_interims_are_not_published_and_an_empty_final_removes_the_segment(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    translate = FakeTranslate()
    script = _transcript(tmp_path / "tr.jsonl", [
        (0.5, "interim", "Hola"), (1.0, "interim", "Hola"), (1.5, "interim", "Hola a todos"),
        (2.0, "final", "Hola a todos."),
        (3.0, "interim", "eh"), (3.5, "final", ""),
        (4.0, "interim", "Sigo."), (4.5, "final", "Sigo."),
    ])
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, script),
                     IngestFactory(), translate=translate)

    await worker.start(_talk("g1"))
    await run_for(clock, 6.0)
    await worker.stop()

    es = [(m.type, m.seg, m.text) for m in _track(bus, "es", "g1") if m.type in ("set", "close")]
    assert es == [
        ("set", 0, "Hola"), ("set", 0, "Hola a todos"), ("set", 0, "Hola a todos."), ("close", 0, None),
        ("set", 1, "eh"), ("set", 1, ""), ("close", 1, None),
        ("set", 2, "Sigo."), ("close", 2, None),  # the final repeats the last interim: only the close
    ]
    assert [s.text for s in await db.get_segments("g1", "es", "live")] == ["Hola a todos.", "Sigo."]
    assert [c[0] for c in translate.calls] == ["Hola a todos.", "Sigo."]


async def test_an_empty_translation_is_not_published(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    script = _transcript(tmp_path / "tr.jsonl", [
        (0.5, "final", "Uno."), (1.0, "final", "Dos."), (1.5, "final", "Tres."),
    ])
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, script),
                     IngestFactory(), translate=FakeTranslate(empty=("Dos.",)))

    await worker.start(_talk("g1"))
    await run_for(clock, 3.0)
    await worker.stop()

    assert _closed_texts(_track(bus, "en", "g1")) == ["[en] Uno.", "[en] Tres."]
    assert [s.text for s in await db.get_segments("g1", "en", "live")] == ["[en] Uno.", "[en] Tres."]


async def test_stopping_translates_what_is_still_open_before_the_talk_ends(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    script = _transcript(tmp_path / "tr.jsonl", [(0.5, "interim", "sin terminar la frase"), (60.0, "final", "x")])
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, script),
                     IngestFactory())

    await worker.start(_talk("g1"))
    await run_for(clock, 1.5)
    await worker.stop()

    en = _track(bus, "en", "g1")
    assert [m.type for m in en][-3:] == ["append", "close", "talk"]  # translated, then the talk ends
    assert _closed_texts(en) == ["[en] sin terminar la frase"]
    assert _closed_texts(_track(bus, "es", "g1")) == ["sin terminar la frase"]
    assert [s.text for s in await db.get_segments("g1", "es", "live")] == ["sin terminar la frase"]
    assert [s.text for s in await db.get_segments("g1", "en", "live")] == ["[en] sin terminar la frase"]
    assert not _live_tasks()


async def test_a_file_that_ends_leaves_no_pipeline_behind(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, TR_ES),
                     IngestFactory(seconds=10.0), tail_s=1.0)

    await worker.start(_talk("g1"))
    await run_for(clock, 13.0)

    assert worker.talk is None
    assert "append" in {m.type for m in _track(bus, "en", "g1")}
    assert not _live_tasks()


async def test_a_newer_session_closes_the_older_ones_open_segment(tmp_path: Path, db) -> None:
    """Rotation: the draining session's late final is dropped once the new
    session speaks; its segment closes with the last interim it showed."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    translate = FakeTranslate()
    relay = RelayCfg(standby_at=3.0, force_at=4.0, stall_timeout=8.0)
    s1 = _transcript(tmp_path / "s1.jsonl", [
        (0.5, "interim", "a1"), (1.5, "interim", "a1 a2"), (3.5, "interim", "a1 a2 a3"),
        (4.6, "final", "a1 a2 a3 fin."),  # after s2's first interim
    ])
    s2 = _transcript(tmp_path / "s2.jsonl", [
        (1.2, "interim", "b1"), (2.0, "interim", "b1 b2"), (2.5, "final", "b1 b2."),
    ])
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es"), relay=relay), bus, db, clock,
                     Factory(clock, s1, s2), IngestFactory(), translate=translate)

    await worker.start(_talk("g1"))
    await run_for(clock, 7.0)
    await worker.stop()

    es = _track(bus, "es", "g1")
    assert _closed_texts(es) == ["a1 a2 a3", "b1 b2."]
    assert "a1 a2 a3 fin." not in [m.text for m in es]
    assert " ".join(c[0] for c in translate.calls) == "a1 a2 a3 b1 b2."  # "fin." never reaches the lane
    assert [s.text for s in await db.get_segments("g1", "es", "live")] == ["a1 a2 a3", "b1 b2."]


async def test_a_glossary_talk_translates_to_every_target(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    script = _transcript(tmp_path / "tr.jsonl", [(0.5, "final", "Hola a todos.")])
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, script),
                     IngestFactory())

    await worker.start(_talk("g1", targets=("es", "en", "pt")))  # the spoken language is not a target
    await run_for(clock, 1.5)

    assert worker.langs() == ["es", "en", "pt"]
    assert worker.view()["langs"] == ["es", "en", "pt"]
    await worker.stop()
    for lang in ("en", "pt"):
        msgs = _track(bus, lang, "g1")
        assert _closed_texts(msgs) == [f"[{lang}] Hola a todos."]
        assert msgs[-1].type == "talk" and msgs[-1].data["talk_id"] is None


async def test_each_run_gets_a_translator_of_its_own_closed_with_the_run(db, translators) -> None:
    made = translators
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = RoomWorker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, Factory(clock, TR_ES),
                        ingest_factory=IngestFactory(), realtime=False)  # no translate: the real kind

    await worker.start(_talk("g1"))
    await run_for(clock, 5.0)
    assert "append" in {m.type for m in _track(bus, "en", "g1")}
    await worker.start(_talk("g2"))  # the next talk: g1's run ends
    assert [t.closed for t in made] == [1, 0]
    await run_for(clock, 1.0)
    await worker.stop()
    assert [t.closed for t in made] == [1, 1] and made[0].api_key == "test-key"

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))  # no lane: no Translator
    await run_for(clock, 1.0)
    await worker.stop()
    assert len(made) == 2


async def test_more_than_100_glossary_terms_are_capped_with_a_warning(db, caplog: pytest.LogCaptureFixture) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, TR_ES)
    glossary = tuple(GlossaryTerm(f"term{i}", True) for i in range(130)) + (GlossaryTerm("TERM0", True),)
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("g1", glossary=glossary))
    await run_for(clock, 0.5)
    await worker.stop()

    assert factory.configs[0].vocabulary == [f"term{i}" for i in range(100)]
    assert "130 glossary terms" in caplog.text
    assert ("warning", "vocabulary", "g1: 130 glossary terms: only the first 100 go to transcribe-live") in (
        await _events(db)
    )


async def test_the_free_session_engine_follows_the_room_language(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    kinds = {}
    for room_id, lang, default_en in (("r1", "es", "fast"), ("r2", "en", "fast"), ("r3", "en", "glossary")):
        factory = Factory(clock, TR_ES)
        settings = _settings((room_id, lang), default_engine_en=default_en)
        worker = _worker(_room(room_id), settings, bus, db, clock, factory, IngestFactory())
        await worker.start(None)
        await run_for(clock, 0.5)
        kinds[room_id] = (worker.talk.engine, factory.configs[0].kind)
        await worker.stop()

    assert kinds == {"r1": ("glossary", "glossary"), "r2": ("fast", "fast"), "r3": ("glossary", "glossary")}


# ------------------------------------------------ extra languages, fast engine (T11)


async def test_a_fast_talk_translates_its_extra_targets_from_the_source(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    translate = FakeTranslate(usd=0.0001)
    factory = Factory(clock, FAKE_LT)
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory(), translate=translate)

    await worker.start(_talk("f1", language="en", targets=("es", "pt"), engine="fast"))
    await run_for(clock, 12.0)
    assert worker.langs() == ["en", "es", "pt"]
    assert "pt" in worker.stream_langs()
    await worker.stop()

    cfg = factory.configs[0]
    assert (cfg.kind, cfg.source_lang, cfg.target_lang) == ("fast", "en", "es")  # Live Translate: the first
    es, pt, en = (_track(bus, lang, "f1") for lang in ("es", "pt", "en"))
    assert _closed_texts(es)[0] == "Un gran escenario de inicio, sin duda."
    assert not any(m.type == "append" and m.text.startswith("[") for m in es)  # es is Live Translate's only
    pt_texts = _closed_texts(pt)
    assert pt_texts and all(t.startswith("[pt] ") for t in pt_texts)
    assert {c[1] for c in translate.calls} == {"pt"}
    said = "".join(m.text or "" for m in en if m.type == "append").split()
    translated = " ".join(c[0] for c in translate.calls).split()
    assert difflib.SequenceMatcher(a=said, b=translated, autojunk=False).ratio() >= 0.9
    assert [s.text for s in await db.get_segments("f1", "pt", "live")] == pt_texts
    assert pt[-1].type == "talk" and pt[-1].data["talk_id"] is None
    assert not _live_tasks()


async def test_local_engine_mode_coerces_a_fast_talk_to_the_glossary_path(db, local_translators) -> None:
    """Task 16: engine_mode "local" has no local Live Translate -- every
    talk, whatever its own Talk.engine, runs the glossary-engine path
    (LocalParakeetEngine + a per-run LocalTranslator lane translating every
    target). glosa/engines/local.py has its own unit tests for the engine
    itself; this only covers glosa/room.py's wiring (the one-line kind
    coercion in _start_locked and the LocalTranslator branch in
    _build_engine)."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, TR_ES)  # any transcribe-style recording: the fixture's engine is a FakeEngine
    worker = RoomWorker(
        _room(targets=["en"]), _settings(("r1", "es"), engine_mode="local"), bus, db, clock, factory,
        ingest_factory=IngestFactory(), realtime=False,  # no translate override: the real (local) kind
    )

    await worker.start(_talk("t1", language="es", targets=("en",), engine="fast"))  # "fast" on the talk itself
    await run_for(clock, 5.0)
    await worker.stop()

    cfg = factory.configs[0]
    assert cfg.kind == "glossary"  # coerced: local mode ignores the talk's own "fast"
    assert len(local_translators) == 1
    assert local_translators[0].source_lang == "es"  # the talk's spoken language, not the target
    assert local_translators[0].closed == 1  # aclose() called on teardown, like Translator's


async def test_a_vad_pause_closes_the_extra_languages_utterance(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    script = _script(tmp_path / "lt.jsonl", [
        (0.5, "source_delta", " uno dos tres"), (1.0, "source_delta", " cuatro"),
        (60.0, "session_resumption_update", ""),
    ])
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, script), IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es", "pt"), engine="fast"))
    await run_for(clock, 2.8)  # FakeIngest: 2 s of voice, the VAD's pause at 2.4 s; no time cut before 3.5 s

    assert _closed_texts(_track(bus, "pt", "f1")) == ["[pt] uno dos tres cuatro"]
    await worker.stop()


async def test_a_fast_talk_with_one_target_has_no_translation_lane(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    translate = FakeTranslate()
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory(),
                     translate=translate)

    await worker.start(_talk("f1", language="en", targets=("en", "es"), engine="fast"))
    await run_for(clock, 8.0)

    assert worker.langs() == ["en", "es"]
    assert translate.calls == []
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("glosa-pipeline")]
    await worker.stop()


# ------------------------------------------------- fallback to the glossary engine (10.5)


def _failing_connect(tmp_path: Path, name: str = "fail.jsonl") -> Path:
    """A session whose connect fails (an error at t=0, retryable)."""
    path = tmp_path / name
    path.write_text(json.dumps({"t": 0.0, "kind": "error", "text": "503 UNAVAILABLE",
                                "meta": {"code": 503, "retryable": True}}) + "\n", encoding="utf-8")
    return path


async def _events(db, n: int = 50) -> list[tuple[str, str, str]]:
    return [(e.level, e.type, e.message) for e in reversed(await db.recent_events(n))]


async def test_three_failed_connects_in_two_minutes_switch_the_talk_to_the_glossary_engine(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    hook = HookRecorder()
    fail = _failing_connect(tmp_path)
    factory = Factory(clock, fail, fail, fail, TR_ES)
    ingests = IngestFactory()
    worker = _worker(_room(), _settings(), bus, db, clock, factory, ingests, on_talk_end=hook)

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 12.0)

    assert [c.kind for c in factory.configs] == ["fast", "fast", "fast", "glossary"]
    assert len(ingests.made) == 1  # hot: the source was never reopened
    assert [m.type for m in _track(bus, "en", "f1")].count("talk") == 1  # nor the talk announced again
    assert worker._run.relay.last_seq == 4  # the glossary relay numbers its sessions after Live Translate's
    assert worker.talk is not None and worker.talk.id == "f1" and worker.talk.engine == "glossary"
    stored = await db.get_talk("f1")
    assert stored.engine == "glossary" and stored.status == "live"  # persisted; the talk goes on
    events = await _events(db)
    assert ("warning", "fallback", "fallback: glossary engine") in events
    assert "talk_end" not in [t for _, t, _ in events]
    assert "set" in {m.type for m in _track(bus, "en", "f1")}  # transcribe-live's text
    assert any(m.type == "append" and m.text.startswith("[es] ") for m in _track(bus, "es", "f1"))
    assert not [m for m in _track(bus, "es", "f1") if m.type == "talk" and m.data["talk_id"] is None]

    await worker.stop()
    await worker.drain_hooks()
    assert [talk_id for talk_id, _, _ in hook.ended] == ["f1"]  # ended once, by stop()
    assert not _live_tasks()


async def test_three_hung_sessions_in_two_minutes_switch_to_the_glossary_engine(db) -> None:
    """Sessions that never answer: the stall watchdog reconnects each one
    (15 s of unanswered voice for a new session); the third does it."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, FAKE_LT, fail_after_s=0.0)  # every session hangs from the start
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 40.0)
    assert [c.kind for c in factory.configs] == ["fast", "fast", "fast"]  # two stalls so far
    await run_for(clock, 10.0)

    # the third stall already opened a fourth Live Translate session; then the switch
    assert [c.kind for c in factory.configs] == ["fast", "fast", "fast", "fast", "glossary"]
    assert all(engine.closed for engine in factory.engines[:4])
    assert (await db.get_talk("f1")).engine == "glossary"
    await worker.stop()
    assert not _live_tasks()


async def test_manual_reconnects_never_trigger_the_fallback(db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, FAKE_LT)
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    for _ in range(4):
        await run_for(clock, 1.0)
        await worker.reconnect("manual")
    await run_for(clock, 2.0)

    assert {c.kind for c in factory.configs} == {"fast"} and len(factory.configs) == 5
    assert worker.talk.engine == "fast"
    await worker.stop()


async def test_a_glossary_talk_has_no_fallback(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    fail = _failing_connect(tmp_path)
    factory = Factory(clock, fail)
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es")), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("g1"))
    await run_for(clock, 12.0)

    assert len(factory.configs) >= 4 and {c.kind for c in factory.configs} == {"glossary"}
    assert "fallback" not in [t for _, t, _ in await _events(db)]
    await worker.stop()


def _error(t: float, code: int, retryable: bool) -> dict:
    return {"t": t, "kind": "error", "text": f"error {code}", "meta": {"code": code, "retryable": retryable}}


def _recording(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


async def test_a_halted_live_translate_swaps_to_the_glossary_engine_hot(tmp_path: Path, db, translators) -> None:
    """Rulings 48-49: a non-retryable Live Translate error (here 1008, a model
    that is gone) switches the talk to the glossary engine at the next tick,
    on the fly: same ingest and VAD, continuous audio clock, no new "talk"
    message, nothing captioned twice; the old relay, lane and Translator are
    closed."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    lt = _recording(tmp_path / "lt.jsonl", [
        {"t": 1.0, "kind": "source_delta", "text": " We deploy."},
        {"t": 1.2, "kind": "target_delta", "text": " Desplegamos."},
        {"t": 3.0, "kind": "source_delta", "text": " With Helm."},
        {"t": 3.2, "kind": "target_delta", "text": " Con Helm."},
        _error(6.0, 1008, False),
    ])
    tr = _transcript(tmp_path / "tr.jsonl", [(1.0, "interim", "Then we"), (1.5, "final", "Then we scale.")])
    factory = Factory(clock, lt, tr)
    ingests = IngestFactory()
    worker = RoomWorker(_room(), _settings(), bus, db, clock, factory, ingest_factory=ingests, realtime=False)

    await worker.start(_talk("f1", language="en", targets=("es", "pt"), engine="fast"))
    await run_for(clock, 5.0)
    old_relay, old_lane = worker._run.relay, worker._run.lane
    assert old_lane is not None and len(translators) == 1  # pt: the fast engine's extra language
    await run_for(clock, 1.5)  # the error at ~6 s halts the relay; the next tick swaps

    assert [c.kind for c in factory.configs] == ["fast", "glossary"]
    assert worker._run.relay is not old_relay and worker._run.engine == "glossary"
    assert factory.engines[0].closed and old_lane.pipeline._closed  # the old side is gone
    assert [t.closed for t in translators] == [1, 0]
    await run_for(clock, 3.0)

    assert len(ingests.made) == 1 and not ingests.made[0].closed  # the same source, never reopened
    fast_t, glossary_t = factory.engines[0].sent, factory.engines[1].sent
    # not one chunk lost: what the halted relay held goes to the new one
    assert glossary_t[0] == pytest.approx(fast_t[-1] + 0.1)
    assert all(round(b - a, 3) == 0.1 for a, b in zip(glossary_t, glossary_t[1:]))
    assert factory.configs[1].source_lang == "en"
    await worker.stop()

    assert [t.closed for t in translators] == [1, 1]
    for lang in ("en", "es", "pt"):
        msgs = _track(bus, lang, "f1")
        assert [m.data["talk_id"] for m in msgs if m.type == "talk"] == ["f1", None]  # announced once, ended once
        texts = _closed_texts(msgs)
        assert len(texts) == len(set(texts)), texts  # nothing captioned twice
    assert [t.strip() for t in _closed_texts(_track(bus, "en", "f1"))] == ["We deploy.", "With Helm.", "Then we scale."]
    es = [t.strip() for t in _closed_texts(_track(bus, "es", "f1"))]
    assert es[:2] == ["Desplegamos.", "Con Helm."] and "[es] Then we scale." in es
    for lang in ("en", "es", "pt"):
        saved = [x.text for x in await db.get_segments("f1", lang, "live")]
        assert len(saved) == len(set(saved)), saved
    assert (await db.get_talk("f1")).engine == "glossary"
    events = await _events(db)
    assert ("warning", "fallback", "fallback: glossary engine") in events
    assert [t for _, t, _ in events].count("talk_start") == 1
    assert not _live_tasks()


class NoEngineWritesDb:
    """A Database whose update_talk(engine=...) fails."""

    def __init__(self, db) -> None:
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def update_talk(self, talk_id, **fields):
        if "engine" in fields:
            raise RuntimeError("disk full")
        return await self._db.update_talk(talk_id, **fields)


async def test_a_fallback_that_cannot_save_the_engine_says_so_and_still_switches(
    tmp_path: Path, db, caplog: pytest.LogCaptureFixture
) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [_error(0.5, 1008, False)]), TR_ES)
    worker = _worker(_room(), _settings(), bus, NoEngineWritesDb(db), clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 2.0)

    assert [c.kind for c in factory.configs] == ["fast", "glossary"]
    assert "could not save engine=glossary for f1" in caplog.text
    await worker.stop()


class BrokenTranslator:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("no translator today")


async def test_a_swap_that_cannot_build_the_new_engine_keeps_the_old_one(
    tmp_path: Path, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("glosa.room.Translator", BrokenTranslator)  # the glossary engine's lane needs one
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [_error(0.5, 1008, False)]), TR_ES)
    worker = RoomWorker(_room(), _settings(), bus, db, clock, factory, ingest_factory=IngestFactory(), realtime=False)

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))  # one target: no lane yet
    old_relay = worker._run.relay
    await run_for(clock, 2.0)

    assert [c.kind for c in factory.configs] == ["fast"]
    assert worker._run.relay is old_relay and worker._run.engine == "fast" and worker._run.lane is None
    assert worker.talk.engine == "fast" and (await db.get_talk("f1")).engine == "fast"
    failed = [e for e in await _events(db) if e[1] == "fallback_failed"]
    assert len(failed) == 1 and failed[0][0] == "error" and "no translator today" in failed[0][2]
    assert not worker._run.ticker.done()  # the room keeps ticking (red: halted)
    assert worker.status().state == "red"
    await worker.stop()
    assert not _live_tasks()


async def test_a_rejected_api_key_halts_without_a_fallback(tmp_path: Path, db) -> None:
    """401/403: the glossary engine would be refused too. The room goes red
    with a clear event instead."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [_error(0.0, 401, False)]))
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 3.0)

    assert [c.kind for c in factory.configs] == ["fast"]
    status = worker.status()
    assert status.state == "red" and "401" in status.detail
    events = await _events(db)
    assert [e for e in events if e[1] == "engine_auth"] == [
        ("error", "engine_auth", "Live Translate refused the API key (401): check GEMINI_API_KEY"),
    ]
    assert "fallback" not in [t for _, t, _ in events]
    await worker.stop()


@pytest.mark.parametrize(
    "text",
    [
        "ClientError: 400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key.",
        "ClientError: 400 INVALID_ARGUMENT. {'reason': 'API_KEY_INVALID'}",
    ],
    ids=["api-key", "API_KEY_INVALID"],
)
async def test_an_error_that_names_the_api_key_counts_as_a_refused_key(tmp_path: Path, db, text: str) -> None:
    """Gemini reports a bad key as a 400 (API_KEY_INVALID), not a 401."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    error = {"t": 0.0, "kind": "error", "text": text, "meta": {"code": 400, "retryable": False}}
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [error]))
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 2.0)

    assert [c.kind for c in factory.configs] == ["fast"]
    assert "API key" in worker.status().detail
    assert [(lvl, typ) for lvl, typ, _ in await _events(db) if typ in ("engine_auth", "fallback")] == [
        ("error", "engine_auth"),
    ]
    await worker.stop()


async def test_a_403_permission_denied_falls_back(tmp_path: Path, db) -> None:
    """Ruling 49a: a 403 or "permission denied" without "API key" in it can be
    a preview model the key cannot reach (any more): the glossary engine uses
    other models, so the talk falls back."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    error = {"t": 0.0, "kind": "error", "meta": {"code": 403, "retryable": False},
             "text": "ClientError: 403 PERMISSION_DENIED. Permission denied on model gemini-3.5-live-translate-preview"}
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [error]), TR_ES)
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 2.0)

    assert [c.kind for c in factory.configs] == ["fast", "glossary"]
    types = [t for _, t, _ in await _events(db)]
    assert "fallback" in types and "engine_auth" not in types
    await worker.stop()


async def test_no_credit_never_falls_back(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    payment = {"t": 0.0, "kind": "error", "text": "402", "meta": {"code": 402, "retryable": False, "payment": True}}
    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [payment]))
    worker = _worker(_room(), _settings(), bus, db, clock, factory, IngestFactory())

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 3.0)

    assert [c.kind for c in factory.configs] == ["fast"]
    assert worker.status().state == "red"  # payment blocked
    assert "fallback" not in [t for _, t, _ in await _events(db)]
    await worker.stop()


# ---------------------------------------------------------- Task 14a: station


async def test_a_quiet_station_marks_the_room_red_and_resumes_without_restarting(db) -> None:
    """RoomWorker + the real EmitterIngest + StationHub (Task 14a), with a
    simulated station: audio is pushed straight into the hub's queue, the
    same way glosa/web/station.py's WebSocket handler would after
    re-packing whatever the station sent. Real captions flow through the
    real FakeEngine pipeline; a quiet station turns the room red (the same
    `_source_down` mechanism AudioIngest's terminal error uses) without
    ending the talk, and resumes on its own once audio flows again -- no
    play_file()/start() needed, unlike a dead AudioIngest."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    hub = StationHub(clock)

    def emitter_factory(source_type, source_url, realtime, clock):
        assert source_type == "emitter"
        return EmitterIngest(hub, "r1", clock)

    room = replace(_room(), source_type="emitter", source_url="r1")
    worker = RoomWorker(
        room, _settings(), bus, db, clock, Factory(clock, FAKE_LT),
        ingest_factory=emitter_factory, station_hub=hub, realtime=False,
    )

    await worker.start(None)
    await run_for(clock, 0.5)  # the ticker's first pass: no station audio yet
    status = worker.status()
    assert status.state == "red"
    assert "station disconnected" in status.detail
    assert worker.talk is not None  # unlike AudioIngest's terminal error, the talk stays open

    # The station connects and streams audio: 2.5 s of voice, then silence
    # (already CHUNK_BYTES frames, as the WS handler hands them to the hub).
    for i in range(30):
        voiced = i < 25
        hub.push_audio("r1", TONE if voiced else SILENCE)
        await run_for(clock, 0.1)
    assert worker.status().state != "red"

    await run_for(clock, 8.0)  # let the fixture's deltas play out and segments idle-close

    en = _history(bus, "r1", "en")
    es = _history(bus, "r1", "es")
    for msgs in (en, es):
        kinds = {m.type for m in msgs}
        assert {"append", "close"} <= kinds, [m.type for m in msgs]

    # No push_audio since the voice/silence loop above (last one ~3.0 s in):
    # by now (~11 s in) the station has been quiet well past STATION_TIMEOUT_S.
    status = worker.status()
    assert status.state == "red"
    assert "station disconnected" in status.detail
    assert worker.talk is not None and worker.talk.id == FREE_R1  # same talk throughout

    await worker.stop()
    assert not _live_tasks()


async def test_station_hub_queue_is_bounded_and_survives_a_burst(db) -> None:
    """A burst of station audio queued faster than RoomWorker can consume it
    (e.g. right after a reconnect flushes its 5 s local buffer) drops the
    oldest chunks instead of growing without bound or blocking push_audio."""
    clock = DrivenClock()
    hub = StationHub(clock)
    for _ in range(200):
        hub.push_audio("r1", SILENCE)  # never awaited/consumed concurrently here
    assert hub.queue("r1").qsize() <= 50


async def test_the_hot_engine_swap_keeps_the_station_ingest(tmp_path: Path, db) -> None:
    """Ruling 48 with a room station: the fallback swaps only the engine
    side; the same EmitterIngest goes on feeding the new engine, and a quiet
    station still turns the room red afterwards (the stale check reads the
    run's ingest, which the swap leaves alone)."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    hub = StationHub(clock)
    made = []

    def emitter_factory(source_type, source_url, realtime, clock):
        made.append(EmitterIngest(hub, "r1", clock))
        return made[-1]

    factory = Factory(clock, _recording(tmp_path / "lt.jsonl", [_error(1.0, 1008, False)]), TR_ES)
    room = replace(_room(), source_type="emitter", source_url="r1")
    worker = _worker(room, _settings(), bus, db, clock, factory, emitter_factory, station_hub=hub)

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    for _ in range(30):  # the station streams 3 s; Live Translate halts at 1 s and the room swaps
        hub.push_audio("r1", TONE)
        await run_for(clock, 0.1)

    assert [c.kind for c in factory.configs] == ["fast", "glossary"]
    assert len(made) == 1 and worker._run.ingest is made[0]  # the station's ingest, never reopened
    glossary_t = factory.engines[1].sent
    assert glossary_t and all(round(b - a, 3) == 0.1 for a, b in zip(glossary_t, glossary_t[1:]))
    assert worker.status().state != "red"

    await run_for(clock, STATION_TIMEOUT_S + 1.0)  # the station goes quiet
    status = worker.status()
    assert status.state == "red" and "station disconnected" in status.detail
    assert worker.talk is not None and worker.talk.id == "f1"
    await worker.stop()
    assert not _live_tasks()


# ------------------------------------------------------------------- quality


async def test_no_typesafe_key_means_quality_factory_is_never_called(db) -> None:  # Task 13w
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    calls: list[str] = []

    def factory(api_key: str) -> FakeQualityMeter:
        calls.append(api_key)
        return FakeQualityMeter()

    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory(),
                     quality_factory=factory)

    await worker.start(None)
    await run_for(clock, 10.0)

    assert calls == []  # no TYPESAFE_API_KEY: the lazy builder is never reached
    assert worker.status().quality is None
    await worker.stop()


async def test_quality_feed_scores_fast_track_pairs_and_status_surfaces_the_average(
    db, fake_typesafe_sdk
) -> None:  # Task 13w
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    meter = FakeQualityMeter(result=0.42)
    worker = _worker(_room(), _settings(typesafe_api_key="fake-key"), bus, db, clock, Factory(clock, FAKE_LT),
                     IngestFactory(), quality_factory=lambda key: meter)

    await worker.start(None)  # a free session: en source, es target (default_targets)
    await run_for(clock, 10.0)

    assert meter.calls  # at least one en->es pair, from the fast engine's tracks via _save
    english, spanish = meter.calls[0]
    assert english == "Great starting scenario, for sure."
    assert spanish == "Un gran escenario de inicio, sin duda."
    assert worker.status().quality == pytest.approx(round(meter.avg(), 2))
    assert worker.status().quality == pytest.approx(0.42)
    assert worker.status().state == "yellow"  # below the 0.5 quality threshold; latency/level otherwise green
    await worker.stop()
    assert meter.closed == 0  # I1: stop() between talks keeps the meter's HTTP client
    await worker.aclose()
    assert meter.closed == 1  # only the final shutdown closes it, after any in-flight score


async def test_quality_is_still_measured_after_a_break(db, fake_typesafe_sdk) -> None:  # I1
    """The autopilot stop()s a room between two talks: the next talk's
    quality must still be measured (the meter's HTTP client stays open)."""
    clock = DrivenClock()
    meter = FakeQualityMeter(result=0.8)
    worker = _worker(_room(), _settings(typesafe_api_key="fake-key"), CaptionBus(clock=clock), db, clock,
                     Factory(clock, FAKE_LT, FAKE_LT), IngestFactory(), quality_factory=lambda key: meter)
    await worker.start(_talk("t1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 10.0)
    await worker.stop()  # the break
    assert worker.status().quality is None  # M6: an idle room shows no (previous talk's) quality

    clock.advance(20.0)  # past QUALITY_MIN_INTERVAL_S since t1's last score
    await worker.start(_talk("t2", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 10.0)

    assert worker.status().quality == pytest.approx(0.8)
    await worker.aclose()
    assert meter.closed == 1 and worker.talk is None
    await worker.aclose()  # idempotent
    assert meter.closed == 1


async def test_quality_feed_scores_glossary_lane_pairs_es_to_en(db, fake_typesafe_sdk) -> None:  # Task 13w
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    meter = FakeQualityMeter()
    translate = FakeTranslate()
    worker = _worker(_room(targets=["en"]), _settings(("r1", "es"), typesafe_api_key="fake-key"), bus, db, clock,
                     Factory(clock, TR_ES), IngestFactory(), translate=translate, quality_factory=lambda key: meter)

    await worker.start(_talk("g1"))  # es source, en target, glossary engine (_on_translation)
    await run_for(clock, 62.0)
    await worker.stop()

    assert meter.calls  # the glossary lane's es->en translations were paired and scored
    for english, spanish in meter.calls:
        assert english.startswith("[en] ")  # es->en swaps: english=target text, spanish=source text


async def test_a_key_with_typesafe_sdk_unimportable_warns_once_and_the_room_runs_unmetered(
    db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:  # Task 13w
    """glosa.room must never import glosa.quality (and so typesafe_sdk) at
    module level: with a key set but the 'jev' extra unimportable, the room
    still starts and runs, quality just stays unmeasured."""
    monkeypatch.setitem(__import__("sys").modules, "typesafe_sdk", None)
    monkeypatch.delitem(__import__("sys").modules, "glosa.quality", raising=False)
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(typesafe_api_key="fake-key"), bus, db, clock, Factory(clock, FAKE_LT),
                     IngestFactory())  # default (lazy) quality_factory: glosa.room.build_quality_meter

    with caplog.at_level(logging.WARNING, logger="glosa.room_quality"):
        await worker.start(None)
        await run_for(clock, 10.0)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "glosa.room_quality"]
    assert len(warnings) == 1 and "jev" in warnings[0].message
    assert worker.status().quality is None
    await worker.stop()


async def test_quality_resets_for_each_new_talk(db, fake_typesafe_sdk) -> None:  # Task 13w
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    meter = FakeQualityMeter()
    worker = _worker(_room(), _settings(typesafe_api_key="fake-key"), bus, db, clock,
                     Factory(clock, FAKE_LT, FAKE_LT), IngestFactory(), quality_factory=lambda key: meter)

    await worker.start(_talk("t1", language="en", targets=("es",), engine="fast"))
    assert meter.reset_calls == 1  # a fresh talk starts with an empty window
    await run_for(clock, 10.0)
    assert worker.status().quality is not None

    await worker.start(_talk("t2", language="en", targets=("es",), engine="fast"))  # t1's run ends here
    assert meter.reset_calls == 2
    assert worker.status().quality is None  # the new talk's window starts empty again

    await worker.stop()

# ---- test_file(): Task 14b, Ruling 5 (the admin listen feature) -----------------


async def test_test_file_is_none_for_a_live_source(db) -> None:
    """A room whose source is not a file (here: never started) never offers
    listen -- test_file() is the sole way the admin listen router decides
    ``available``."""
    clock = DrivenClock()
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), IngestFactory())
    assert worker.test_file() is None
    await worker.start(None)  # _room()'s configured source_type is "file" too (fixture default)
    await run_for(clock, 1.0)
    # Ruling 5 explicitly counts a room *configured* with source_type "file"
    # as test mode, same as an admin's "Probar con audio" -- so this one
    # does report a file.
    info = worker.test_file()
    assert info is not None and info[0] == "fake://r1"
    await worker.stop()


async def test_test_file_offset_grows_with_the_clock(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory())
    clip = _clip(tmp_path)

    await worker.play_file(clip)
    await run_for(clock, 3.0)
    first = worker.test_file()
    assert first is not None
    path, offset = first
    assert path == clip
    assert offset == pytest.approx(3.0, abs=0.15)

    await run_for(clock, 2.0)
    path2, offset2 = worker.test_file()
    assert path2 == clip
    assert offset2 > offset
    await worker.stop()


async def test_test_file_offset_resets_when_a_new_file_replaces_the_old_one(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory())

    await worker.start(None)  # _room() is a "file" source too
    await run_for(clock, 4.0)
    await worker.play_file(_clip(tmp_path))  # the operator plays something else mid-talk
    await run_for(clock, 1.0)

    _, offset = worker.test_file()
    assert offset < 2.0  # the new file's own clock, not the room's cumulative one
    await worker.stop()


async def test_test_file_is_none_once_the_room_goes_idle(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    worker = _worker(_room(), _settings(), bus, db, clock, Factory(clock, FAKE_LT), IngestFactory())

    await worker.play_file(_clip(tmp_path))
    await run_for(clock, 1.0)
    assert worker.test_file() is not None
    await worker.stop()
    assert worker.test_file() is None


# --------------------------------------------------------------------- silence gate (task-19)


async def test_silence_gate_stops_and_resumes_sending_through_the_relay(tmp_path: Path, db) -> None:
    """Integration-level check of glosa/audio/gate.py's ordering guarantees
    (unit tested directly in tests/audio/test_gate.py), through
    RoomWorker._feed -> SessionRelay.feed -> engine.send_audio."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _steady_fixture(tmp_path))
    lead_s, silence_s, gate_after_s = 2.0, 10.0, 5.0
    worker = _worker(
        _room(), _settings(silence_gate_s=gate_after_s), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=lead_s, silence_s=silence_s),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, lead_s + silence_s + 2.0)  # + a slice of the resumed voice
    await worker.stop()

    ts = factory.engines[0].sent
    assert ts == sorted(ts) and len(ts) == len(set(ts))  # strictly in order, never duplicated
    assert ts[:20] == [round(i * 0.1, 2) for i in range(20)]  # the lead voice: sent untouched

    # EnergyVad flips in_speech False on the 4th silence chunk (pause_ms=400 ms
    # of *cumulative* silence including that chunk's own 100 ms), i.e. 300 ms
    # into the silence; the gate then sends chunks for gate_after_s more.
    last_before_gap = round(lead_s + 0.3 + gate_after_s - 0.1, 2)
    first_after_gap = round(lead_s + silence_s - 1.0, 2)  # exactly PREROLL_S before voice returns
    gap_i = ts.index(last_before_gap) + 1
    assert ts[gap_i] == first_after_gap  # nothing sent while gated: the gap is silent, not lost
    # the pre-roll (10 chunks) then the live chunk resume with regular 0.1 s spacing
    assert ts[gap_i : gap_i + 11] == [round(first_after_gap + 0.1 * k, 2) for k in range(11)]


async def test_status_marker_and_gated_s_while_gated(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=3.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=12.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 5.0)  # gate closes at ~5.4 s in: not gated yet
    status = worker.status()
    assert "silence gate" not in status.detail
    assert status.gated_s == 0.0

    await run_for(clock, 0.8)  # now clearly gated, but still inside the 1 s pre-roll window
    status = worker.status()
    assert "silence gate: paused" in status.detail
    assert status.gated_s == 0.0  # nothing dropped for good yet -- it's all still in the pre-roll

    await run_for(clock, 5.0)  # well past the pre-roll now
    status = worker.status()
    assert "silence gate: paused" in status.detail
    assert status.gated_s > 0.0

    await worker.stop()


async def test_no_watchdog_or_fallback_incident_while_gated(tmp_path: Path, db) -> None:
    """(a)/(b): a long gated silence must not trip the relay's stall
    watchdog (only checked in SessionRelay._poll(), reached from feed(),
    which the gate stops calling) or FlapDetector's fallback. The session
    here never dies; see the tests below for one that does while gated."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=3.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=30.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 2.0 + 30.0)  # long past both the gate threshold and stall_timeout (8 s default)

    relay = worker._run.relay
    assert relay.stats["reconnects"] == 0
    assert relay.stats["rotations"] == 0
    assert worker.talk.engine == "fast"  # no fallback: FlapDetector never saw an incident
    assert worker.status().state != "red"
    await worker.stop()
    assert not _live_tasks()


async def test_silence_gate_disabled_with_zero(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=0.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=30.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 2.0 + 30.0)
    assert "silence gate" not in worker.status().detail
    await worker.stop()

    ts = factory.engines[0].sent
    assert ts == [round(i * 0.1, 2) for i in range(len(ts))]  # every chunk sent, nothing ever withheld


async def test_glossary_end_utterance_fires_before_the_gate_closes(tmp_path: Path, db) -> None:
    """(c): the glossary engine's client-side VAD end_utterance() -- called
    on the room's own VAD "pause" event, ~400 ms after speech ends -- is
    long done by the time GATE_AFTER_S is reached; gating must not delay,
    skip or duplicate it."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=3.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=12.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="glossary"))
    await run_for(clock, 2.5)  # past the VAD's pause (~2.4 s in), well before the 3 s gate
    assert factory.engines[0].end_utterances == 1

    await run_for(clock, 10.0)  # deep into the now-gated silence
    assert factory.engines[0].end_utterances == 1  # not called again, not skipped

    await worker.stop()


# ------------------------------------------- silence gate: relay events while gated (task-19 fix 1)


def _records(path: Path, records: list[dict]) -> Path:
    """A FakeEngine recording from raw records (for meta, e.g. go_away's time_left_s)."""
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def _dies_at(tmp_path: Path, t: float) -> Path:
    """Answers once, then the session closes on its own at ``t`` s after connect."""
    return _script(tmp_path / f"dies_{t:g}.jsonl", [(0.3, "source_final", "hello"), (t, "closed", "")])


def _failed_connect(tmp_path: Path) -> Path:
    return _records(tmp_path / "fail.jsonl", [{"t": 0.0, "kind": "error", "text": "boom", "meta": {"code": 503}}])


def _preroll_first(sent: list[float], voice_at: float) -> None:
    """The replacement session got the 1 s pre-roll first, then the live
    voice, in order with no gap -- nothing pruned while it was connecting."""
    expected = [round(voice_at - 1.0 + 0.1 * k, 2) for k in range(15)]
    assert sent[:15] == expected


async def test_session_death_while_gated_is_not_an_incident(tmp_path: Path, db) -> None:
    """Ruling 58: a session that dies during a gated silence is reconnected
    by the relay's event-driven path (not frozen by the gate) -- housekeeping,
    not an incident: no "recent reconnect" yellow once voice returns, nothing
    for FlapDetector, no fallback, and the pre-roll reaches the new session."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _dies_at(tmp_path, 12.0), _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=5.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=20.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 15.0)
    run = worker._run
    assert run.gate.gated and run.relay.stats["reconnects"] == 1  # died while gated
    await run_for(clock, 22.0 + 3.0 - 15.0)  # voice is back at 22 s

    assert not run.gate.gated
    assert run.last_reconnect_at is None
    assert "recent reconnect" not in worker.status().detail
    assert len(run.flaps._at) == 0
    assert worker.talk.engine == "fast" and not run.falling_back
    _preroll_first(factory.engines[1].sent, 22.0)
    await worker.stop()


async def test_go_away_and_failed_reconnect_while_gated_are_not_incidents(tmp_path: Path, db) -> None:
    """A go_away while gated starts a standby connect (event-driven) that
    fails, then the old session is killed: one error and one reconnect, both
    while gated -- neither may count toward the fast->glossary fallback."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    first = _records(
        tmp_path / "go_away.jsonl",
        [
            {"t": 0.3, "kind": "source_final", "text": "hello"},
            {"t": 10.0, "kind": "go_away", "meta": {"time_left_s": 5.0}},
            {"t": 16.0, "kind": "closed"},
        ],
    )
    factory = Factory(clock, first, _failed_connect(tmp_path), _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=5.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=20.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 20.0)
    run = worker._run
    stats = run.relay.stats
    assert run.gate.gated
    assert sum(stats["errors"].values()) == 1 and stats["reconnects"] == 1
    assert len(run.flaps._at) == 0
    await run_for(clock, 22.0 + 3.0 - 20.0)

    assert run.last_reconnect_at is None
    assert "recent reconnect" not in worker.status().detail
    assert len(run.flaps._at) == 0
    assert worker.talk.engine == "fast" and not run.falling_back
    _preroll_first(factory.engines[2].sent, 22.0)
    await worker.stop()


async def test_preroll_survives_a_slow_reconnect_after_the_gate_opens(tmp_path: Path, db) -> None:
    """The session died while gated; when voice returns the first connect
    attempt fails and the retry waits out a backoff (> buffer_s): the pre-roll
    and first live chunks are held for it, not pruned to the last 2 s."""
    clock = DrivenClock()
    bus = CaptionBus(clock=clock)
    factory = Factory(clock, _dies_at(tmp_path, 12.0), _failed_connect(tmp_path), _steady_fixture(tmp_path))
    worker = _worker(
        _room(), _settings(silence_gate_s=5.0), bus, db, clock, factory,
        SilenceIngestFactory(lead_s=2.0, silence_s=20.0),
    )

    await worker.start(_talk("f1", language="en", targets=("es",), engine="fast"))
    await run_for(clock, 22.0 + 6.0)

    assert len(factory.engines) == 3
    assert factory.engines[1].sent == []  # the failed attempt took nothing
    _preroll_first(factory.engines[2].sent, 22.0)
    ts = factory.engines[2].sent
    assert ts == [round(ts[0] + 0.1 * k, 2) for k in range(len(ts))]  # contiguous, no duplicate
    await worker.stop()
