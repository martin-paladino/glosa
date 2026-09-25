"""SilenceGate: stop paying for silence between and inside talks (task-19).

Spec: "mas de 20 s sin voz -> deja de enviar audio, guarda el ultimo segundo
y lo manda primero al volver la voz". Keyed off the room's own EnergyVad
(glosa/audio/vad.py) ``in_speech`` -- no separate detector: a chunk counts
as voice exactly when the VAD says so, the same signal SessionRelay's
watchdog already uses (``relay.feed(chunk, voiced=vad.in_speech)``).

``update()`` is the whole contract: fed one AudioChunk in stream order and
the VAD's ``in_speech`` for it, it returns the chunks the caller should
actually forward to the engine this call -- normally ``[chunk]``, ``[]``
while gated, or the saved pre-roll plus ``chunk`` the instant voice returns,
in order, with no gap or duplicate. Everything else in the room's per-chunk
loop (the VAD itself, ``relay.on_vad``, the translation lane's ``tick``,
level tracking for ``RoomWorker.status()``) keeps running on every chunk
regardless of gating: the gate only decides what gets SENT. See
glosa/room.py's module docstring ("Silence gate") for how RoomWorker wires
this in and why that is enough to leave the relay's rotation/watchdog/
fallback machinery, and the glossary engine's own end-of-utterance, alone.

``gated_s`` counts only audio that is gone for good: chunks aged out of the
fixed-size pre-roll ring buffer. The pre-roll itself is sent, just delayed,
so it is never counted as saved.
"""

from __future__ import annotations

from collections import deque

from glosa.audio.ingest import CHUNK_S
from glosa.models import AudioChunk

GATE_AFTER_S = 20.0  # no voice this long -> stop sending
PREROLL_S = 1.0  # ... but keep sending these seconds back once voice returns
_EPS = 1e-9


class SilenceGate:
    """Stateful; ``update()`` expects chunks in stream order (increasing
    ``AudioChunk.t``), like EnergyVad. Not safe to call concurrently on the
    same instance."""

    def __init__(self, after_s: float = GATE_AFTER_S, preroll_s: float = PREROLL_S) -> None:
        self.after_s = after_s
        self.preroll_s = preroll_s
        self.gated = False
        self.gated_s = 0.0  # cumulative audio dropped for good (not the pre-roll)

        preroll_len = max(round(preroll_s / CHUNK_S), 0)
        self._preroll: deque[AudioChunk] = deque(maxlen=preroll_len)
        self._silence_since: float | None = None  # chunk.t the current silence run started at
        self._closed_at: float | None = None  # chunk.t the gate last closed at
        self._last_t = 0.0

    @property
    def enabled(self) -> bool:
        return self.after_s > 0

    @property
    def paused_s(self) -> float:
        """How long (audio seconds) the gate has been closed -- for the
        operator-facing status marker. 0 while open."""
        if not self.gated or self._closed_at is None:
            return 0.0
        return max(self._last_t - self._closed_at, 0.0)

    def update(self, chunk: AudioChunk, voiced: bool) -> list[AudioChunk]:
        self._last_t = chunk.t
        if not self.enabled:
            return [chunk]

        if voiced:
            self._silence_since = None
            if not self.gated:
                return [chunk]
            self.gated = False
            self._closed_at = None
            out = list(self._preroll)
            out.append(chunk)
            self._preroll.clear()
            return out

        if self.gated:
            self._withhold(chunk)
            return []

        if self._silence_since is None:
            self._silence_since = chunk.t
        elif chunk.t - self._silence_since >= self.after_s - _EPS:
            self.gated = True
            self._closed_at = chunk.t
            self._withhold(chunk)
            return []
        return [chunk]

    def _withhold(self, chunk: AudioChunk) -> None:
        if len(self._preroll) == self._preroll.maxlen:  # about to push the oldest one out for good
            self.gated_s = round(self.gated_s + CHUNK_S, 3)
        self._preroll.append(chunk)
