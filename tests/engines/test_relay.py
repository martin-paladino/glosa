"""SessionRelay: pause-aligned rotation, stall watchdog, backoff, 402, and the
"no audio chunk ever goes to two sessions" invariant.

Every test runs on simulated time: FakeEngine sessions (spied) replay JSONL
recordings, and a DrivenClock is the only thing that moves time. DrivenClock
is a FakeClock whose sleep() *waits* for the test to advance time instead of
jumping it forward, so several replaying engines and the test's 100 ms feed
loop share one coherent timeline (with a plain FakeClock every replaying
engine would drag the shared clock forward on its own).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from glosa.clock import FakeClock
from glosa.engines.fake import FakeEngine
from glosa.engines.relay import SessionRelay
from glosa.models import AudioChunk, EngineConfig, EngineEvent, VadEvent

REAL_RECORDING = Path(__file__).resolve().parents[2] / "samples" / "fixtures" / "lt_en.jsonl"
CFG = EngineConfig(kind="fake", source_lang="en", target_lang="es")
PCM = b"\x00" * 3200
TEXT_KINDS = {"source_delta", "target_delta", "source_final"}


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

    def set(self, t: float) -> None:
        self.advance(t - self.now())

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now() + s, fut))
        await fut


async def settle(max_rounds: int = 50) -> None:
    """Run the event loop until no task is ready (nothing here waits on I/O or
    timers, only on DrivenClock). Uses CPython's run queue to stop early, and
    falls back to a fixed number of rounds without it."""
    ready = getattr(asyncio.get_running_loop(), "_ready", None)
    for _ in range(max_rounds):
        await asyncio.sleep(0)
        if ready is not None and not ready:
            return


@dataclass
class Plan:
    """How the n-th engine the factory builds behaves."""

    fixture: Path
    fail_after_s: float | None = None
    connect_s: float = 0.0  # simulated handshake time (real one: ~0.45 s)
    connect_hangs: bool = False
    usd: float = 0.0  # meta["usd"] increment put on every event it emits


class SpyEngine(FakeEngine):
    """FakeEngine that records what the relay does to it."""

    def __init__(self, cfg: EngineConfig, plan: Plan, factory: SpyFactory) -> None:
        super().__init__(cfg, factory.clock, fail_after_s=plan.fail_after_s)
        self.plan = plan
        self.factory = factory
        self.sent: list[float] = []
        self.connect_started_at: float | None = None
        self.connected_at: float | None = None
        self.end_utterance_at: list[float] = []
        self.close_calls: list[float] = []
        self.emitted: list[EngineEvent] = []

    def _now(self) -> float:
        return self.factory.clock.now()

    async def connect(self) -> None:
        self.connect_started_at = self._now()
        f = self.factory
        f.in_flight += 1
        f.max_in_flight = max(f.max_in_flight, f.in_flight)
        try:
            if self.plan.connect_hangs:
                await asyncio.Event().wait()  # never set: only the relay's timeout ends it
            if self.plan.connect_s:
                await f.clock.sleep(self.plan.connect_s)
            await super().connect()
            self.connected_at = self._now()
        finally:
            f.in_flight -= 1

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk.t)
        await super().send_audio(chunk)

    async def end_utterance(self) -> None:
        self.end_utterance_at.append(self._now())
        await super().end_utterance()

    async def close(self) -> None:
        self.close_calls.append(self._now())
        await super().close()

    async def events(self):  # type: ignore[override]
        async for ev in super().events():
            if self.plan.usd:
                ev.meta["usd"] = self.plan.usd
            self.emitted.append(ev)
            yield ev


class SpyFactory:
    """EngineFactory: the n-th session gets plans[n] (the last plan repeats)."""

    def __init__(self, clock: DrivenClock, plans: list[Plan]) -> None:
        self.clock = clock
        self.plans = plans
        self.engines: list[SpyEngine] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, cfg: EngineConfig) -> SpyEngine:
        plan = self.plans[min(len(self.engines), len(self.plans) - 1)]
        engine = SpyEngine(replace(cfg, fixture_path=str(plan.fixture)), plan, self)
        self.engines.append(engine)
        return engine


class Harness:
    """A relay fed one 100 ms chunk per tick, with VAD events at given times.

    Each chunk is fed with ``voiced`` = EnergyVad's ``in_speech``: True from a
    ``speech_start`` until its ``pause``, or until an ``"end"`` entry. "end"
    is not a VadEvent: it marks a short utterance ending, for which EnergyVad
    emits nothing.
    """

    def __init__(self, plans: list[Plan], **relay_kw: object) -> None:
        self.clock = DrivenClock()
        self.factory = SpyFactory(self.clock, plans)
        relay_kw.setdefault("jitter", 0.0)
        self.relay = SessionRelay(self.factory, CFG, self.clock, **relay_kw)  # type: ignore[arg-type]
        self.fed: list[float] = []
        self.events: list[EngineEvent] = []
        self.reconnects: list[tuple[float, str]] = []
        self._tick = 0
        self._in_speech = False
        real_reconnect = self.relay.reconnect

        async def spy_reconnect(reason: str) -> None:
            self.reconnects.append((self.clock.now(), reason))
            await real_reconnect(reason)

        self.relay.reconnect = spy_reconnect  # type: ignore[method-assign]

    @property
    def engines(self) -> list[SpyEngine]:
        return self.factory.engines

    async def start(self) -> None:
        await self.relay.start()
        await settle()

    async def run_until(
        self,
        until: float,
        vad: list[tuple[float, str]] = (),  # type: ignore[assignment]
        at: dict[float, Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        vad_at: dict[int, list[str]] = {}
        for t, kind in vad:
            vad_at.setdefault(round(t * 10), []).append(kind)
        actions = {round(t * 10): fn for t, fn in (at or {}).items()}
        while self._tick <= round(until * 10):
            k = self._tick
            t = k / 10
            self.clock.set(t)
            for kind in vad_at.get(k, ()):
                self._in_speech = kind == "speech_start"
                if kind != "end":
                    self.relay.on_vad(VadEvent(kind=kind, t=t))  # type: ignore[arg-type]
            if k in actions:
                await actions[k]()
            await self.relay.feed(AudioChunk(pcm=PCM, t=t), voiced=self._in_speech)
            self.fed.append(t)
            await settle()
            self._tick += 1

    async def stop(self, collect: bool = True) -> None:
        task = asyncio.create_task(self.relay.stop())
        await settle()
        while not task.done():  # wake replaying engines so they can see their close()
            self.clock.advance(1.0)
            await settle()
        await task
        if collect:
            self.events = [ev async for ev in self.relay.events()]


def talking(start: float = 0.0, pauses: list[float] = (), resume_after: float = 0.5):  # type: ignore[assignment]
    """VAD events for someone who talks from `start`, pausing at each `pauses`."""
    events = [(start, "speech_start")]
    for p in pauses:
        events += [(p, "pause"), (p + resume_after, "speech_start")]
    return events


def write_fixture(
    dir_: Path,
    name: str,
    *,
    deltas: list[float] | None = None,
    extra: list[dict] = (),  # type: ignore[assignment]
    until: int = 1200,
) -> Path:
    """A JSONL recording: a source+target delta pair at each time in `deltas`
    (default: every second from 1 s to `until`), plus `extra` records. An
    `error`/`closed` record ends the recording, as a real session end does."""
    times = deltas if deltas is not None else [float(k) for k in range(1, until + 1)]
    rows: list[dict] = []
    for t in times:
        rows.append({"t": t, "kind": "source_delta", "text": f" s{t:g}", "meta": {"lang": "en"}})
        rows.append({"t": t, "kind": "target_delta", "text": f" t{t:g}", "meta": {"lang": "es"}})
    rows += list(extra)
    rows.sort(key=lambda r: r["t"])
    end = next((r for r in rows if r["kind"] in ("error", "closed")), None)
    if end is not None:
        rows = [r for r in rows if r["t"] < end["t"]] + [end]
    path = dir_ / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def error_record(t: float, code: int, retryable: bool, **meta: object) -> dict:
    return {"t": t, "kind": "error", "text": f"error {code}", "meta": {"code": code, "retryable": retryable, **meta}}


def go_away_record(t: float, time_left_s: float) -> dict:
    return {"t": t, "kind": "go_away", "meta": {"time_left_s": time_left_s}}


@pytest.fixture
def steady(tmp_path: Path) -> Path:
    return write_fixture(tmp_path, "steady")


def assert_no_chunk_in_two_sessions(h: Harness) -> None:
    counts = Counter(t for e in h.engines for t in e.sent)
    assert [t for t, n in counts.items() if n > 1] == []


def switch_time(old: SpyEngine) -> float:
    assert len(old.end_utterance_at) == 1
    return old.end_utterance_at[0]


# ------------------------------------------------------------------- rotation


async def test_forced_rotation_at_force_at_without_pauses(steady: Path) -> None:  # 3.1
    h = Harness([Plan(steady)])
    await h.start()
    await h.run_until(600, vad=talking(0))
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(510.0)  # standby opened at standby_at
    assert switch_time(old) == pytest.approx(570.0)
    assert max(old.sent) == pytest.approx(569.9)
    assert min(new.sent) == pytest.approx(570.0)
    assert old.close_calls[0] == pytest.approx(575.0)  # drained 5 s, then closed
    assert h.relay.stats == {"rotations": 1, "reconnects": 0, "errors": {}}
    assert_no_chunk_in_two_sessions(h)


async def test_session_age_counts_from_connect_not_from_activation(steady: Path) -> None:
    # The server's ~10 min start at connect: session 2 connects at 510 s and
    # takes over at 570 s, so its own standby is due at 1020 s, not 1080 s.
    h = Harness([Plan(steady)])
    await h.start()
    await h.run_until(1100, vad=talking(0))
    await h.stop()

    _, second, third = h.engines
    assert switch_time(second) == pytest.approx(510.0 + 570.0)
    assert third.connected_at == pytest.approx(510.0 + 510.0)
    assert h.relay.stats["rotations"] == 2


async def test_rotation_happens_at_first_pause_after_standby_at(steady: Path) -> None:  # 3.2
    h = Harness([Plan(steady)])
    await h.start()
    await h.run_until(560, vad=talking(0, pauses=[100, 300, 505, 520]))
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(510.0)
    assert switch_time(old) == pytest.approx(520.0)  # not at 505, before standby_at
    assert max(old.sent) == pytest.approx(519.9)
    assert min(new.sent) == pytest.approx(520.0)
    assert h.relay.stats["rotations"] == 1


async def test_standby_ready_during_a_pause_switches_right_away(steady: Path) -> None:
    h = Harness([Plan(steady, connect_s=0.45)])
    await h.start()
    # The first session is up at 0.5 s, so its standby is opened at 510.5 s
    # and is up at 511 s. The speaker paused at 509.9 s and is still silent:
    # no reason to wait for another pause.
    await h.run_until(560, vad=[(0, "speech_start"), (509.9, "pause"), (530, "speech_start")])
    await h.stop()

    old, new = h.engines
    assert old.connected_at == pytest.approx(0.5)
    assert new.connect_started_at == pytest.approx(510.5)  # counted from the connect
    assert new.connected_at == pytest.approx(511.0)
    assert switch_time(old) == pytest.approx(511.0)


# --------------------------------------------------- GoAway (Ruling 15)


@pytest.mark.parametrize(
    ("go_away_t", "time_left_s", "force_at", "expected_switch"),
    [
        (540, 50, 570, 570.0),  # what the real session did: force_at comes first
        (540, 50, 600, 575.0),  # without that cover: t_go_away + time_left - 15
        (400, 20, 570, 405.0),  # early GoAway, 20 s of notice
        (400, 18, 570, 400.0),  # < 20 s of notice: now, not at 403 s
    ],
)
async def test_go_away_forces_the_switch_before_the_server_kills_the_session(
    tmp_path: Path, go_away_t: float, time_left_s: float, force_at: float, expected_switch: float
) -> None:
    kill_t = go_away_t + time_left_s + 1  # as measured: GoAway 540 s + 50 s, killed at ~591 s
    first = write_fixture(
        tmp_path,
        "first",
        extra=[go_away_record(go_away_t, time_left_s), error_record(kill_t, 1008, True)],
    )
    h = Harness([Plan(first), Plan(write_fixture(tmp_path, "rest"))], force_at=force_at)
    await h.start()
    await h.run_until(expected_switch + 10, vad=talking(0))  # no pauses at all
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(min(go_away_t, 510.0))  # standby right after the GoAway
    assert switch_time(old) == pytest.approx(expected_switch)
    assert all(t <= expected_switch for t in old.sent)
    assert all(t >= expected_switch for t in new.sent)
    assert old.close_calls[0] == pytest.approx(expected_switch + 5)  # before the kill
    assert h.relay.stats == {"rotations": 1, "reconnects": 0, "errors": {}}


async def test_go_away_switches_at_the_next_pause(tmp_path: Path) -> None:
    first = write_fixture(tmp_path, "first", extra=[go_away_record(400, 50)])
    h = Harness([Plan(first), Plan(write_fixture(tmp_path, "rest"))])
    await h.start()
    await h.run_until(420, vad=talking(0, pauses=[402]))
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(400.0)
    assert switch_time(old) == pytest.approx(402.0)  # the pause, not the 435 s deadline


async def test_real_recording_rotates_before_the_server_kill() -> None:
    """Replays the real 10-min Live Translate session (GoAway at 540.7 s, killed
    with 1008 at 591 s), with the default 8 s watchdog. The recording has a
    real 9.2 s output hiccup right after its first words (5.1 -> 14.3 s,
    speaker talking), and every replayed session repeats it: at connect and
    right after the rotation. The first-output grace (15 s) must absorb both."""
    h = Harness([Plan(REAL_RECORDING)])
    await h.start()
    pauses = [float(p) for p in range(25, 600, 10)]
    await h.run_until(600, vad=talking(0, pauses=pauses))
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(510.0)
    assert switch_time(old) == pytest.approx(515.0)  # first pause after 510 s
    assert old.close_calls[0] == pytest.approx(520.0)  # long before its GoAway and kill
    assert not any(ev.kind == "go_away" for ev in old.emitted)
    assert h.reconnects == []  # no false stall at connect or after the rotation
    assert h.relay.stats == {"rotations": 1, "reconnects": 0, "errors": {}}
    targets = [ev.t_recv for ev in h.events if ev.kind == "target_delta"]
    gaps = [b - a for a, b in itertools.pairwise(targets) if a >= 20]
    assert max(gaps) < 6.0  # captions keep flowing across the hand-over
    assert_no_chunk_in_two_sessions(h)


# ------------------------------------------------------------------ invariant


async def test_no_chunk_is_ever_sent_to_two_sessions(tmp_path: Path) -> None:  # 3.3
    plans = [
        # 1: GoAway at 400 s -> rotation at the 403 s pause
        Plan(write_fixture(tmp_path, "s1", extra=[go_away_record(400, 50), error_record(451, 1008, True)])),
        # 2: hangs 30 s after connecting -> stall reconnect
        Plan(write_fixture(tmp_path, "s2"), fail_after_s=30),
        # 3: dies with a retryable 1011 after 40 s -> 1 s backoff
        Plan(write_fixture(tmp_path, "s3", extra=[error_record(40, 1011, True)]), connect_s=0.45),
        # 4, 5: healthy (5 comes from a manual reconnect at 500 s)
        Plan(write_fixture(tmp_path, "s4"), connect_s=0.45),
    ]
    h = Harness(plans)
    await h.start()
    await h.run_until(520, vad=talking(0, pauses=[403]), at={500.0: lambda: h.relay.reconnect("manual")})
    await h.stop()

    assert len(h.engines) == 5
    counts = Counter(t for e in h.engines for t in e.sent)
    assert [t for t, n in counts.items() if n != 1] == []  # none twice...
    assert sorted(counts) == h.fed  # ...and, with short gaps, none lost either
    for earlier, later in itertools.pairwise(h.engines):
        assert max(earlier.sent) < min(later.sent)  # a clean hand-over, in order
    for e in h.engines:
        assert e.sent == sorted(e.sent)
    assert h.reconnects == [(pytest.approx(438.1), "stall"), (pytest.approx(500.0), "manual")]
    assert h.relay.stats == {"rotations": 1, "reconnects": 3, "errors": {1011: 1}}
    assert h.factory.max_in_flight == 1


# ------------------------------------------------------------------- watchdog


async def test_stall_reconnects_after_stall_timeout_of_unanswered_speech(steady: Path) -> None:  # 3.4
    h = Harness([Plan(steady, fail_after_s=30), Plan(steady)])
    await h.start()
    await h.run_until(80, vad=talking(0))
    await h.stop()

    # Last output at 30.0 s; first unanswered voiced chunk 30.1 s; + 8 s.
    assert h.reconnects == [(pytest.approx(38.1), "stall")]
    assert h.relay.stats["reconnects"] == 1
    hung, fresh = h.engines
    assert max(hung.sent) == pytest.approx(38.0)
    assert min(fresh.sent) == pytest.approx(38.1)
    assert hung.close_calls[0] == pytest.approx(43.1)  # drained, then closed
    assert_no_chunk_in_two_sessions(h)


async def test_watchdog_ignores_silence_and_speech_that_just_resumed(tmp_path: Path) -> None:
    # 1st utterance (0-20 s): output right up to the pause, then 40 s of
    # silence (no output, rightly). 2nd (59.5-80 s): the usual ~1.5 s until
    # the first output, and the last words arrive after the pause.
    deltas = [float(k) for k in range(1, 20)] + [19.8] + [float(k) for k in range(61, 81)] + [81.5]
    quiet = write_fixture(tmp_path, "quiet", deltas=deltas + [1000.0])
    h = Harness([Plan(quiet)])
    await h.start()
    vad = [(0, "speech_start"), (20.0, "pause"), (59.5, "speech_start"), (80.0, "pause")]
    await h.run_until(120, vad=vad)
    await h.stop()

    assert h.reconnects == []
    assert len(h.engines) == 1


async def test_watchdog_fires_on_unanswered_speech_even_across_pauses(tmp_path: Path) -> None:
    steady = write_fixture(tmp_path, "steady")
    h = Harness([Plan(steady, fail_after_s=20), Plan(steady)])
    await h.start()
    # Short utterances with pauses in between; the model says nothing after 20 s.
    await h.run_until(40, vad=talking(0, pauses=[22, 24.5, 27]))
    await h.stop()

    assert h.reconnects == [(pytest.approx(28.1), "stall")]


async def test_answered_short_utterance_does_not_start_a_reconnect_loop(tmp_path: Path) -> None:
    # Review finding 1: a 20 s utterance, then a 0.8 s "Thanks!" at 30 s
    # (EnergyVad emits no pause for it) answered at 31 s, then silence.
    talk = write_fixture(tmp_path, "talk", deltas=[float(k) for k in range(1, 21)] + [31.0, 5000.0])
    silent = write_fixture(tmp_path, "silent", deltas=[5000.0])
    h = Harness([Plan(talk), Plan(silent)])
    await h.start()
    await h.run_until(120, vad=[(0, "speech_start"), (20.4, "pause"), (30.0, "speech_start"), (30.8, "end")])
    await h.stop()

    assert h.reconnects == []
    assert len(h.engines) == 1


async def test_after_a_stall_reconnect_only_new_voice_can_stall_again(tmp_path: Path) -> None:
    mute = write_fixture(tmp_path, "mute", deltas=[5000.0])  # never says a word
    steady = write_fixture(tmp_path, "steady")
    h = Harness([Plan(steady, fail_after_s=20), Plan(mute), Plan(steady)])
    await h.start()
    # Talking until 27 s, then silence until 100 s, then talking again.
    await h.run_until(140, vad=[(0, "speech_start"), (27, "pause"), (100, "speech_start")])
    await h.stop()

    # 20.1 + 8 s -> stall (voice until 26.9 s). The new session never
    # answers, but there is no voice until 100 s: the next stall is 15 s
    # (first-output grace) after that.
    assert h.reconnects == [(pytest.approx(28.1), "stall"), (pytest.approx(115.0), "stall")]


async def test_draining_output_does_not_vouch_for_the_new_session(tmp_path: Path, steady: Path) -> None:
    # Forced switch at 570 s into a session that never answers. The old one
    # keeps talking while it drains (571-574 s), but only the active
    # session's output counts: stall after the 15 s first-output grace.
    mute = write_fixture(tmp_path, "mute", deltas=[5000.0])
    h = Harness([Plan(steady), Plan(mute), Plan(steady)])
    await h.start()
    await h.run_until(590, vad=talking(0))
    await h.stop()

    assert h.reconnects == [(pytest.approx(585.1), "stall")]


# --------------------------------------------------------- failures and backoff


@pytest.fixture
def err429(tmp_path: Path) -> Path:
    return write_fixture(tmp_path, "err429", deltas=[], extra=[error_record(0, 429, True)])


async def test_retryable_connect_errors_back_off_1_2_4_s(err429: Path, steady: Path) -> None:  # 3.5
    h = Harness([Plan(err429)] * 3 + [Plan(steady)])
    await h.start()
    await h.run_until(20, vad=talking(0))
    await h.stop()

    starts = [e.connect_started_at for e in h.engines]
    assert starts == pytest.approx([0.0, 1.0, 3.0, 7.0])  # waits of 1, 2 and 4 s
    assert h.relay.stats["errors"] == {429: 3}
    assert h.relay.stats["reconnects"] == 0  # the first connection, not a reconnect
    assert h.factory.max_in_flight == 1
    assert h.engines[3].sent  # the fourth attempt carries the audio


async def test_backoff_caps_at_16_s_and_resets_after_a_healthy_session(
    tmp_path: Path, err429: Path, steady: Path
) -> None:
    short = write_fixture(tmp_path, "short", extra=[error_record(20, 503, True)])
    h = Harness([Plan(err429)] * 6 + [Plan(short), Plan(err429), Plan(steady)])
    await h.start()
    await h.run_until(140, vad=talking(0))
    await h.stop()

    starts = [e.connect_started_at for e in h.engines]
    waits = [b - a for a, b in itertools.pairwise(starts)]
    # 6 failed connects (1, 2, 4, 8, 16, 16) -> a session that works for 20 s
    # and then dies -> the count starts over: 1 s, then 2 s.
    assert waits[:6] == pytest.approx([1, 2, 4, 8, 16, 16])
    assert starts[7] == pytest.approx(starts[6] + 20 + 1)
    assert waits[7] == pytest.approx(2)


async def test_backoff_adds_jitter(err429: Path, steady: Path) -> None:
    h = Harness([Plan(err429)] * 4 + [Plan(steady)], jitter=0.25, rng=random.Random(7))
    await h.start()
    await h.run_until(40, vad=talking(0))
    await h.stop()

    starts = [e.connect_started_at for e in h.engines]
    waits = [b - a for a, b in itertools.pairwise(starts)]
    for base, wait in zip([1, 2, 4, 8], waits):
        assert base <= wait <= base * 1.25 + 0.1  # + one 100 ms tick of feed granularity
    assert waits != pytest.approx([1, 2, 4, 8])


async def test_payment_required_blocks_and_retries_every_30_s(tmp_path: Path, steady: Path) -> None:  # 3.6
    err402 = write_fixture(
        tmp_path, "err402", deltas=[], extra=[error_record(0, 402, False, payment=True)]
    )
    h = Harness([Plan(err402), Plan(err402), Plan(steady)])
    await h.start()
    await h.run_until(59.9, vad=talking(0))
    assert h.relay.payment_blocked
    await h.run_until(90)
    await h.stop()

    assert [e.connect_started_at for e in h.engines] == pytest.approx([0.0, 30.0, 60.0])
    assert h.relay.stats["errors"] == {402: 2}
    assert not h.relay.payment_blocked  # cleared once the new session produced output


async def test_non_retryable_error_halts_until_a_manual_reconnect(tmp_path: Path, steady: Path) -> None:
    err400 = write_fixture(tmp_path, "err400", deltas=[], extra=[error_record(0, 400, False)])
    h = Harness([Plan(err400), Plan(steady)])
    await h.start()
    await h.run_until(120, vad=talking(0))

    assert len(h.engines) == 1  # no retry loop on a bad request / bad config
    assert h.relay.halted
    await h.run_until(130, at={120.5: lambda: h.relay.reconnect("manual")})
    await h.stop()

    assert len(h.engines) == 2
    assert not h.relay.halted
    assert h.engines[1].sent
    errors = [ev for ev in h.events if ev.kind == "error"]
    assert [(ev.meta["code"], ev.meta["retryable"], ev.meta["session"]) for ev in errors] == [(400, False, 1)]
    assert h.relay.stats["errors"] == {400: 1}


async def test_session_that_ends_by_itself_is_closed_and_replaced(tmp_path: Path, steady: Path) -> None:
    ends = write_fixture(tmp_path, "ends", extra=[{"t": 50, "kind": "closed"}])
    h = Harness([Plan(ends), Plan(steady)])
    await h.start()
    await h.run_until(80, vad=talking(0))
    await h.stop()

    first, second = h.engines
    assert first.close_calls[0] == pytest.approx(50.0)  # close() on seeing `closed`
    assert second.connect_started_at == pytest.approx(51.0)  # 1 s backoff
    assert h.relay.stats["reconnects"] == 1
    assert sorted(first.sent + second.sent) == h.fed  # the gap was buffered, not lost
    assert_no_chunk_in_two_sessions(h)


async def test_failed_connects_are_closed_when_their_closed_event_arrives(err429: Path, steady: Path) -> None:
    h = Harness([Plan(err429)] * 2 + [Plan(steady)])
    await h.start()
    await h.run_until(10, vad=talking(0))
    await h.stop()

    assert h.engines[0].close_calls == pytest.approx([0.0])
    assert h.engines[1].close_calls == pytest.approx([1.0])
    assert all(e.close_calls for e in h.engines)  # stop() closes the rest


async def test_active_death_with_a_ready_standby_switches_to_it_at_once(tmp_path: Path, steady: Path) -> None:
    dies = write_fixture(tmp_path, "dies", extra=[error_record(530, 1011, True)])
    h = Harness([Plan(dies), Plan(steady)])
    await h.start()
    await h.run_until(560, vad=talking(0))  # no pauses: the standby is idle when the active dies
    await h.stop()

    old, new = h.engines
    assert new.connected_at == pytest.approx(510.0)
    assert max(old.sent) == pytest.approx(530.0)
    assert min(new.sent) == pytest.approx(530.1)
    assert len(h.engines) == 2  # no new connection was needed
    assert h.relay.stats == {"rotations": 0, "reconnects": 1, "errors": {1011: 1}}


@pytest.fixture
def err503(tmp_path: Path) -> Path:
    return write_fixture(tmp_path, "err503", deltas=[], extra=[error_record(0, 503, True)])


async def test_failing_standby_during_a_pause_does_not_retire_the_active_session(
    err503: Path, steady: Path
) -> None:
    # Review finding 2: the speaker is in a pause when the standby "connects",
    # but the standby reports 503 on its first read.
    h = Harness([Plan(steady)] + [Plan(err503)] * 3 + [Plan(steady)])
    await h.start()
    await h.run_until(580, vad=[(0, "speech_start"), (509, "pause"), (511.5, "speech_start")])
    await h.stop()

    old, *failed, new = h.engines
    assert [e.connect_started_at for e in failed] == pytest.approx([510.0, 511.0, 513.0])
    assert all(e.sent == [] and e.end_utterance_at == [] for e in failed)
    assert new.connect_started_at == pytest.approx(517.0)
    assert switch_time(old) == pytest.approx(570.0)  # speaking again since 511.5: forced
    assert sorted(t for e in h.engines for t in e.sent) == h.fed  # nothing lost
    assert h.relay.stats == {"rotations": 1, "reconnects": 0, "errors": {503: 3}}


async def test_failing_standby_at_the_force_deadline_does_not_retire_the_active_session(
    err429: Path, steady: Path
) -> None:
    h = Harness([Plan(steady), Plan(err429), Plan(steady)], standby_at=570, force_at=570)
    await h.start()
    await h.run_until(580, vad=talking(0))
    await h.stop()

    old, failed, new = h.engines
    assert failed.sent == []
    assert switch_time(old) == pytest.approx(571.0)  # the retry, 1 s later
    assert max(old.sent) == pytest.approx(571.0)  # routed before the retry came up
    assert min(new.sent) == pytest.approx(571.1)
    assert sorted(t for e in h.engines for t in e.sent) == h.fed


async def test_failed_first_connect_does_not_swallow_buffered_audio(err429: Path, steady: Path) -> None:
    h = Harness([Plan(err429), Plan(steady)])
    await h.relay.start()
    await asyncio.sleep(0)  # connect() has returned; its events() were not read yet
    await h.relay.feed(AudioChunk(pcm=PCM, t=0.0), voiced=True)
    h.fed.append(0.0)
    h._tick = 1
    await settle()
    await h.run_until(5, vad=talking(0.1))
    await h.stop()

    failed, good = h.engines
    assert failed.sent == []
    assert good.connect_started_at == pytest.approx(1.0)
    assert good.sent == h.fed  # 0.0-1.0 s were held, then sent to the good session


# ------------------------------------------------------- connect timeout, in-flight


async def test_hung_connect_times_out_as_a_retryable_error(steady: Path) -> None:
    h = Harness([Plan(steady, connect_hangs=True), Plan(steady)], connect_timeout=10.0)
    await h.start()
    await h.run_until(30, vad=talking(0), at={5.0: lambda: h.relay.reconnect("manual")})
    await h.stop()

    hung, fresh = h.engines  # the manual reconnect at 5 s did not open a second attempt
    assert hung.close_calls == pytest.approx([10.0])
    assert fresh.connect_started_at == pytest.approx(11.0)  # 10 s timeout + 1 s backoff
    assert h.relay.stats["errors"] == {0: 1}
    errors = [ev for ev in h.events if ev.kind == "error"]
    assert [(ev.meta["code"], ev.meta["retryable"]) for ev in errors] == [(0, True)]
    assert h.factory.max_in_flight == 1


async def test_at_most_one_connection_attempt_in_flight(steady: Path) -> None:
    h = Harness([Plan(steady, connect_s=3.0)])
    await h.start()
    spam = {t: (lambda: h.relay.reconnect("manual")) for t in (1.0, 2.0, 20.0, 21.0, 22.0)}
    await h.run_until(40, vad=talking(0), at=spam)
    await h.stop()

    assert h.factory.max_in_flight == 1
    # 0 s: first connect (up at 3 s); 20 s: reconnect -> one attempt, up at 23 s.
    assert [e.connect_started_at for e in h.engines] == pytest.approx([0.0, 20.0])
    assert_no_chunk_in_two_sessions(h)


# ------------------------------------------------------------ end_utterance()


async def test_end_utterance_reaches_only_the_confirmed_active_session(steady: Path) -> None:
    # The glossary engine's hybrid VAD: the room calls relay.end_utterance() on
    # each pause, and only the session that has the audio may get it.
    h = Harness([Plan(steady, connect_s=3.0)])
    await h.start()
    await h.run_until(1, vad=talking(0))
    await h.relay.end_utterance()  # the first session is still connecting
    [first] = h.engines
    assert first.end_utterance_at == []

    await h.run_until(515, vad=talking(0))  # the standby connected at 510 s, up at 513 s
    await h.relay.end_utterance()
    active, standby = h.engines
    assert active is first
    assert active.end_utterance_at == [pytest.approx(515.0)]
    assert standby.end_utterance_at == []

    await h.stop()
    await h.relay.end_utterance()  # stopped: a no-op
    assert active.end_utterance_at == [pytest.approx(515.0)]
    assert standby.end_utterance_at == []


# ---------------------------------------------------------------- event stream


async def test_events_merge_active_and_draining_sessions_only(steady: Path) -> None:
    h = Harness([Plan(steady, usd=0.001)])
    await h.start()
    await h.run_until(540, vad=talking(0, pauses=[520]))
    await h.stop()

    texts = [ev for ev in h.events if ev.kind in TEXT_KINDS]
    first = [ev.t_recv for ev in texts if ev.meta["session"] == 1]
    second = [ev.t_recv for ev in texts if ev.meta["session"] == 2]
    assert max(first) == pytest.approx(524.0)  # drained 520-525 s, then closed
    assert min(second) == pytest.approx(520.0)  # standby output (511-519 s) is not forwarded
    assert h.events[-1].kind == "closed"  # one final `closed`, when the relay stops
    assert [ev.kind for ev in h.events].count("closed") == 1
    emitted = sum(ev.meta.get("usd", 0.0) for e in h.engines for ev in e.emitted)
    forwarded = sum(ev.meta.get("usd", 0.0) for ev in h.events)
    assert forwarded == pytest.approx(emitted)  # usd increments are never dropped


async def test_events_can_be_consumed_while_feeding(steady: Path) -> None:
    h = Harness([Plan(steady)])
    got: list[EngineEvent] = []

    async def consume() -> None:
        async for ev in h.relay.events():
            got.append(ev)

    consumer = asyncio.create_task(consume())
    await h.start()
    await h.run_until(300, vad=talking(0))
    assert got and got[-1].t_recv == pytest.approx(300.0)  # delivered as they happen
    await h.run_until(530, vad=talking(300.5, pauses=[520]))
    await h.stop(collect=False)
    await asyncio.wait_for(consumer, 1.0)  # the stream ends after stop()

    assert got[-1].kind == "closed"
    assert {ev.meta.get("session") for ev in got if ev.kind in TEXT_KINDS} == {1, 2}


# --------------------------------------------------------------------- stop()


async def test_stop_closes_active_standby_and_draining_sessions(tmp_path: Path, steady: Path) -> None:
    # 1 is draining (switched at 520 s), 2 is active and got a GoAway at 521 s,
    # 3 is its standby.
    second = write_fixture(tmp_path, "second", extra=[go_away_record(11, 50)])
    h = Harness([Plan(steady), Plan(second), Plan(steady)])
    await h.start()
    await h.run_until(522, vad=talking(0, pauses=[520]))
    draining, active, standby = h.engines
    assert standby.connected_at == pytest.approx(521.0)
    await h.stop()

    assert draining.close_calls[0] == pytest.approx(522.0)  # before its 525 s drain end
    assert active.close_calls and standby.close_calls
    assert h.events[-1].kind == "closed"


async def test_stop_closes_a_session_that_is_still_connecting(steady: Path) -> None:
    h = Harness([Plan(steady), Plan(steady, connect_hangs=True)])
    await h.start()
    await h.run_until(512, vad=talking(0))
    active, connecting = h.engines
    assert connecting.connect_started_at == pytest.approx(510.0)
    await h.stop()

    assert connecting.connected_at is None
    assert connecting.close_calls and active.close_calls
    assert h.factory.in_flight == 0
