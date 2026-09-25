"""LocalParakeetEngine (Task 16, engine_mode "local"): event shapes match
transcribe.py's contract, using fake transcribe callables injected -- no
mlx/parakeet-mlx needed to run these tests (Linux-safe)."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from glosa.clock import FakeClock
from glosa.config import ConfigError
from glosa.engines.local import LocalParakeetEngine, _SharedParakeetModel, reset_shared_model
from glosa.models import AudioChunk, EngineConfig, EngineEvent

CHUNK = AudioChunk(pcm=b"\x00\x01" * 1600, t=0.0)  # 100 ms of PCM16 mono @ 16 kHz -- content is irrelevant here


@pytest.fixture(autouse=True)
def _fresh_shared_model():
    """Each test gets its own process-wide singleton (its own injected fake)."""
    reset_shared_model()
    yield
    reset_shared_model()


def _cfg(source_lang: str = "en") -> EngineConfig:
    return EngineConfig(kind="glossary", source_lang=source_lang, target_lang=None)


async def _collect_until_closed(engine: LocalParakeetEngine, n: int) -> list[EngineEvent]:
    """The next ``n`` events off ``engine.events()`` (it never ends on its
    own until close(), so a plain full-consume would hang)."""
    got: list[EngineEvent] = []
    stream = engine.events()
    for _ in range(n):
        got.append(await anext(stream))
    return got


async def test_interim_then_final_then_closed_match_transcribe_contract() -> None:
    """source_delta interim (meta.interim=True, replaces), source_final
    (closes), closed -- meta['usd'] == 0 on every event, as transcribe.py's
    module docstring specifies for this engine (Task 16 brief)."""
    calls: list[bytes] = []

    def fake_transcribe(pcm: bytes) -> str:
        calls.append(pcm)
        return f"transcript of {len(pcm)} bytes"

    clock = FakeClock()
    engine = LocalParakeetEngine(_cfg("en"), clock, transcribe=fake_transcribe, interim_period_s=1.0)
    await engine.connect()
    stream = engine.events()  # ONE stream, like a real consumer (glosa/room.py's _consume)

    await engine.send_audio(CHUNK)  # first chunk always triggers an interim attempt
    interim = await anext(stream)
    assert interim.kind == "source_delta"
    assert interim.meta == {"interim": True, "usd": 0.0}
    assert interim.lang == "en"
    assert interim.text == f"transcript of {len(CHUNK.pcm)} bytes"

    await engine.end_utterance()
    final = await anext(stream)
    assert final.kind == "source_final"
    assert final.meta == {"usd": 0.0}
    assert final.lang == "en"
    assert final.text == f"transcript of {len(CHUNK.pcm)} bytes"

    await engine.close()
    closed = await anext(stream)
    assert closed.kind == "closed"
    assert closed.meta == {"usd": 0.0}

    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_end_utterance_with_no_audio_emits_nothing() -> None:
    """Unlike transcribe-live's empty final (server-driven), an utterance
    boundary with nothing buffered since the last one is simply not an
    event: there is nothing to transcribe."""
    clock = FakeClock()
    engine = LocalParakeetEngine(_cfg(), clock, transcribe=lambda pcm: "unused")
    await engine.connect()

    await engine.end_utterance()
    await engine.close()

    events = [ev async for ev in engine.events()]
    assert [ev.kind for ev in events] == ["closed"]


async def test_repeated_interim_text_is_not_re_emitted() -> None:
    clock = FakeClock()
    engine = LocalParakeetEngine(_cfg(), clock, transcribe=lambda pcm: "same text", interim_period_s=0.5)
    await engine.connect()

    await engine.send_audio(CHUNK)
    first = await anext(engine.events())
    assert first.text == "same text"

    clock.advance(0.5)
    await engine.send_audio(CHUNK)
    await asyncio.sleep(0.05)  # let the (real, background) interim task run
    clock.advance(0.5)
    await engine.send_audio(CHUNK)
    await asyncio.sleep(0.05)

    await engine.end_utterance()
    final = await anext(engine.events())
    assert final.kind == "source_final"  # no repeated interim landed in between
    await engine.close()


async def test_a_stale_interim_after_end_utterance_is_dropped() -> None:
    """A slow interim transcribe still in flight when end_utterance() closes
    the segment (cancelled, and/or its result checked against the
    generation counter that bumped -- see glosa/engines/local.py's module
    docstring) must never reach events() as a source_delta: only the
    (legitimate) source_final for that same audio, once."""
    started = asyncio.Event()
    release = asyncio.Event()

    def slow_transcribe(pcm: bytes) -> str:
        started.set()
        # a blocking wait, safe inside asyncio.to_thread's worker thread
        while not release.is_set():
            time.sleep(0.01)
        return "settled text"

    clock = FakeClock()
    engine = LocalParakeetEngine(_cfg(), clock, transcribe=slow_transcribe, interim_period_s=0.0)
    await engine.connect()

    await engine.send_audio(CHUNK)
    await asyncio.wait_for(started.wait(), timeout=2.0)  # the interim transcribe is now in flight

    await engine.end_utterance()  # closes the segment (the same buffered audio); bumps the generation
    release.set()  # let the blocked transcribe call(s) finish, the interim now stale
    await asyncio.sleep(0.1)

    await engine.close()
    events = [ev async for ev in engine.events()]
    assert [ev.kind for ev in events] == ["source_final", "closed"]  # never a stale source_delta first


async def test_lazy_import_without_the_local_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """glosa.engines.local must import fine even when parakeet-mlx is not
    installed (Linux/Docker); building a REAL engine (no fake injected)
    then raises ConfigError immediately."""
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)  # simulates "not installed"
    clock = FakeClock()
    with pytest.raises(ConfigError, match="local"):
        LocalParakeetEngine(_cfg(), clock)


async def test_building_with_a_fake_transcribe_never_touches_parakeet_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)
    clock = FakeClock()
    engine = LocalParakeetEngine(_cfg(), clock, transcribe=lambda pcm: "ok")  # must not raise
    await engine.connect()
    await engine.close()
    assert [ev.kind async for ev in engine.events()] == ["closed"]


async def test_two_engines_share_one_process_wide_model() -> None:
    """Only the FIRST engine's fake becomes the shared model's: this is the
    point of the singleton -- multiple rooms' engines share one loaded
    model, not one each."""
    clock = FakeClock()
    e1 = LocalParakeetEngine(_cfg(), clock, transcribe=lambda pcm: "from e1")
    e2 = LocalParakeetEngine(_cfg(), clock, transcribe=lambda pcm: "from e2 (never used)")
    await e1.connect()
    await e2.connect()
    assert e1._model is e2._model  # type: ignore[attr-defined]

    await e2.send_audio(CHUNK)
    ev = await anext(e2.events())
    assert ev.text == "from e1"  # e2's own fake was never installed: e1's model won the race


async def test_shared_model_serializes_calls_with_a_lock() -> None:
    """_SharedParakeetModel.transcribe(): never more than one call inside
    the lock at once, even when several are awaited concurrently (the
    'ONE shared model instance ... with a lock/queue' requirement)."""
    def slow(pcm: bytes) -> str:
        time.sleep(0.02)
        return pcm.decode()

    model = _SharedParakeetModel(slow)
    results = await asyncio.gather(
        model.transcribe(b"a"), model.transcribe(b"b"), model.transcribe(b"c")
    )
    assert set(results) == {"a", "b", "c"}
    assert model.max_calls_in_flight == 1
