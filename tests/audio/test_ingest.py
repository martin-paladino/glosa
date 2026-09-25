"""AudioIngest: ffmpeg command building and the restart supervisor.

Test 1.2 checks build_ffmpeg_cmd's shape (including the youtube -> resolve_youtube
path, mocked). Test 1.3 runs a real (tiny) ffmpeg process over a short WAV
fixture and checks the chunking/timing contract. Test 1.4 checks the
supervisor's backoff/retry/terminal-error behavior using a command that
always fails ("false") and a FakeClock, so no real waiting happens.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from glosa.audio import ingest as ingest_module
from glosa.audio.ingest import (
    CHUNK_BYTES,
    STATION_QUEUE_CHUNKS,
    STATION_TIMEOUT_S,
    AudioIngest,
    EmitterIngest,
    StationHub,
    build_ffmpeg_cmd,
)
from glosa.clock import FakeClock

FIXTURE_WAV = Path(__file__).parent.parent / "fixtures" / "short_clip.wav"
PCM = bytes(CHUNK_BYTES)  # one silent 100 ms frame, already server-side-repacked


def test_build_ffmpeg_cmd_realtime_flag() -> None:
    cmd_rt = build_ffmpeg_cmd("file", "in.wav", realtime=True)
    cmd_no_rt = build_ffmpeg_cmd("file", "in.wav", realtime=False)

    assert "-re" in cmd_rt
    assert "-re" not in cmd_no_rt
    assert cmd_rt == [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-i",
        "in.wav",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "-",
    ]


def test_build_ffmpeg_cmd_youtube_resolves_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_resolve(url: str) -> str:
        calls.append(url)
        return "https://resolved.example/stream.m3u8"

    monkeypatch.setattr(ingest_module, "resolve_youtube", fake_resolve)

    cmd = build_ffmpeg_cmd("youtube", "https://www.youtube.com/watch?v=abc", realtime=True)

    assert calls == ["https://www.youtube.com/watch?v=abc"]
    assert "-i" in cmd
    assert cmd[cmd.index("-i") + 1] == "https://resolved.example/stream.m3u8"


@pytest.mark.asyncio
async def test_ingest_chunks_short_wav_file() -> None:
    ingest = AudioIngest(
        source_type="file",
        source_url=str(FIXTURE_WAV),
        realtime=False,
        clock=FakeClock(),
    )

    chunks = [c async for c in ingest.chunks()]

    assert len(chunks) >= 20
    assert all(len(c.pcm) == 3200 for c in chunks)

    expected_t = 0.0
    for c in chunks:
        assert abs(c.t - expected_t) < 1e-6
        expected_t = round(expected_t + 0.1, 2)

    assert ingest.restarts == 0
    assert ingest.last_error is None


@pytest.mark.asyncio
async def test_ingest_supervisor_retries_then_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd_calls: list[tuple] = []

    def fake_build_cmd(source_type: str, source_url: str, realtime: bool) -> list[str]:
        cmd_calls.append((source_type, source_url, realtime))
        return ["false"]

    monkeypatch.setattr(ingest_module, "build_ffmpeg_cmd", fake_build_cmd)

    clock = FakeClock()
    ingest = AudioIngest(
        source_type="file",
        source_url="unused",
        realtime=False,
        clock=clock,
    )

    chunks = [c async for c in ingest.chunks()]

    assert chunks == []
    assert ingest.restarts == 5
    assert ingest.last_error is not None
    assert clock.now() == pytest.approx(1 + 2 + 4 + 8 + 16)
    # build_ffmpeg_cmd (and thus resolve_youtube, for a youtube source) is
    # called fresh on every attempt: 1 initial + 5 retries.
    assert len(cmd_calls) == 6


@pytest.mark.asyncio
async def test_ingest_youtube_resolution_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolve_youtube failure must flow into the same backoff/retry path
    as an ffmpeg start/exit failure, not crash chunks() outright."""
    calls: list[str] = []

    def flaky_resolve(url: str) -> str:
        calls.append(url)
        if len(calls) <= 2:
            raise RuntimeError("yt-dlp: transient resolution failure")
        return str(FIXTURE_WAV)

    monkeypatch.setattr(ingest_module, "resolve_youtube", flaky_resolve)

    clock = FakeClock()
    ingest = AudioIngest(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=example",
        realtime=False,
        clock=clock,
    )

    chunks = await _take(ingest, 20)  # a live source never ends by itself (I6): take what we need

    assert len(calls) == 3  # 2 failures + 1 success
    assert ingest.restarts == 2
    assert clock.now() == pytest.approx(1 + 2)
    assert len(chunks) == 20
    assert all(len(c.pcm) == 3200 for c in chunks)


@pytest.mark.asyncio
async def test_ingest_youtube_resolution_always_fails_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def always_fail(url: str) -> str:
        calls.append(url)
        raise RuntimeError("yt-dlp: could not resolve")

    monkeypatch.setattr(ingest_module, "resolve_youtube", always_fail)

    clock = FakeClock()
    ingest = AudioIngest(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=example",
        realtime=False,
        clock=clock,
    )

    chunks = [c async for c in ingest.chunks()]

    assert chunks == []
    assert len(calls) == 6  # 1 initial + 5 retries
    assert ingest.restarts == 5
    assert ingest.last_error is not None
    assert "could not resolve" in ingest.last_error
    assert clock.now() == pytest.approx(1 + 2 + 4 + 8 + 16)


async def _take(ingest: AudioIngest, n: int) -> list:
    """The first ``n`` chunks, then close the generator (kills ffmpeg)."""
    chunks = []
    async with contextlib.aclosing(ingest.chunks()) as stream:
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) >= n:
                break
    return chunks


def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch, script: str) -> list[tuple]:
    """build_ffmpeg_cmd -> a shell script standing in for ffmpeg."""
    calls: list[tuple] = []

    def fake_build_cmd(source_type: str, source_url: str, realtime: bool) -> list[str]:
        calls.append((source_type, source_url))
        return ["sh", "-c", script]

    monkeypatch.setattr(ingest_module, "build_ffmpeg_cmd", fake_build_cmd)
    return calls


@pytest.mark.parametrize("source_type", ["url", "youtube"])
async def test_a_live_stream_that_ends_cleanly_is_restarted(
    monkeypatch: pytest.MonkeyPatch, source_type: str
) -> None:  # final-review-A I6
    """A url/youtube stream whose connection closes (ffmpeg exits 0) is not
    the end of the talk: restart it with the usual backoff."""
    calls = _fake_ffmpeg(monkeypatch, f"head -c {CHUNK_BYTES * 3} /dev/zero")
    clock = FakeClock()
    ingest = AudioIngest(source_type=source_type, source_url="https://example.test/live", realtime=True, clock=clock)

    chunks = await _take(ingest, 7)

    assert len(chunks) == 7 and len(calls) == 3
    assert ingest.restarts == 2 and ingest.last_error == "the stream ended"
    assert [c.t for c in chunks] == pytest.approx([0.1 * i for i in range(7)])  # one continuous clock


async def test_a_file_that_ends_cleanly_still_just_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_ffmpeg(monkeypatch, f"head -c {CHUNK_BYTES * 3} /dev/zero")
    ingest = AudioIngest(source_type="file", source_url="clip.opus", realtime=True, clock=FakeClock())

    chunks = [c async for c in ingest.chunks()]

    assert len(chunks) == 3 and len(calls) == 1
    assert ingest.restarts == 0 and ingest.last_error is None


async def test_a_live_stream_with_no_audio_for_5_s_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §6: "no llega audio en 5 s" -> restart (a half-open connection
    leaves ffmpeg blocked forever)."""
    monkeypatch.setattr(ingest_module, "NO_AUDIO_TIMEOUT_S", 0.2)
    calls = _fake_ffmpeg(monkeypatch, f"head -c {CHUNK_BYTES * 2} /dev/zero; exec sleep 30")
    ingest = AudioIngest(source_type="url", source_url="https://example.test/live", realtime=True, clock=FakeClock())

    chunks = await asyncio.wait_for(_take(ingest, 4), 5)

    assert len(chunks) == 4 and len(calls) == 2
    assert ingest.restarts == 1 and ingest.last_error == "no audio for 0.2 s"


async def test_a_live_stream_that_never_sends_audio_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_module, "CONNECT_TIMEOUT_S", 0.2)
    calls = _fake_ffmpeg(monkeypatch, "exec sleep 30")
    clock = FakeClock()
    ingest = AudioIngest(source_type="url", source_url="https://example.test/live", realtime=True, clock=clock)

    chunks = await asyncio.wait_for(_take(ingest, 1), 5)

    assert chunks == [] and len(calls) == 6  # 1 + 5 restarts, then the terminal error
    assert ingest.restarts == 5 and ingest.last_error == "no audio for 0.2 s"


async def test_the_restart_budget_starts_over_after_a_recovery(monkeypatch: pytest.MonkeyPatch) -> None:  # M1
    """Five short blips over a day-long session must not add up to a
    terminal "source down": each attempt that delivers audio resets the
    backoff (restarts stays cumulative for RoomWorker)."""
    calls = _fake_ffmpeg(monkeypatch, f"head -c {CHUNK_BYTES} /dev/zero; exit 1")
    clock = FakeClock()
    ingest = AudioIngest(source_type="url", source_url="https://example.test/live", realtime=True, clock=clock)

    chunks = await _take(ingest, 8)  # 8 attempts, each one chunk then a crash

    assert len(chunks) == 8 and len(calls) == 8
    assert ingest.restarts == 7  # cumulative
    assert clock.now() == pytest.approx(7 * 1)  # the backoff starts over at 1 s each time


# --------------------------------------------------------------- StationHub


def test_station_hub_queue_drops_the_oldest_chunk_when_full() -> None:
    hub = StationHub(FakeClock())
    total = STATION_QUEUE_CHUNKS + 3
    for i in range(total):
        hub.push_audio("r1", bytes([i % 256]) * CHUNK_BYTES)

    queue = hub.queue("r1")
    assert queue.qsize() == STATION_QUEUE_CHUNKS
    kept = [queue.get_nowait() for _ in range(STATION_QUEUE_CHUNKS)]
    # the oldest 3 were dropped: what's left starts at chunk #3
    assert kept[0][0] == 3 % 256
    assert kept[-1][0] == (total - 1) % 256


@pytest.mark.asyncio
async def test_station_hub_connect_replaces_the_previous_connection() -> None:
    hub = StationHub(FakeClock())
    old = MagicMock()
    old.close = AsyncMock()

    gen1 = await hub.connect("r1", old)
    new = MagicMock()
    gen2 = await hub.connect("r1", new)

    old.close.assert_awaited_once_with(code=4409)
    assert gen2 != gen1
    assert hub.info("r1").connected is True

    # disconnect() from the *old* (superseded) generation must not clear the
    # still-active connection.
    hub.disconnect("r1", gen1)
    assert hub.info("r1").connected is True
    hub.disconnect("r1", gen2)
    assert hub.info("r1").connected is False


@pytest.mark.asyncio
async def test_station_hub_connect_tolerates_a_close_failure_on_the_old_socket() -> None:
    hub = StationHub(FakeClock())
    old = MagicMock()
    old.close = AsyncMock(side_effect=RuntimeError("already gone"))

    await hub.connect("r1", old)
    await hub.connect("r1", MagicMock())  # must not raise

    assert hub.info("r1").connected is True


@pytest.mark.asyncio
async def test_station_hub_connect_serializes_concurrent_connections() -> None:
    """Fix round 1, review #3: two connects interleaving around ``await
    old.close(...)`` must not leave the loser's socket unclosed (and still
    pushing into the queue). connect() takes a per-room lock around its
    whole body, so a second connect() started while the first is still
    inside its close() call blocks until the first finishes registering,
    instead of both reading the same (stale) `old` and racing."""
    hub = StationHub(FakeClock())
    a = MagicMock()
    await hub.connect("r1", a)  # gen 1: the connection that B and C will race to replace

    entered_close = asyncio.Event()
    gate = asyncio.Event()

    async def slow_close(code=1000):
        entered_close.set()
        await gate.wait()

    a.close = AsyncMock(side_effect=slow_close)  # B's connect() will be stuck closing `a`
    b = MagicMock()
    b.close = AsyncMock()
    c = MagicMock()
    c.close = AsyncMock()

    task_b = asyncio.ensure_future(hub.connect("r1", b))
    await entered_close.wait()  # B is now inside `await old.close()` (old == a)

    task_c = asyncio.ensure_future(hub.connect("r1", c))
    await asyncio.sleep(0)
    assert not task_b.done() and not task_c.done()

    gate.set()  # let B finish closing `a` and register itself
    gen_b = await task_b
    gen_c = await task_c

    a.close.assert_awaited_once_with(code=4409)
    # The regression this guards against: without the lock, C could read
    # `old` before B ever wrote its own socket into place, so B's socket
    # would never be closed at all (and would keep pushing audio into the
    # queue forever).
    b.close.assert_awaited_once_with(code=4409)
    assert gen_c == gen_b + 1

    # Only C (the last writer) is registered now.
    hub.disconnect("r1", gen_b)
    assert hub.info("r1").connected is True
    hub.disconnect("r1", gen_c)
    assert hub.info("r1").connected is False


def test_station_hub_hello_and_level_update_info() -> None:
    hub = StationHub(FakeClock())
    assert hub.info("r1") == ingest_module.StationInfo(
        connected=False, device=None, level_db=None, last_audio_age_s=None
    )

    hub.set_hello("r1", "Focusrite Scarlett 2i2")
    hub.set_level("r1", -23.4)

    info = hub.info("r1")
    assert info.device == "Focusrite Scarlett 2i2"
    assert info.level_db == -23.4


@pytest.mark.asyncio
async def test_station_hub_reload_only_reaches_a_connected_station() -> None:
    hub = StationHub(FakeClock())
    assert await hub.reload("r1") is False  # nobody connected

    ws = MagicMock()
    ws.send_json = AsyncMock()
    await hub.connect("r1", ws)

    assert await hub.reload("r1") is True
    ws.send_json.assert_awaited_once_with({"type": "reload"})


def test_station_hub_is_stale_before_first_audio_and_after_a_timeout() -> None:
    clock = FakeClock()
    hub = StationHub(clock)

    assert hub.is_stale("r1") is True  # never sent audio: not "up" yet

    hub.push_audio("r1", PCM)
    assert hub.is_stale("r1") is False
    assert hub.info("r1").last_audio_age_s == 0.0

    clock.advance(STATION_TIMEOUT_S - 0.01)
    assert hub.is_stale("r1") is False

    clock.advance(0.02)
    assert hub.is_stale("r1") is True
    assert hub.info("r1").last_audio_age_s == pytest.approx(STATION_TIMEOUT_S + 0.01)

    hub.push_audio("r1", PCM)  # the station is back
    assert hub.is_stale("r1") is False


def test_station_hub_rooms_are_independent() -> None:
    hub = StationHub(FakeClock())
    hub.push_audio("r1", PCM)
    assert hub.is_stale("r1") is False
    assert hub.is_stale("r2") is True  # r2 never sent anything


# ------------------------------------------------------------- EmitterIngest


@pytest.mark.asyncio
async def test_emitter_ingest_yields_audio_chunks_from_the_hub_with_monotonic_t() -> None:
    hub = StationHub(FakeClock())
    ingest = EmitterIngest(hub, "r1", FakeClock())
    hub.push_audio("r1", b"\x01" * CHUNK_BYTES)
    hub.push_audio("r1", b"\x02" * CHUNK_BYTES)
    hub.push_audio("r1", b"\x03" * CHUNK_BYTES)

    chunks = []
    async with contextlib.aclosing(ingest.chunks()) as stream:
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) == 3:
                break

    assert [c.pcm[:1] for c in chunks] == [b"\x01", b"\x02", b"\x03"]
    assert all(len(c.pcm) == CHUNK_BYTES for c in chunks)
    assert [c.t for c in chunks] == pytest.approx([0.0, 0.1, 0.2])
    assert ingest.restarts == 0
    assert ingest.last_error is None


@pytest.mark.asyncio
async def test_emitter_ingest_waits_for_the_next_chunk_without_ending() -> None:
    """Unlike AudioIngest, chunks() never ends on its own: a quiet station
    just leaves the generator waiting (RoomWorker notices via stale())."""
    hub = StationHub(FakeClock())
    ingest = EmitterIngest(hub, "r1", FakeClock())

    async with contextlib.aclosing(ingest.chunks()) as stream:
        task = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        assert not task.done()
        hub.push_audio("r1", PCM)
        chunk = await task
        assert chunk.pcm == PCM


def test_emitter_ingest_stale_reflects_the_hub() -> None:
    clock = FakeClock()
    hub = StationHub(clock)
    ingest = EmitterIngest(hub, "r1", clock)

    assert ingest.stale() == "station disconnected"
    hub.push_audio("r1", PCM)
    assert ingest.stale() is None
    clock.advance(STATION_TIMEOUT_S)
    assert ingest.stale() == "station disconnected"
