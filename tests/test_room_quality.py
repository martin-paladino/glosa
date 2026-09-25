"""QualityFeed (Task 13w): pairs closed source/translation segments and
feeds glosa.quality.QualityMeter without blocking the room -- pure unit
tests against a fake meter, no network, no RoomWorker.

build_quality_meter()'s ImportError path is exercised with
sys.modules["typesafe_sdk"] = None (Task 13w spec): glosa.quality itself
imports typesafe_sdk at module level, so this is what makes the "extra not
installed" case reproducible regardless of whether the real extra happens
to be installed in the environment running the tests.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from glosa.room_quality import QUALITY_MIN_INTERVAL_S, QualityFeed, build_quality_meter


@pytest.fixture
def fake_typesafe_sdk():
    """A minimal stub of typesafe_sdk, so glosa.quality -- which imports it
    at module level -- can actually be imported in this dev environment
    (the real 'jev' extra is deliberately not installed here; Task 13w
    keeps it optional). No network: these tests only reach into
    glosa.quality for the pure find_matching_source(); QualityMeter itself
    is always a FakeMeter here, never the real thing.
    """
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


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeMeter:
    """A MeterLike whose score() is controlled by the test: it waits on
    `gate` (if set) so tests can hold a score "in flight" on purpose."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.added: list[float | None] = []
        self.reset_calls = 0
        self.closed = False
        self.gate: asyncio.Event | None = None
        self.result: float | None = 0.9

    async def score(self, src: str, tgt: str) -> float | None:
        self.calls.append((src, tgt))
        if self.gate is not None:
            await self.gate.wait()
        return self.result

    def add(self, p: float | None) -> None:
        self.added.append(p)

    def avg(self) -> float | None:
        scored = [p for p in self.added if p is not None]
        return sum(scored) / len(scored) if scored else None

    def reset(self) -> None:
        self.reset_calls += 1

    async def aclose(self) -> None:
        self.closed = True


def _spawner(tasks: list[asyncio.Task]):
    def spawn(coro, name: str) -> None:
        tasks.append(asyncio.ensure_future(coro))

    return spawn


def _feed(meter: FakeMeter, clock: FakeClock, tasks: list[asyncio.Task]) -> QualityFeed:
    return QualityFeed(meter, now=clock.now, spawn=_spawner(tasks))


# ------------------------------------------------------------------- pairing


async def test_on_target_pairs_with_the_overlapping_source_and_scores_en_to_es(fake_typesafe_sdk) -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("This is the English source sentence.", 10.0, 12.0)
    feed.on_target("en", "es", "Esta es la traduccion al espanol.", 10.2, 12.3)
    await asyncio.gather(*tasks)

    assert meter.calls == [("This is the English source sentence.", "Esta es la traduccion al espanol.")]
    assert meter.added == [0.9]


async def test_es_to_en_mapping_swaps_the_texts(fake_typesafe_sdk) -> None:
    """english=source, spanish=translation for en->es; es->en swaps which
    text lands in which state key (the Jev question is fixed to
    english/spanish, not source/target)."""
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("Esta es la oracion en espanol fuente.", 5.0, 7.0)
    feed.on_target("es", "en", "This is the English translation sentence.", 5.1, 7.2)
    await asyncio.gather(*tasks)

    assert meter.calls == [("This is the English translation sentence.", "Esta es la oracion en espanol fuente.")]


@pytest.mark.parametrize("source_lang,target_lang", [("en", "fr"), ("fr", "en"), ("es", "pt"), ("en", "en")])
async def test_other_language_pairs_are_never_scored(source_lang: str, target_lang: str) -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("A source sentence with enough words in it.", 0.0, 2.0)
    feed.on_target(source_lang, target_lang, "A target sentence with enough words too.", 0.1, 2.1)
    await asyncio.gather(*tasks)

    assert meter.calls == []
    assert meter.added == []


async def test_no_matching_source_is_not_scored() -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_target("en", "es", "Nothing to pair this translation against.", 10.0, 12.0)
    await asyncio.gather(*tasks)

    assert meter.calls == []


@pytest.mark.parametrize(
    "source_text,target_text",
    [("Too short", "This translation has plenty of words in it"), ("This source has plenty of words in it", "Muy corto")],
)
async def test_short_pairs_are_skipped(fake_typesafe_sdk, source_text: str, target_text: str) -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source(source_text, 0.0, 2.0)
    feed.on_target("en", "es", target_text, 0.1, 2.1)
    await asyncio.gather(*tasks)

    assert meter.calls == []


async def test_source_window_keeps_only_the_last_20_segments() -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    for i in range(25):
        feed.on_source(f"Source segment number {i} with words.", float(i * 10), float(i * 10 + 2))
    assert len(feed._sources) == 20
    assert feed._sources[0].text == "Source segment number 5 with words."


# -------------------------------------------------------------- rate limits


async def test_one_score_in_flight_per_room_drops_pairs_meanwhile(fake_typesafe_sdk) -> None:
    meter = FakeMeter()
    meter.gate = asyncio.Event()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("First English source sentence right here.", 0.0, 2.0)
    feed.on_target("en", "es", "Primera traduccion al espanol aqui.", 0.1, 2.1)
    await asyncio.sleep(0)  # let the background score() start and reach the gate

    # A second pair arrives while the first is still in flight: dropped, not queued.
    feed.on_source("Second English source sentence right here.", 5.0, 7.0)
    feed.on_target("en", "es", "Segunda traduccion al espanol aqui.", 5.1, 7.1)
    await asyncio.sleep(0)

    assert len(meter.calls) == 1  # the second call never happened

    meter.gate.set()
    await asyncio.gather(*tasks)
    assert len(meter.calls) == 1
    assert meter.added == [0.9]


async def test_interval_drops_a_pair_started_too_soon_then_allows_one_after_15s(fake_typesafe_sdk) -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("First English source sentence right here.", 0.0, 2.0)
    feed.on_target("en", "es", "Primera traduccion al espanol aqui.", 0.1, 2.1)
    await asyncio.gather(*tasks)
    tasks.clear()
    assert len(meter.calls) == 1

    clock.advance(QUALITY_MIN_INTERVAL_S - 1.0)
    feed.on_source("Second English source sentence right here.", 20.0, 22.0)
    feed.on_target("en", "es", "Segunda traduccion al espanol aqui.", 20.1, 22.1)
    await asyncio.gather(*tasks)
    tasks.clear()
    assert len(meter.calls) == 1  # too soon: dropped

    clock.advance(1.0)  # now exactly QUALITY_MIN_INTERVAL_S since the first started
    feed.on_source("Third English source sentence right here.", 40.0, 42.0)
    feed.on_target("en", "es", "Tercera traduccion al espanol aqui.", 40.1, 42.1)
    await asyncio.gather(*tasks)
    assert len(meter.calls) == 2


# ------------------------------------------------------------------- reset


async def test_reset_clears_the_source_window_and_the_meter(fake_typesafe_sdk) -> None:
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("An old talk's English source sentence.", 0.0, 2.0)
    feed.on_target("en", "es", "La vieja traduccion de esta charla.", 0.1, 2.1)
    await asyncio.gather(*tasks)
    tasks.clear()
    assert feed.avg() == 0.9

    feed.reset()
    assert meter.reset_calls == 1
    assert len(feed._sources) == 0

    # The new talk's target has nothing to pair with until a new source arrives.
    feed.on_target("en", "es", "Traduccion de la charla nueva aqui.", 0.1, 2.1)
    await asyncio.gather(*tasks)
    assert len(meter.calls) == 1  # unchanged: no source in the new (reset) window to pair with


async def test_reset_during_an_in_flight_score_does_not_leak_into_the_new_talk(fake_typesafe_sdk) -> None:
    """A score started for the outgoing talk must never land in the new
    talk's freshly-reset window, even if it's still awaiting the meter when
    reset() runs (review fix round 1, finding 1: a talk-boundary race)."""
    meter = FakeMeter()
    meter.gate = asyncio.Event()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("An old talk's English source sentence.", 0.0, 2.0)
    feed.on_target("en", "es", "La vieja traduccion de esta charla.", 0.1, 2.1)
    await asyncio.sleep(0)  # let the background score() start and reach the gate

    feed.reset()  # the new talk starts while the old talk's score is still in flight

    meter.gate.set()  # only now does the stale score resolve
    await asyncio.gather(*tasks)

    assert meter.added == []  # never added: the score belonged to a talk that's gone
    assert feed.avg() is None


async def test_avg_and_aclose_proxy_the_meter() -> None:
    meter = FakeMeter()
    clock = FakeClock()
    feed = _feed(meter, clock, [])
    assert feed.avg() is None
    await feed.aclose()
    assert meter.closed is True


async def test_aclose_is_idempotent() -> None:
    """stop() may run twice (already-stopped is a no-op): a second aclose()
    must not touch the meter again."""
    meter = FakeMeter()
    clock = FakeClock()
    feed = _feed(meter, clock, [])
    await feed.aclose()
    meter.closed = False  # prove a second real aclose() would have flipped it back
    await feed.aclose()
    assert meter.closed is False


# --------------------------------------------------------- build_quality_meter


async def test_on_target_guards_its_lazy_import_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_target()'s lazy `from glosa.quality import find_matching_source`
    (review fix round 1, minor finding) must never raise into the room, even
    in the (unreachable in production) case where glosa.quality is present
    in sys.modules but doesn't have the name -- e.g. a broken/partial module
    from a custom quality_factory."""
    monkeypatch.setitem(sys.modules, "glosa.quality", types.ModuleType("glosa.quality"))
    meter = FakeMeter()
    clock = FakeClock()
    tasks: list[asyncio.Task] = []
    feed = _feed(meter, clock, tasks)

    feed.on_source("A source sentence with enough words in it.", 0.0, 2.0)
    feed.on_target("en", "es", "A target sentence with enough words too.", 0.1, 2.1)  # must not raise
    await asyncio.gather(*tasks)

    assert meter.calls == []


def test_build_quality_meter_warns_once_and_returns_none_when_the_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "typesafe_sdk", None)
    with caplog.at_level(logging.WARNING, logger="glosa.room_quality"):
        meter = build_quality_meter("some-key")
    assert meter is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "jev" in warnings[0].message
