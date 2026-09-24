"""AudioIngest: ffmpeg command building and the restart supervisor.

Test 1.2 checks build_ffmpeg_cmd's shape (including the youtube -> resolve_youtube
path, mocked). Test 1.3 runs a real (tiny) ffmpeg process over a short WAV
fixture and checks the chunking/timing contract. Test 1.4 checks the
supervisor's backoff/retry/terminal-error behavior using a command that
always fails ("false") and a FakeClock, so no real waiting happens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glosa.audio import ingest as ingest_module
from glosa.audio.ingest import AudioIngest, build_ffmpeg_cmd
from glosa.clock import FakeClock

FIXTURE_WAV = Path(__file__).parent.parent / "fixtures" / "short_clip.wav"


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
