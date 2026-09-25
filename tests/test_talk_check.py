"""Tests for glosa.talk_check (Task 18): TalkChecker asks Jev's Choice
primitive which of scheduled/next/qa/break matches a room's recent
source-language captions; TalkCheckScheduler runs that every
TALK_CHECK_EVERY_S s per room and raises ONE admin suggestion
(TalkMismatchStore) after two mismatches in a row.

No network: TalkChecker's client is always a fake/mock here (an AsyncMock
shaped like typesafe_sdk's AsyncTypeSafeClient, or a RecordingChecker
standing in for TalkChecker itself in the scheduler tests). Real
typesafe_sdk types (Choice's pydantic validation) are exercised directly --
pytest.importorskip mirrors tests/test_quality.py, since typesafe_sdk is an
optional extra (`uv sync --extra jev`).

The lazy-import test (build_talk_checker() with the extra "missing")
mirrors tests/test_room_quality.py: sys.modules["typesafe_sdk"] = None
makes `import typesafe_sdk` raise regardless of whether the extra actually
happens to be installed in the environment running the tests.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

pytest.importorskip("typesafe_sdk")
from typesafe_sdk import Choice  # noqa: E402

from glosa.clock import FakeClock  # noqa: E402
from glosa.db import Segment  # noqa: E402
from glosa.models import Talk  # noqa: E402
from glosa.talk_check import (  # noqa: E402
    CHECK_WINDOW_S,
    MIN_WORDS,
    TALK_CHECK_EVERY_S,
    TalkCheckResult,
    TalkChecker,
    TalkCheckScheduler,
    TalkMismatchStore,
    TalkMismatchSuggestion,
    build_criteria,
    build_talk_checker,
)

MAIN_REPO_ENV = Path("/Users/mpaladino/repos/glosa/.env")


def _seg(text: str, t_start: float, t_end: float, *, lang: str = "en", talk_id: str = "t1") -> Segment:
    return Segment(
        id=0, talk_id=talk_id, room_id="r1", lang=lang, kind="source",
        version="live", text=text, t_start=t_start, t_end=t_end, created_at="",
    )


def _words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def _talk(talk_id: str = "t1", *, title: str = "Charla de prueba", abstract: str = "", speakers=("Ana",)) -> Talk:
    return Talk(
        id=talk_id, room_id="r1", title=title, speakers=list(speakers), language="en", targets=["es"],
        engine="fast", start=None, end=None, abstract=abstract, tags=[], glossary=[], status="live",
        actual_start=None, actual_end=None,
    )


def _mock_client(label: str, confidence: float = 0.9) -> AsyncMock:
    """A mocked TypeSafe client whose system_one() answers one Choice question."""
    client = AsyncMock()
    client.system_one.return_value = SimpleNamespace(
        answers={"talk_check": SimpleNamespace(choice=label, confidence=confidence)}
    )
    return client


# ---------------------------------------------------------------- build_criteria


def test_build_criteria_names_the_current_and_next_talk() -> None:
    current = _talk(title="AI Agents", abstract="About building agents.", speakers=["Ana"])
    nxt = _talk("t2", title="Kubernetes Costs", abstract="FinOps.", speakers=["Beto"])

    criteria = build_criteria(current, nxt)

    assert set(criteria) == {"scheduled", "next", "qa", "break"}
    assert "AI Agents" in criteria["scheduled"] and "Ana" in criteria["scheduled"]
    assert "Kubernetes Costs" in criteria["next"] and "Beto" in criteria["next"]
    assert "qa" not in criteria["scheduled"].lower()  # sanity: not accidentally identical text


def test_build_criteria_with_no_next_talk_is_still_a_valid_question() -> None:
    criteria = build_criteria(_talk(title="Solo talk"), None)
    assert "Solo talk" not in criteria["next"]  # nothing to name
    assert criteria["next"]  # but the criterion still has content


# ---------------------------------------------------------------- TalkChecker


async def test_check_asks_a_choice_question_and_parses_the_answer() -> None:
    client = _mock_client("qa", confidence=0.77)
    checker = TalkChecker(client)

    result = await checker.check("some captions", _talk(), None)

    assert result == TalkCheckResult(label="qa", confidence=0.77)
    kwargs = client.system_one.call_args.kwargs
    assert kwargs["model"] == "jev-latest"
    assert kwargs["state"] == {"captions": "some captions"}
    question = kwargs["questions"]["talk_check"]
    assert isinstance(question, Choice)
    assert set(question.criteria) == {"scheduled", "next", "qa", "break"}


async def test_check_returns_none_and_counts_a_failure_on_error() -> None:
    client = AsyncMock()
    client.system_one.side_effect = RuntimeError("boom")
    checker = TalkChecker(client)

    result = await checker.check("text", _talk(), None)

    assert result is None
    assert checker.failures == 1


async def test_check_returns_none_on_timeout() -> None:
    async def slow(**kwargs):
        await asyncio.sleep(10)

    client = AsyncMock()
    client.system_one.side_effect = slow
    checker = TalkChecker(client)

    result = await checker.check("text", _talk(), None, timeout_s=0.01)

    assert result is None
    assert checker.failures == 1


async def test_aclose_releases_the_client() -> None:
    client = AsyncMock()
    checker = TalkChecker(client)
    await checker.aclose()
    client.aclose.assert_awaited_once()


def test_build_talk_checker_warns_once_and_returns_none_when_the_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    with caplog.at_level(logging.WARNING, logger="glosa.talk_check"):
        checker = build_talk_checker("some-key")
    assert checker is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "jev" in warnings[0].message


def test_build_talk_checker_builds_a_real_checker_with_a_key() -> None:
    checker = build_talk_checker("some-key")
    assert isinstance(checker, TalkChecker)


# ---------------------------------------------------------------- TalkMismatchStore


def test_store_returns_none_until_set_and_clear_removes_just_that_room() -> None:
    store = TalkMismatchStore()
    assert store.get("r1") is None

    store.set("r1", TalkMismatchSuggestion(talk_id="t1", guess="next", next_title="Next Talk"))
    store.set("r2", TalkMismatchSuggestion(talk_id="t9", guess="break", next_title=None))
    assert store.get("r1").guess == "next"

    store.clear("r1")
    assert store.get("r1") is None
    assert store.get("r2").guess == "break"  # a different room is untouched


# ---------------------------------------------------------------- TalkCheckScheduler fakes


class FakeDb:
    def __init__(self) -> None:
        self.segments: dict[tuple[str, str], list[Segment]] = {}
        self.calls: list[tuple[str, str]] = []

    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Segment]:
        assert version == "live"
        self.calls.append((talk_id, lang))
        return list(self.segments.get((talk_id, lang), []))


@dataclass
class _Room:
    id: str = "r1"


class FakeWorker:
    def __init__(self, room_id: str = "r1") -> None:
        self.room = _Room(room_id)
        self.talk: Talk | None = None


class FakeAutopilot:
    def __init__(self, mode: str = "auto", next_talk: Talk | None = None) -> None:
        self._mode = mode
        self._next_talk = next_talk
        self.next_talk_calls = 0

    def mode(self, room_id: str) -> str:
        return self._mode

    async def next_talk(self, room_id: str) -> Talk | None:
        self.next_talk_calls += 1
        return self._next_talk


@dataclass
class RecordingChecker:
    """Records every check() call and plays back a fixed script of
    results/exceptions, one per call, in order. A gate lets a test hold a
    call open (to check "one call in flight")."""

    script: list[Any] = field(default_factory=list)
    gate: asyncio.Event | None = None
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def check(self, text: str, current: Talk, next_talk: Talk | None) -> TalkCheckResult | None:
        self.calls.append((text, current.id, next_talk.id if next_talk is not None else None))
        if self.gate is not None:
            await self.gate.wait()
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return TalkCheckResult(label="scheduled", confidence=0.9)

    async def aclose(self) -> None:
        pass


def _mismatch(label: str) -> TalkCheckResult:
    return TalkCheckResult(label=label, confidence=0.8)


# ---------------------------------------------------------------- TalkCheckScheduler tests


async def test_two_mismatches_in_a_row_raise_one_suggestion() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    autopilot = FakeAutopilot(next_talk=_talk("t2", title="Next Talk"))
    checker = RecordingChecker(script=[_mismatch("next"), _mismatch("next")])
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, autopilot, store, checker, db, FakeClock())

    await scheduler.tick()
    assert store.get("r1") is None  # one mismatch: not yet

    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS * 2), 10.0, 30.0)]
    await scheduler.tick()

    suggestion = store.get("r1")
    assert suggestion is not None
    assert suggestion.talk_id == "t1"
    assert suggestion.guess == "next"
    assert suggestion.next_title == "Next Talk"
    assert len(checker.calls) == 2


async def test_a_single_mismatch_raises_no_suggestion() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker(script=[_mismatch("break")])
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), store, checker, db, FakeClock())

    await scheduler.tick()

    assert store.get("r1") is None


async def test_qa_is_not_a_mismatch() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker(script=[_mismatch("qa")])
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), store, checker, db, FakeClock())

    await scheduler.tick()

    assert store.get("r1") is None


async def test_agreement_clears_a_pending_suggestion() -> None:
    db = FakeDb()
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), store, checker, db, FakeClock())

    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    checker.script = [_mismatch("next"), _mismatch("next")]
    await scheduler.tick()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS * 2), 10.0, 30.0)]
    await scheduler.tick()
    assert store.get("r1") is not None  # armed

    checker.script = [_mismatch("scheduled")]
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS * 3), 30.0, 60.0)]
    await scheduler.tick()

    assert store.get("r1") is None


async def test_manual_room_makes_no_calls() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(mode="manual"), TalkMismatchStore(), checker, db, FakeClock())

    await scheduler.tick()

    assert checker.calls == []
    assert db.calls == []


async def test_manual_room_dismisses_an_already_active_suggestion() -> None:
    db = FakeDb()
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    store = TalkMismatchStore()
    store.set("r1", TalkMismatchSuggestion(talk_id="t1", guess="next", next_title="X"))
    autopilot = FakeAutopilot(mode="manual")
    scheduler = TalkCheckScheduler(worker, autopilot, store, checker, db, FakeClock())

    await scheduler.tick()

    assert store.get("r1") is None


async def test_no_talk_makes_no_calls() -> None:
    db = FakeDb()
    worker = FakeWorker()  # worker.talk is None
    checker = RecordingChecker()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), checker, db, FakeClock())

    await scheduler.tick()

    assert checker.calls == []
    assert db.calls == []


async def test_no_key_makes_no_calls() -> None:
    """checker=None stands in for "no TYPESAFE_API_KEY" (app.py's
    make_talk_checker never builds one) -- the scheduler still exists (like
    every room's) but tick() is a pure no-op, not even a db read."""
    db = FakeDb()
    worker = FakeWorker()
    worker.talk = _talk("t1")
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), None, db, FakeClock())

    await scheduler.tick()

    assert db.calls == []


async def test_fewer_than_40_new_words_is_skipped() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS - 1), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), checker, db, FakeClock())

    await scheduler.tick()

    assert checker.calls == []


async def test_only_the_last_check_window_s_of_captions_is_sent() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [
        _seg(_words(50, "old"), t_start=0.0, t_end=30.0),  # well outside the window
        _seg(_words(50, "recent"), t_start=110.0, t_end=160.0),  # ref=160, window starts at 160-60=100
    ]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), checker, db, FakeClock())

    await scheduler.tick()

    assert len(checker.calls) == 1
    text = checker.calls[0][0]
    assert "old0" not in text and "recent0" in text


async def test_a_new_talk_resets_the_streak_and_dismisses_the_suggestion() -> None:
    db = FakeDb()
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), store, checker, db, FakeClock())

    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    checker.script = [_mismatch("next"), _mismatch("next")]
    await scheduler.tick()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS * 2), 10.0, 30.0)]
    await scheduler.tick()
    assert store.get("r1") is not None

    worker.talk = _talk("t2")
    db.segments[("t2", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    checker.script = [_mismatch("next")]  # one mismatch on the new talk: not enough on its own
    await scheduler.tick()

    assert store.get("r1") is None  # the old talk's suggestion is gone
    assert len(checker.calls) == 3


async def test_one_call_in_flight_per_room_a_slow_tick_is_not_overlapped() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    gate = asyncio.Event()
    checker = RecordingChecker(gate=gate)
    worker = FakeWorker()
    worker.talk = _talk("t1")
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), checker, db, FakeClock())

    first = asyncio.ensure_future(scheduler.tick())
    await asyncio.sleep(0)  # let the first tick start and reach the gate
    second = asyncio.ensure_future(scheduler.tick())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert len(checker.calls) == 1  # the second tick found the room busy and skipped


async def test_run_defaults_to_talk_check_every_30s() -> None:
    assert TALK_CHECK_EVERY_S == 30.0


async def test_a_failure_is_logged_not_raised_and_does_not_advance_the_marker() -> None:
    db = FakeDb()
    db.segments[("t1", "en")] = [_seg(_words(MIN_WORDS), 0.0, 10.0)]
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker(script=[RuntimeError("boom")])
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), store, checker, db, FakeClock())

    await scheduler.tick()  # must not raise

    assert store.get("r1") is None
    # the marker never advanced, so the same words count as "new" again
    checker.script = [TalkCheckResult(label="scheduled", confidence=0.9)]
    await scheduler.tick()
    assert len(checker.calls) == 2


async def test_no_segments_yet_is_a_noop() -> None:
    db = FakeDb()
    worker = FakeWorker()
    worker.talk = _talk("t1")
    checker = RecordingChecker()
    scheduler = TalkCheckScheduler(worker, FakeAutopilot(), TalkMismatchStore(), checker, db, FakeClock())

    await scheduler.tick()

    assert checker.calls == []


# ---------------------------------------------------------------- Live check (task-18-brief-v2.md, <= 3 real calls)


def _build_source_text_from_bench(path: Path, lang: str, window_s: float = CHECK_WINDOW_S) -> tuple[str, str]:
    """Reconstructs the closed source-language segments from a bench
    recording (append/close CaptionMsg-shaped JSONL, one per line) the same
    way the room does -- mirrors tests/test_summary.py's
    _build_es_text_from_bench, but for the ORIGINAL (source) language track,
    not a translation. Returns (window text, title)."""
    import json

    open_segs: dict[int, str] = {}
    closed: list[tuple[float, str]] = []
    title = ""
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("lang") != lang:
                continue
            if rec.get("type") == "talk":
                title = (rec.get("data") or {}).get("title") or title
            elif rec.get("type") == "append":
                seg = rec["seg"]
                open_segs[seg] = open_segs.get(seg, "") + (rec.get("text") or "")
            elif rec.get("type") == "close":
                seg = rec["seg"]
                text = open_segs.pop(seg, "")
                if text.strip():
                    closed.append((rec["t"], text.strip()))
    if not closed:
        return "", title
    ref = closed[-1][0]
    window = [text for t, text in closed if t >= ref - window_s]
    return " ".join(window), title


BENCH_DIR = Path("/Users/mpaladino/repos/glosa/bench/raw")


@pytest.mark.live
async def test_live_choice_picks_scheduled_when_the_agenda_agrees() -> None:
    """Real Jev call #1: the bench's en_clip captions are genuinely about
    "AI agents"; current = that topic, next = an unrelated hobby talk. The
    obvious answer is "scheduled"."""
    env = dotenv_values(MAIN_REPO_ENV) if MAIN_REPO_ENV.exists() else {}
    api_key = env.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY not set in the repo's .env")

    text, _title = _build_source_text_from_bench(BENCH_DIR / "en_clip_fast.jsonl", "en")
    assert text, "no closed en segments found in the bench recording"
    current = _talk("live1", title="AI Agents in Practice",
                     abstract="Building the infrastructure and requirements AI agents need.")
    nxt = _talk("live1-next", title="Watercolor Painting for Beginners", abstract="A relaxing hobby workshop.")

    checker = build_talk_checker(api_key)
    assert checker is not None, "the 'jev' extra must be installed (uv sync --extra jev)"
    try:
        result = await asyncio.wait_for(checker.check(text, current, nxt), timeout=15.0)
    finally:
        await checker.aclose()

    assert result is not None
    print(f"\nLIVE talk check (agrees): label={result.label!r} confidence={result.confidence:.2f}")
    assert result.label == "scheduled", result


@pytest.mark.live
async def test_live_choice_picks_next_when_the_next_talk_already_started() -> None:
    """Real Jev call #2: same idea, reversed -- the bench's es_clip captions
    are genuinely about Kubernetes cloud costs; "scheduled" is made an
    unrelated hobby talk and "next" is given that real topic. The obvious
    answer is "next"."""
    env = dotenv_values(MAIN_REPO_ENV) if MAIN_REPO_ENV.exists() else {}
    api_key = env.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY not set in the repo's .env")

    text, _title = _build_source_text_from_bench(BENCH_DIR / "es_clip_fast.jsonl", "es")
    assert text, "no closed es segments found in the bench recording"
    current = _talk("live2", title="Acuarela para principiantes", abstract="Un taller de pintura relajante.")
    nxt = _talk("live2-next", title="Kubernetes: entendiendo la factura de la nube",
                abstract="Cómo mapear el gasto de Cloud a namespaces, teams y workloads.")

    checker = build_talk_checker(api_key)
    assert checker is not None, "the 'jev' extra must be installed (uv sync --extra jev)"
    try:
        result = await asyncio.wait_for(checker.check(text, current, nxt), timeout=15.0)
    finally:
        await checker.aclose()

    assert result is not None
    print(f"\nLIVE talk check (mismatch): label={result.label!r} confidence={result.confidence:.2f}")
    assert result.label == "next", result
