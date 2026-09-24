"""RoomWorker: one room's pipeline, AudioIngest -> EnergyVad -> SessionRelay ->
CaptionAssembler (one per engine session and language) -> CaptionBus -> SQLite.

Everything runs on simulated time. DrivenClock is a FakeClock whose sleep()
waits for the test to advance time (like the one in tests/engines/
test_relay.py): the audio source, the replaying engines and the worker's
ticker then share one coherent timeline. FakeIngest stands in for ffmpeg with
100 ms chunks of a square wave (voice) or zeros (silence), paced by that clock;
one test feeds the real samples/en_clip.opus through the real AudioIngest.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from glosa.audio.ingest import AudioIngest
from glosa.captions.bus import CaptionBus
from glosa.clock import FakeClock, RealClock
from glosa.config import RelayCfg, RoomCfg, Settings
from glosa.db import init_db
from glosa.engines.fake import FakeEngine
from glosa.models import AudioChunk, CaptionMsg, EngineConfig, Room
from glosa.room import RoomWorker

ROOT = Path(__file__).resolve().parents[1]
FAKE_LT = ROOT / "tests" / "fixtures" / "fake_lt.jsonl"
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
    def __init__(self, cfg: EngineConfig, clock: DrivenClock, usd: float = 0.0) -> None:
        self._closing = asyncio.Event()
        self._usd = usd
        super().__init__(cfg, _SleepUntilClosed(clock, self._closing))

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

    def __init__(self, clock: DrivenClock, *fixtures: Path, usd: float = 0.0) -> None:
        self.clock = clock
        self.fixtures = fixtures
        self.usd = usd
        self.configs: list[EngineConfig] = []

    def __call__(self, cfg: EngineConfig) -> QuickFakeEngine:
        self.configs.append(cfg)
        fixture = self.fixtures[min(len(self.configs), len(self.fixtures)) - 1]
        return QuickFakeEngine(replace(cfg, fixture_path=str(fixture)), self.clock, self.usd)


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


def _settings(*rooms: tuple[str, str], relay: RelayCfg | None = None, timezone: str = "UTC") -> Settings:
    return Settings(
        gemini_api_key="test-key",
        admin_password="test-password",
        timezone=timezone,
        rooms=[
            RoomCfg(id=rid, name=f"Sala {rid}", source_type="file", source_url=f"fake://{rid}", language=lang)
            for rid, lang in (rooms or (("r1", "en"),))
        ],
        relay=relay or RelayCfg(),
    )


def _script(path: Path, records: list[tuple[float, str, str]]) -> Path:
    """Write a FakeEngine recording: (t, kind, text) per record."""
    with path.open("w", encoding="utf-8") as f:
        for t, kind, text in records:
            f.write(json.dumps({"t": t, "kind": kind, "text": text}) + "\n")
    return path


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
        if t is not asyncio.current_task() and t.get_name().startswith(("room-", "relay-"))
    ]


@pytest.fixture
def db(tmp_path: Path):
    database = init_db(tmp_path / "glosa.db")
    yield database
    database.close()


def _worker(room, settings, bus, db, clock, factory, ingests, **kw) -> RoomWorker:
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
    # r2 speaks Spanish: its source deltas feed "es", its translation "en"
    assert "betasrc0" in _segments(r2["es"])[0] and "betatgt0" in _segments(r2["en"])[0]
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
    assert worker.view() == {
        "slug": "r1",
        "name": "Sala r1",
        "langs": ["en", "es"],
        "now": {"talk_id": FREE_R1, "title": "Sesión libre", "speakers": [], "language": "en"},
        "next": None,
    }
    stored = await db.get_talk(FREE_R1)
    assert stored is not None and stored.status == "live" and stored.actual_start is not None

    await worker.stop()

    stored = await db.get_talk(FREE_R1)
    assert stored is not None and stored.status == "done" and stored.actual_end is not None
    assert worker.talk is None and worker.view()["now"] is None
    assert worker.status().state == "idle"
    last = _all(bus, "r1", "es")[-1]
    assert last.type == "talk" and last.data["talk_id"] is None  # the audience goes idle


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


async def test_play_file_rejects_a_missing_file(tmp_path: Path, db) -> None:
    clock = DrivenClock()
    worker = _worker(_room(), _settings(), CaptionBus(clock=clock), db, clock, Factory(clock, FAKE_LT), IngestFactory())

    with pytest.raises(FileNotFoundError):
        await worker.play_file(str(tmp_path / "missing.opus"))
    assert worker.talk is None


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
    await worker.play_file(_clip(tmp_path))  # same talk, new pipeline
    await run_for(clock, 0.5)

    stored = await db.get_talk("a")
    assert stored.targets == ["en", "es"] and stored.title == "Edited while live"
    assert worker.talk.targets == ["en", "es"] and worker.talk.title == "Edited while live"
    assert stored.actual_start == began and stored.status == "live"
    await worker.stop()
