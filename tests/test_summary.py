"""Tests for glosa.summary (Task 17: "¿Qué me perdí?"): SummaryScheduler
builds, every SUMMARY_EVERY_S while a talk is live, a 3-5 bullet summary of
the last SUMMARY_WINDOW_S s of CLOSED caption segments, per language, from
the segments the room already saved (db.get_segments). No network: a fake
db and a fake summarizer stand in for glosa.db.Database and Summarizer.

The genai client itself is faked the same way tests/text/test_translator.py
fakes it (FakeGenAIClient mirrors client.aio.models.generate_content). The
one exception is test_live_summarizes_five_minutes_of_real_captions at the
bottom: it is @pytest.mark.live (excluded by default), spends real API
budget building the bench's own report line.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from glosa.clock import FakeClock
from glosa.config import Settings
from glosa.db import Segment
from glosa.summary import (
    MIN_NEW_WORDS,
    SUMMARY_WINDOW_S,
    FakeSummarizer,
    Summarizer,
    SummaryScheduler,
    SummaryStore,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = _REPO_ROOT / ".env"
BENCH_JSONL = _REPO_ROOT / "bench" / "raw" / "en_clip_fast.jsonl"


def _seg(text: str, t_start: float, t_end: float, *, lang: str = "es", talk_id: str = "t1") -> Segment:
    return Segment(
        id=0, talk_id=talk_id, room_id="r1", lang=lang, kind="translation",
        version="live", text=text, t_start=t_start, t_end=t_end, created_at="",
    )


class FakeDb:
    """Stands in for glosa.db.Database: get_segments(talk_id, lang, "live")
    returns whatever this test seeded per (talk_id, lang)."""

    def __init__(self) -> None:
        self.segments: dict[tuple[str, str], list[Segment]] = {}
        self.calls: list[tuple[str, str]] = []

    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Segment]:
        assert version == "live"
        self.calls.append((talk_id, lang))
        return list(self.segments.get((talk_id, lang), []))


@dataclass
class _Talk:
    id: str
    title: str = "Charla de prueba"


class FakeWorker:
    """Stands in for glosa.room.RoomWorker: the scheduler only ever reads
    .room.id, .talk and .stream_langs(), and calls .add_external_cost()."""

    def __init__(self, room_id: str = "r1", langs: tuple[str, ...] = ("es",)) -> None:
        self.room = _Room(room_id)
        self.talk: _Talk | None = None
        self._langs = langs
        self.costs: list[tuple[str, float, float]] = []

    def stream_langs(self) -> set[str]:
        return set(self._langs)

    def add_external_cost(self, component: str, usd: float, units: float) -> None:
        self.costs.append((component, usd, units))


@dataclass
class _Room:
    id: str


class RecordingSummarizer:
    """Records every summarize() call and plays back a fixed script of
    results/exceptions, one per call, in order. Also lets a test hold a
    call open (via a gate) to check "one call in flight"."""

    def __init__(self, script: list[Any] | None = None, *, gate: asyncio.Event | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[tuple[str, str, str]] = []
        self._gate = gate

    async def summarize(self, text: str, target: str, title: str):
        self.calls.append((text, target, title))
        if self._gate is not None:
            await self._gate.wait()
        if self._script:
            outcome = self._script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        from glosa.summary import SummarizeResult

        return SummarizeResult(bullets=["ok"], usd=0.0)


def _words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


# ---- SummaryStore -----------------------------------------------------------------


def test_store_returns_none_until_set_and_reset_clears_a_room() -> None:
    store = SummaryStore()
    assert store.get("r1", "es") is None

    from glosa.summary import Summary

    store.set("r1", "es", Summary(talk_id="t1", generated_at=1.0, bullets=["a"]))
    store.set("r1", "en", Summary(talk_id="t1", generated_at=1.0, bullets=["b"]))
    store.set("r2", "es", Summary(talk_id="t9", generated_at=1.0, bullets=["c"]))
    assert store.get("r1", "es").bullets == ["a"]

    store.reset("r1")
    assert store.get("r1", "es") is None
    assert store.get("r1", "en") is None
    assert store.get("r2", "es").bullets == ["c"]  # a different room is untouched


# ---- FakeSummarizer -----------------------------------------------------------------


async def test_fake_summarizer_builds_deterministic_bullets_with_no_network() -> None:
    fake = FakeSummarizer()
    result = await fake.summarize(_words(40), "es", "Charla")
    assert 1 <= len(result.bullets) <= 5
    assert result.usd == 0.0
    assert all(bullet.startswith("[es] ") for bullet in result.bullets)

    again = await fake.summarize(_words(40), "es", "Charla")
    assert again.bullets == result.bullets  # deterministic


async def test_fake_summarizer_handles_empty_text() -> None:
    result = await FakeSummarizer().summarize("", "en", "Charla")
    assert result.bullets == []


# ---- SummaryScheduler: window, cost guard, reset, in-flight, failures --------------


async def test_summary_is_built_from_window_text_only_older_segments_excluded() -> None:
    db = FakeDb()
    talk_id = "t1"
    db.segments[(talk_id, "es")] = [
        _seg(_words(50, "old"), t_start=0.0, t_end=99.0, talk_id=talk_id),  # outside the 300s window
        _seg(_words(50, "recent"), t_start=350.0, t_end=400.0, talk_id=talk_id),
    ]
    summarizer = RecordingSummarizer()
    worker = FakeWorker(langs=("es",))
    worker.talk = _Talk(id=talk_id, title="Mi Charla")
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    await scheduler.tick()

    assert len(summarizer.calls) == 1
    text, target, title = summarizer.calls[0]
    assert "old0" not in text and "recent0" in text
    assert target == "es"
    assert title == "Mi Charla"


async def test_summary_covers_one_call_per_stream_lang() -> None:
    db = FakeDb()
    talk_id = "t1"
    for lang in ("en", "es", "pt"):
        db.segments[(talk_id, lang)] = [_seg(_words(50), t_start=0.0, t_end=10.0, talk_id=talk_id, lang=lang)]
    summarizer = RecordingSummarizer()
    worker = FakeWorker(langs=("en", "es", "pt"))
    worker.talk = _Talk(id=talk_id)
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    await scheduler.tick()

    assert {call[1] for call in summarizer.calls} == {"en", "es", "pt"}


async def test_skipped_when_fewer_than_40_new_words_since_the_last_summary() -> None:
    db = FakeDb()
    talk_id = "t1"
    below = MIN_NEW_WORDS - 1
    db.segments[(talk_id, "es")] = [_seg(_words(below), t_start=0.0, t_end=10.0, talk_id=talk_id)]
    summarizer = RecordingSummarizer()
    worker = FakeWorker()
    worker.talk = _Talk(id=talk_id)
    store = SummaryStore()
    scheduler = SummaryScheduler(worker, store, summarizer, db, FakeClock())

    await scheduler.tick()

    assert summarizer.calls == []
    assert store.get("r1", "es") is None


async def test_at_least_40_new_words_triggers_a_summary() -> None:
    db = FakeDb()
    talk_id = "t1"
    db.segments[(talk_id, "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id=talk_id)]
    summarizer = RecordingSummarizer()
    worker = FakeWorker()
    worker.talk = _Talk(id=talk_id)
    store = SummaryStore()
    scheduler = SummaryScheduler(worker, store, summarizer, db, FakeClock())

    await scheduler.tick()

    assert len(summarizer.calls) == 1
    assert store.get("r1", "es") is not None


async def test_second_tick_only_counts_words_new_since_the_last_summary() -> None:
    db = FakeDb()
    talk_id = "t1"
    db.segments[(talk_id, "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id=talk_id)]
    summarizer = RecordingSummarizer()
    worker = FakeWorker()
    worker.talk = _Talk(id=talk_id)
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())
    await scheduler.tick()
    assert len(summarizer.calls) == 1

    # A few more words arrive, but fewer than MIN_NEW_WORDS: still skipped.
    db.segments[(talk_id, "es")].append(_seg(_words(5, "new"), t_start=10.0, t_end=20.0, talk_id=talk_id))
    await scheduler.tick()
    assert len(summarizer.calls) == 1

    # Enough new words arrive: triggers again.
    db.segments[(talk_id, "es")].append(_seg(_words(MIN_NEW_WORDS, "more"), t_start=20.0, t_end=30.0, talk_id=talk_id))
    await scheduler.tick()
    assert len(summarizer.calls) == 2


async def test_resets_per_talk_a_new_talk_starts_with_no_summary() -> None:
    db = FakeDb()
    db.segments[("t1", "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id="t1")]
    summarizer = RecordingSummarizer()
    worker = FakeWorker()
    worker.talk = _Talk(id="t1")
    store = SummaryStore()
    scheduler = SummaryScheduler(worker, store, summarizer, db, FakeClock())
    await scheduler.tick()
    assert store.get("r1", "es") is not None

    # A new talk starts on the same room: the old summary must not survive it.
    worker.talk = _Talk(id="t2")
    db.segments[("t2", "es")] = []  # nothing said yet in the new talk
    await scheduler.tick()

    assert store.get("r1", "es") is None


async def test_one_call_in_flight_per_room_a_slow_tick_is_not_overlapped() -> None:
    db = FakeDb()
    db.segments[("t1", "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id="t1")]
    gate = asyncio.Event()
    summarizer = RecordingSummarizer(gate=gate)
    worker = FakeWorker()
    worker.talk = _Talk(id="t1")
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    first = asyncio.ensure_future(scheduler.tick())
    await asyncio.sleep(0)  # let the first tick start and reach the gate
    second = asyncio.ensure_future(scheduler.tick())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert len(summarizer.calls) == 1  # the second tick found the room busy and skipped


async def test_a_failure_is_logged_not_raised_and_does_not_advance_the_marker() -> None:
    db = FakeDb()
    db.segments[("t1", "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id="t1")]
    summarizer = RecordingSummarizer(script=[RuntimeError("boom")])
    worker = FakeWorker()
    worker.talk = _Talk(id="t1")
    store = SummaryStore()
    scheduler = SummaryScheduler(worker, store, summarizer, db, FakeClock())

    await scheduler.tick()  # must not raise

    assert store.get("r1", "es") is None
    assert worker.costs == []


async def test_idle_room_is_a_noop() -> None:
    db = FakeDb()
    summarizer = RecordingSummarizer()
    worker = FakeWorker()  # worker.talk is None: no talk running
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    await scheduler.tick()

    assert summarizer.calls == []
    assert db.calls == []


async def test_a_successful_call_with_cost_joins_the_room_cost_accounting() -> None:
    from glosa.summary import SummarizeResult

    db = FakeDb()
    db.segments[("t1", "es")] = [_seg(_words(MIN_NEW_WORDS), t_start=0.0, t_end=10.0, talk_id="t1")]
    summarizer = RecordingSummarizer(script=[SummarizeResult(bullets=["a", "b"], usd=0.0007)])
    worker = FakeWorker()
    worker.talk = _Talk(id="t1")
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    await scheduler.tick()

    assert worker.costs == [("summary", 0.0007, 1.0)]


async def test_no_segments_yet_is_a_noop() -> None:
    db = FakeDb()  # no segments seeded for this (talk, lang) at all
    summarizer = RecordingSummarizer()
    worker = FakeWorker()
    worker.talk = _Talk(id="t1")
    scheduler = SummaryScheduler(worker, SummaryStore(), summarizer, db, FakeClock())

    await scheduler.tick()

    assert summarizer.calls == []


# ---- Summarizer (fake genai client, mirrors test_translator.py) -------------------


@dataclass
class _FakeUsage:
    prompt_token_count: int = 500
    candidates_token_count: int = 40
    thoughts_token_count: int = 0


@dataclass
class _FakeResponse:
    text: str
    usage_metadata: _FakeUsage = field(default_factory=_FakeUsage)


class _FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: str, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeAio:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class FakeGenAIClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = _FakeModels(script)
        self.aio = _FakeAio(self.models)


def _summarizer(script: list[Any]) -> tuple[Summarizer, FakeGenAIClient]:
    client = FakeGenAIClient(script)
    summarizer = Summarizer(api_key="test-key", client=client, price_in_per_m=0.30, price_out_per_m=2.50)
    return summarizer, client


async def test_summarizer_parses_bullets_and_computes_cost_from_usage_metadata() -> None:
    summarizer, client = _summarizer([_FakeResponse(text="- Uno\n- Dos\n- Tres")])

    result = await summarizer.summarize("captions...", "es", "Mi Charla")

    assert result.bullets == ["Uno", "Dos", "Tres"]
    assert result.usd == pytest.approx((500 * 0.30 + 40 * 2.50) / 1_000_000)
    assert client.models.calls[0]["model"] == "gemini-3.5-flash-lite"
    assert client.models.calls[0]["contents"] == "captions..."


async def test_summarizer_strips_bullet_markers_and_caps_at_five() -> None:
    summarizer, client = _summarizer([_FakeResponse(text="1. A\n2) B\n* C\n• D\n- E\n- F")])

    result = await summarizer.summarize("captions...", "en", "Talk")

    assert result.bullets == ["A", "B", "C", "D", "E"]


async def test_summarizer_prompt_carries_the_title_and_target_language() -> None:
    summarizer, client = _summarizer([_FakeResponse(text="- ok")])
    await summarizer.summarize("captions...", "pt", "What Kubernetes really costs")

    instruction = client.models.calls[0]["config"].system_instruction
    assert "What Kubernetes really costs" in instruction
    assert "pt" in instruction


async def test_summarizer_thinking_level_is_minimal() -> None:
    summarizer, client = _summarizer([_FakeResponse(text="- ok")])
    await summarizer.summarize("hi", "es", "T")
    assert client.models.calls[0]["config"].thinking_config.thinking_level == "MINIMAL"


async def test_summarizer_raises_on_error_no_retry() -> None:
    from google.genai.errors import ServerError

    summarizer, client = _summarizer([ServerError(503, {"message": "overloaded"})])
    with pytest.raises(ServerError):
        await summarizer.summarize("hi", "es", "T")
    assert len(client.models.calls) == 1  # no retry: the scheduler's own next tick tries again


async def test_summarizer_aclose_releases_the_client_connections() -> None:
    summarizer, client = _summarizer([])
    await summarizer.aclose()
    assert client.aio.closed == 1


# ---- Live check (task-17-brief-v2.md, authorized <= US$0.02) ----------------------


def _build_es_text_from_bench(path: Path, window_s: float = SUMMARY_WINDOW_S) -> tuple[str, float]:
    """Reconstructs the closed "es" (target) segments from a bench recording
    (append/close CaptionMsg-shaped JSONL, one per line) the same way the
    room does: text accumulates per seg on "append", a segment closes on
    "close". Returns (window text, title)."""
    open_segs: dict[int, str] = {}
    closed: list[tuple[float, str]] = []
    title = ""
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("lang") != "es":
                continue
            if rec.get("type") == "talk":
                title = (rec.get("data") or {}).get("title") or title  # keep it: a later "talk end" clears it to None
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


def test_live_test_paths_are_repo_relative_not_a_hardcoded_local_path() -> None:
    # B-I5: these used to be absolute paths under one developer's home
    # directory, which leaked the username/layout into the public repo
    # and broke as soon as a worktree was removed.
    # Resolve them from the repo root instead, like tests/test_quality.py's
    # MAIN_REPO_ENV already does.
    repo_root = Path(__file__).resolve().parents[1]
    assert ENV_PATH == repo_root / ".env"
    assert BENCH_JSONL == repo_root / "bench" / "raw" / "en_clip_fast.jsonl"
    assert BENCH_JSONL.exists()


@pytest.mark.live
async def test_live_summarizes_five_minutes_of_real_captions() -> None:
    """Runs once against the real gemini-3.5-flash-lite API: builds Spanish
    caption text from the bench recording's "es" (translated) track and asks
    for a real summary (task-17-brief-v2.md's Live check, <= US$0.02)."""
    settings = Settings.load(env_path=ENV_PATH)
    summarizer = Summarizer(
        api_key=settings.gemini_api_key,
        price_in_per_m=settings.prices.flash_lite_in_per_m,
        price_out_per_m=settings.prices.flash_lite_out_per_m,
    )
    text, title = _build_es_text_from_bench(BENCH_JSONL)
    assert text, "no closed es segments found in the bench recording"

    result = await summarizer.summarize(text, "es", title)

    assert result.bullets
    assert result.usd < 0.02
    print(f"\nLIVE summary bullets (es, title={title!r}, usd={result.usd:.6f}):")
    for bullet in result.bullets:
        print(f"  - {bullet}")
