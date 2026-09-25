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


def test_steady_noise_after_digital_silence_stops_reading_as_speech() -> None:  # final-review-A I5
    """The console is muted (digital silence: the floor drops to -96 dBFS),
    then the line's steady analog noise (~-70 dBFS) comes in. The floor used
    to adapt only on non-voice chunks, so that noise read as endless speech
    (no pause ever again). It must adapt while "in speech" too when the
    level is steady, so pauses resume within a few seconds; speech on top
    of that noise then still gets its pauses."""
    vad = EnergyVad(pause_ms=400, min_speech_s=1.5)
    noise_amp = 18  # uniform noise, RMS = 18/sqrt(3) ~ 10.4 -> ~ -70 dBFS
    pcm = _silence_pcm(3.0) + _noise_pcm(noise_amp, 8.0, seed=4)
    for i in range(3):  # then speech (2 s) with 1-s pauses, on top of the same noise
        pcm += _mix(_noise_pcm(noise_amp, 2.0, seed=10 + i), _tone_pcm(440, 1800, 2.0))
        pcm += _noise_pcm(noise_amp, 1.0, seed=20 + i)

    events = _run(vad, _to_chunks(pcm))

    first_pause = next(e for e in events if e.kind == "pause")
    assert first_pause.t <= 3.0 + 6.0  # within a few seconds of the noise coming in
    speech_pauses = [e for e in events if e.kind == "pause" and e.t > 11.0]
    assert len(speech_pauses) == 3  # one per 1-s pause between the phrases
    assert vad.in_speech is False  # the trailing noise is not speech


def test_real_speech_level_changes_never_move_the_floor_mid_speech() -> None:
    """Speech is not steady (syllables): a long voice run whose level keeps
    changing leaves the floor alone, so the next pause is still detected."""
    vad = EnergyVad(pause_ms=400, min_speech_s=1.5)
    speech = b"".join(_tone_pcm(440, amp, 0.2) for amp in [16000, 4000] * 25)  # 10 s, 12 dB swings
    pcm = _noise_pcm(58, 2.0, seed=1) + speech + _noise_pcm(58, 1.0, seed=3)

    events = _run(vad, _to_chunks(pcm))

    assert [e.kind for e in events] == ["speech_start", "pause"]
    assert abs(events[1].t - 12.4) < 0.15
