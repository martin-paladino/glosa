"""Energy-based voice activity detector with an adaptive noise floor.

EnergyVad classifies each incoming AudioChunk (PCM 16 kHz mono s16le) as
voice or non-voice by comparing its RMS level (in dBFS) against a noise
floor that is itself an exponential moving average of recent non-voice
chunks. A chunk counts as voice once its level exceeds the floor by
VOICE_MARGIN_DB (10 dB, per spec).

End-of-speech ("pause") fires once >= pause_ms of continuous silence has
followed a voice run that itself lasted >= min_speech_s; shorter blips end
silently (in_speech goes back to False, but no pause event is emitted), per
the spec's "400 ms of silence with at least 1.5 s of voice".
"""

from __future__ import annotations

import array
import math
import sys

from glosa.models import AudioChunk, VadEvent

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

# Engineering constants (not spec values): a conservative starting guess for
# the noise floor, and how fast the EMA chases the real one. Chosen so a
# typical "quiet room" background stays classified as non-voice from the
# first chunk, while genuine speech (well above that guess) is recognized
# immediately; see tests/audio/test_vad.py for the scenarios this covers.
_INITIAL_FLOOR_DB = -30.0
_FLOOR_EMA_ALPHA = 0.2
_MIN_LEVEL_DB = -96.0  # ~ dynamic range floor of 16-bit PCM

VOICE_MARGIN_DB = 10.0  # "hay voz si el nivel supera el piso en 10 dB"


def _level_db(pcm: bytes) -> float:
    """RMS level of s16le PCM, in dBFS (0 dB = full scale), clamped at the
    bottom to _MIN_LEVEL_DB so pure silence doesn't hit log(0).
    """
    if not pcm:
        return _MIN_LEVEL_DB
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return _MIN_LEVEL_DB
    mean_sq = sum(s * s for s in samples) / len(samples)
    rms = math.sqrt(mean_sq)
    if rms < 1.0:
        return _MIN_LEVEL_DB
    db = 20.0 * math.log10(rms / 32768.0)
    return max(db, _MIN_LEVEL_DB)


def _chunk_duration_s(pcm: bytes) -> float:
    n_samples = len(pcm) // BYTES_PER_SAMPLE
    return n_samples / SAMPLE_RATE


class EnergyVad:
    """Energy VAD with an adaptive noise floor.

    process() is stateful and expects chunks in stream order (increasing
    AudioChunk.t); it is not safe to call concurrently on the same instance.
    """

    def __init__(self, pause_ms: int = 400, min_speech_s: float = 1.5) -> None:
        self.pause_ms = pause_ms
        self.min_speech_s = min_speech_s

        self.level_db: float = _MIN_LEVEL_DB
        self.in_speech: bool = False

        self._floor_db: float = _INITIAL_FLOOR_DB
        self._speech_start_t: float | None = None
        self._silence_start_t: float | None = None

    def process(self, chunk: AudioChunk) -> list[VadEvent]:
        events: list[VadEvent] = []

        level = _level_db(chunk.pcm)
        self.level_db = level
        duration = _chunk_duration_s(chunk.pcm)
        is_voice = level > self._floor_db + VOICE_MARGIN_DB

        if is_voice:
            self._silence_start_t = None
            if not self.in_speech:
                self.in_speech = True
                self._speech_start_t = chunk.t
                events.append(VadEvent(kind="speech_start", t=chunk.t))
            return events

        # Non-voice chunk: adapt the noise floor towards it (never while a
        # chunk was classified as voice, per spec).
        self._floor_db = _FLOOR_EMA_ALPHA * level + (1 - _FLOOR_EMA_ALPHA) * self._floor_db

        if not self.in_speech:
            return events

        if self._silence_start_t is None:
            self._silence_start_t = chunk.t

        silence_elapsed = (chunk.t + duration) - self._silence_start_t
        # Tolerate float accumulation error (t is a running sum of ~0.1 s
        # steps) so the threshold crossing isn't pushed a whole chunk late.
        if silence_elapsed >= self.pause_ms / 1000 - 1e-9:
            speech_start_t = self._speech_start_t if self._speech_start_t is not None else self._silence_start_t
            speech_duration = self._silence_start_t - speech_start_t
            if speech_duration >= self.min_speech_s:
                events.append(VadEvent(kind="pause", t=chunk.t + duration))
            self.in_speech = False
            self._speech_start_t = None
            self._silence_start_t = None

        return events
