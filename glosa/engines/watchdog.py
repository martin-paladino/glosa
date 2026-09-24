"""StallWatchdog: tells when an engine session has stopped answering.

Spec §6: "voz sin texto de salida durante más de 8 s" -> reconnect. It is fed
with the real per-chunk voice activity (``EnergyVad.in_speech``, passed to
``SessionRelay.feed(chunk, voiced=...)``) rather than inferred from VAD
events. EnergyVad emits ``pause`` only after >= 1.5 s of voice, so events
alone cannot tell when a short utterance ended.

``stalled(t)`` is True only when **all** of these hold:

1. there was voice *after* the session's last output;
2. the first such voice is at least ``limit`` s old;
3. there was voice within the last ``limit`` s. Voice that has since gone
   quiet stops counting, so a stall is not raised in the middle of a
   silence;
4. that unanswered voice spans at least ``min_voice_s`` (1 s). EnergyVad
   keeps ``in_speech`` True for 400 ms after the voice ends, so output that
   arrives in that tail leaves a few "voiced" chunks that the engine has
   nothing to say about.

When voice resumes after a gap, what was left unanswered before is dropped
if it was shorter than ``min_voice_s`` (a tail, or a blip the model may
rightly ignore) or older than the limit. Otherwise a stale tail from an
earlier utterance would make new speech look many seconds overdue the
moment it starts.

``limit`` is ``timeout`` (8 s), raised to ``first_output_grace_s`` (15 s)
while the session warms up: until its first output, and for unanswered
stretches that begin in its first ``first_output_grace_s`` seconds. The
real 10-min Live Translate run shows why: first output 5.1 s after connect,
then 9.2 s of nothing while the speaker talked on, then normal output.

``reset(t)`` starts over for a session that begins taking audio at ``t``
(activation, reconnect). Earlier voice is forgotten, so after a stall
reconnect it cannot fire again until new voice arrives.
"""

from __future__ import annotations

_EPS = 1e-9  # t values are sums of 0.1 s steps
_RUN_GAP_S = 0.5  # voiced chunks further apart than this are separate runs


class StallWatchdog:
    def __init__(
        self,
        timeout: float = 8.0,
        first_output_grace_s: float = 15.0,
        min_voice_s: float = 1.0,
    ) -> None:
        self.timeout = timeout
        self.first_output_grace_s = first_output_grace_s
        self.min_voice_s = min_voice_s
        self.reset(0.0)

    def reset(self, t: float) -> None:
        """A session starts taking audio at ``t``: forget voice and output."""
        self._started_at = t
        self._had_output = False
        self._first_unanswered: float | None = None
        self._last_voice: float | None = None

    def on_voice(self, t: float) -> None:
        """A voiced chunk (``in_speech``) was sent at ``t``."""
        first, last = self._first_unanswered, self._last_voice
        if first is not None and last is not None and t - last > _RUN_GAP_S:
            # Voice resumes after a gap. A tail/blip, or voice too old to
            # count: start over from this voice.
            stale = last - first < self.min_voice_s - _EPS or t - last > self._limit(first) + _EPS
            first = None if stale else first
        self._first_unanswered = t if first is None else first
        self._last_voice = t

    def on_output(self, t: float) -> None:
        """The session produced text (a source/target delta) at ``t``."""
        self._had_output = True
        self._first_unanswered = None

    def stalled(self, t: float) -> bool:
        first, last = self._first_unanswered, self._last_voice
        if first is None or last is None:
            return False
        limit = self._limit(first)
        return (
            t - first >= limit - _EPS
            and t - last <= limit + _EPS
            and last - first >= self.min_voice_s - _EPS
        )

    def _limit(self, first: float) -> float:
        """``timeout``, or the grace while the session warms up."""
        warming_up = not self._had_output or first < self._started_at + self.first_output_grace_s
        return max(self.timeout, self.first_output_grace_s) if warming_up else self.timeout
