"""EnergyVad: energy-based VAD with an adaptive noise floor.

Synthetic PCM (silence / tone / noise) is generated in-process and fed to
EnergyVad.process() in 100 ms (3200-byte) chunks, matching the chunk size
AudioIngest produces, to check speech_start/pause timing per the spec:
end-of-speech = 400 ms of silence with at least 1.5 s of preceding voice.
"""

from __future__ import annotations

import math
import random
import struct

from glosa.audio.vad import EnergyVad
from glosa.models import AudioChunk

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100 ms @ 16kHz mono s16le
CHUNK_S = 0.1


def _tone_pcm(freq: float, amplitude: int, seconds: float) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    samples = [int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _silence_pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def _noise_pcm(amplitude: int, seconds: float, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    n = int(SAMPLE_RATE * seconds)
    return struct.pack(f"<{n}h", *(rng.randint(-amplitude, amplitude) for _ in range(n)))


def _mix(*parts: bytes) -> bytes:
    """Sample-wise sum of same-length s16le PCM streams, clipped to int16."""
    unpacked = [struct.unpack(f"<{len(p) // 2}h", p) for p in parts]
    n = len(unpacked[0])
    assert all(len(u) == n for u in unpacked)
    out = []
    for i in range(n):
        total = sum(u[i] for u in unpacked)
        out.append(max(-32768, min(32767, total)))
    return struct.pack(f"<{n}h", *out)


def _to_chunks(pcm: bytes) -> list[AudioChunk]:
    chunks = []
    t = 0.0
    for i in range(0, len(pcm) - (len(pcm) % CHUNK_BYTES), CHUNK_BYTES):
        chunks.append(AudioChunk(pcm=pcm[i : i + CHUNK_BYTES], t=round(t, 2)))
        t += CHUNK_S
    return chunks


def _run(vad: EnergyVad, chunks: list[AudioChunk]) -> list:
    events = []
    for chunk in chunks:
        events.extend(vad.process(chunk))
    return events


def test_silence_produces_no_events() -> None:
    vad = EnergyVad()
    chunks = _to_chunks(_silence_pcm(2.0))
    events = _run(vad, chunks)
    assert events == []
    assert vad.in_speech is False


def test_tone_then_silence_emits_speech_start_and_pause() -> None:
    vad = EnergyVad(pause_ms=400, min_speech_s=1.5)
    pcm = _tone_pcm(440, 16000, 2.0) + _silence_pcm(0.5)
    events = _run(vad, _to_chunks(pcm))

    assert len(events) == 2
    start, pause = events
    assert start.kind == "speech_start"
    assert abs(start.t - 0.0) < 0.15
    assert pause.kind == "pause"
    assert abs(pause.t - 2.4) < 0.15


def test_short_speech_below_min_speech_s_has_no_pause() -> None:
    vad = EnergyVad(pause_ms=400, min_speech_s=1.5)
    pcm = _tone_pcm(440, 16000, 1.0) + _silence_pcm(0.8)
    events = _run(vad, _to_chunks(pcm))

    kinds = [e.kind for e in events]
    assert kinds == ["speech_start"]
    assert vad.in_speech is False


def test_adaptive_floor_detects_voice_over_background_noise() -> None:
    vad = EnergyVad(pause_ms=400, min_speech_s=1.5)

    noise_prefix = _noise_pcm(amplitude=58, seconds=2.0, seed=1)
    voice = _mix(
        _noise_pcm(amplitude=58, seconds=2.0, seed=2),
        _tone_pcm(440, 1800, 2.0),
    )
    trailing_silence = _noise_pcm(amplitude=58, seconds=1.0, seed=3)
    pcm = noise_prefix + voice + trailing_silence

    events = _run(vad, _to_chunks(pcm))

    # No spurious events while only background noise is present.
    assert [e for e in events if e.t < 2.0] == []
    assert events, "expected the voice segment to be detected"
    assert events[0].kind == "speech_start"
    assert 1.9 <= events[0].t <= 2.3
