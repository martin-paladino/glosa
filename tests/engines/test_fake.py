"""FakeEngine: replays a T0.5-style JSONL recording, paced by the Clock."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from glosa.clock import FakeClock
from glosa.engines.fake import FakeEngine
from glosa.models import AudioChunk, EngineConfig, EngineEvent

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_lt.jsonl"
DELTA_KINDS = {"source_delta", "target_delta"}


def _cfg(path: Path | str | None = FIXTURE) -> EngineConfig:
    return EngineConfig(
        kind="fake",
        source_lang="en",
        target_lang="es",
        fixture_path=str(path) if path is not None else None,
    )


def _fixture_deltas() -> list[dict]:
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["kind"] in DELTA_KINDS]


async def _collect(engine: FakeEngine) -> list[EngineEvent]:
    return [ev async for ev in engine.events()]


async def test_replays_fixture_in_order_at_times_relative_to_connect() -> None:
    clock = FakeClock(start=100.0)
    engine = FakeEngine(_cfg(), clock)
    clock.advance(5.0)  # connect() happens at t=105, not at the clock's origin
    await engine.connect()

    events = await _collect(engine)

    expected = _fixture_deltas()
    assert len(expected) == 10  # sanity: the fixture is the real 4.5-8 s excerpt
    deltas = [ev for ev in events if ev.kind in DELTA_KINDS]
    assert [(ev.kind, ev.text, ev.lang) for ev in deltas] == [
        (r["kind"], r["text"], r["meta"]["lang"]) for r in expected
    ]
    assert [ev.t_recv for ev in deltas] == pytest.approx([105.0 + r["t"] for r in expected])
    # usage / session_resumption_update records are not EngineEvents: skipped.
    assert [ev.kind for ev in events] == [ev.kind for ev in deltas] + ["closed"]
    last_record_t = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[-1])["t"]
    assert clock.now() == pytest.approx(105.0 + last_record_t)  # paced to the very last record


async def test_send_audio_and_end_utterance_do_nothing() -> None:
    clock = FakeClock()
    engine = FakeEngine(_cfg(), clock)
    await engine.connect()
    await engine.send_audio(AudioChunk(pcm=b"\x00" * 3200, t=0.0))
    await engine.end_utterance()

    events = await _collect(engine)

    assert len([ev for ev in events if ev.kind in DELTA_KINDS]) == 10


async def test_fail_after_s_stops_emitting_and_hangs_until_closed() -> None:
    clock = FakeClock()
    engine = FakeEngine(_cfg(), clock, fail_after_s=6.0)
    await engine.connect()
    got: list[EngineEvent] = []

    async def consume() -> None:
        async for ev in engine.events():
            got.append(ev)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    assert not consumer.done()  # a hang: the stream neither emits nor ends
    expected = [r for r in _fixture_deltas() if r["t"] <= 6.0]
    assert [ev.text for ev in got] == [r["text"] for r in expected]
    assert clock.now() == pytest.approx(expected[-1]["t"])

    await engine.close()
    await asyncio.wait_for(consumer, timeout=1.0)
    assert [ev.kind for ev in got[len(expected):]] == ["closed"]


async def test_replay_yields_to_other_tasks_between_records() -> None:
    # FakeClock.sleep never suspends; without an explicit yield a consumer would
    # drain the whole recording in one go and starve e.g. the relay's feed loop.
    engine = FakeEngine(_cfg(), FakeClock())
    await engine.connect()
    consumer = asyncio.create_task(_collect(engine))

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not consumer.done()
    await asyncio.wait_for(consumer, timeout=1.0)


async def test_close_ends_the_replay_early() -> None:
    clock = FakeClock()
    engine = FakeEngine(_cfg(), clock)
    await engine.connect()
    stream = engine.events()

    first = await anext(stream)
    await engine.close()
    rest = [ev async for ev in stream]

    assert first.kind == "source_delta"
    assert [ev.kind for ev in rest] == ["closed"]


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


async def test_maps_go_away_error_and_final_records(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "rec.jsonl",
        [
            {"t": 1.0, "kind": "target_final", "text": " fin.", "raw_type": "x", "meta": {"lang": "es", "finished": True}},
            {"t": 2.0, "kind": "go_away", "text": "", "raw_type": "go_away", "meta": {"time_left_raw": "50s"}},
            {"t": 3.0, "kind": "error", "text": "quota", "raw_type": "ClientError", "meta": {"code": 429, "retryable": True}},
            {"t": 5.0, "kind": "source_delta", "text": "after error", "raw_type": "x", "meta": {"lang": "en"}},
        ],
    )
    engine = FakeEngine(_cfg(path), FakeClock())
    await engine.connect()

    events = await _collect(engine)

    # An error ends the session, as with LiveTranslateEngine: error, then closed.
    assert [(ev.kind, ev.text, ev.lang, ev.meta, ev.t_recv) for ev in events] == [
        ("target_delta", " fin.", "es", {}, 1.0),
        ("go_away", "", None, {"time_left_s": 50.0}, 2.0),
        ("error", "quota", None, {"code": 429, "retryable": True}, 3.0),
        ("closed", "", None, {}, 3.0),
    ]


async def test_replays_interim_source_deltas_and_finals(tmp_path: Path) -> None:
    # A transcribe-live recording: interims rewrite the open segment, the final closes it.
    path = _write_jsonl(
        tmp_path / "tr.jsonl",
        [
            {"t": 1.0, "kind": "source_delta", "text": "Por cierto,", "meta": {"lang": "es", "interim": True}},
            {"t": 1.5, "kind": "source_delta", "text": "Por cierto, cuando", "meta": {"lang": "es", "interim": True}},
            {"t": 2.0, "kind": "source_final", "text": "Por cierto, cuando.", "meta": {"lang": "es"}},
            {"t": 2.1, "kind": "audio_stream_end", "text": "", "meta": {"audio_t": 1.9}},
        ],
    )
    engine = FakeEngine(EngineConfig(kind="fake", source_lang="es", target_lang=None, fixture_path=str(path)), FakeClock())
    await engine.connect()

    events = await _collect(engine)

    assert [(ev.kind, ev.text, ev.lang, ev.meta) for ev in events] == [
        ("source_delta", "Por cierto,", "es", {"interim": True}),
        ("source_delta", "Por cierto, cuando", "es", {"interim": True}),
        ("source_final", "Por cierto, cuando.", "es", {}),
        ("closed", "", None, {}),  # client-side records (audio_stream_end, ...) are skipped
    ]


async def test_a_closed_record_ends_the_stream(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "rec.jsonl",
        [
            {"t": 1.0, "kind": "closed", "text": "", "raw_type": "probe_finished"},
            {"t": 2.0, "kind": "source_delta", "text": "after close", "raw_type": "x", "meta": {"lang": "en"}},
        ],
    )
    engine = FakeEngine(_cfg(path), FakeClock())
    await engine.connect()

    assert [(ev.kind, ev.t_recv) for ev in await _collect(engine)] == [("closed", 1.0)]


def test_requires_a_fixture_path() -> None:
    with pytest.raises(ValueError):
        FakeEngine(_cfg(None), FakeClock())
