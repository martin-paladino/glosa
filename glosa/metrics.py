"""Pure production-monitoring building blocks: latency, cost and room health.

These classes track and classify already-computed signals; they don't read
audio, call engines or touch the DB. Wiring them into RoomWorker and the
admin SSE stream (populating models.RoomStatus once a second, per the
spec's "cada 1 s") is a later, full task - see the module docstring in
exports.py for the same split.

All timestamps (t) are seconds on the room's own clock (see Clock in
clock.py) - the same origin as AudioChunk.t / VadEvent.t, not wall time.
"""

from __future__ import annotations

from typing import Literal

from glosa.config import Prices

# "el retraso supera 5 s" (yellow if latency > 5s).
_LATENCY_YELLOW_S = 5.0
# "la calidad promedio es menor a 0.5" (yellow if avg quality < 0.5).
_QUALITY_YELLOW = 0.5
# "el nivel es menor a -50 dB con charla activa" (yellow if level < -50dB
# while a talk is active).
_LEVEL_YELLOW_DB = -50.0
# Gap in output after which the last target_delta before it counts as the
# end of that utterance's translation ("el ultimo target_delta antes de
# >=0.7 s sin salida").
_OUTPUT_GAP_S = 0.7


class LatencyTracker:
    """Tracks end-to-end caption latency from streaming events.

    Delay = the last target_delta (on_output) received before a gap of
    >=0.7s without further output, minus the pause (on_pause) that preceded
    that utterance's output. Each pause is consumed by exactly one
    measurement: a pause becomes the anchor for the next output stretch
    only once (on the first on_output() after it), and an output stretch
    that starts with no unconsumed pause available does not produce a
    measurement at all - it is simply not attributable to any utterance.

    The >=0.7s gap can be noticed two ways:
      - on_output(): when a *later* output arrives after the gap has
        already passed (the common case while a room stays busy);
      - tick(t): RoomWorker calls this periodically (and at end of talk)
        so a stretch still gets closed even if no further output ever
        arrives - otherwise the last utterance in a talk would never be
        finalized.

    p50() is computed only over closed (finalized) samples whose closing
    time falls within the last window_s seconds, relative to the most
    recent timestamp this tracker has seen.
    """

    def __init__(self, window_s: float = 300.0) -> None:
        self.window_s = window_s
        self._unconsumed_pause: float | None = None
        self._pending_pause: float | None = None
        self._last_output: float | None = None
        self._samples: list[tuple[float, float]] = []  # (closed_at, latency_s)
        self._latest_t: float = 0.0

    def on_pause(self, t: float) -> None:
        self._unconsumed_pause = t
        self._latest_t = max(self._latest_t, t)

    def on_output(self, t: float) -> None:
        self._latest_t = max(self._latest_t, t)

        if self._last_output is not None and t - self._last_output >= _OUTPUT_GAP_S:
            self._close_stretch()

        self._arm_pending()
        self._last_output = t
        self._prune()

    def tick(self, t: float) -> None:
        """Periodic/end-of-talk check: close the pending measurement if t is
        already >=0.7s past the last output, even though no further output
        has arrived to notice that gap on its own. Idempotent - safe to
        call repeatedly (e.g. once a second) and a no-op once nothing is
        pending or the gap hasn't been reached yet.
        """
        self._latest_t = max(self._latest_t, t)
        if self._last_output is not None and t - self._last_output >= _OUTPUT_GAP_S:
            self._close_stretch()
        self._prune()

    def _arm_pending(self) -> None:
        """Attach the most recent not-yet-used pause to the in-progress
        measurement, if there is one and no measurement is already open.
        Consumes _unconsumed_pause so it can back at most one measurement.
        """
        if self._pending_pause is None and self._unconsumed_pause is not None:
            self._pending_pause = self._unconsumed_pause
            self._unconsumed_pause = None

    def _close_stretch(self) -> None:
        if self._pending_pause is not None and self._last_output is not None:
            closed_at = self._last_output
            self._samples.append((closed_at, closed_at - self._pending_pause))
        self._pending_pause = None

    def _prune(self) -> None:
        cutoff = self._latest_t - self.window_s
        self._samples = [(t, latency) for t, latency in self._samples if t >= cutoff]

    def samples(self) -> list[tuple[float, float]]:
        """Closed (finalized) samples currently in the window, as
        (closed_at, latency_s) pairs - exposed mainly for tests/debugging.
        """
        return list(self._samples)

    def p50(self) -> float | None:
        values = sorted(latency for _t, latency in self._samples)
        if not values:
            return None
        n = len(values)
        mid = n // 2
        if n % 2 == 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2


class CostTracker:
    """Accumulates API spend against the event's prepaid budget.

    prices (Settings.prices) is accepted per the spec's constructor
    signature and kept for callers that need the price table alongside
    tracked spend (e.g. an admin breakdown); this tracker's own logic only
    needs budget_usd and the usd amounts passed to add().
    """

    def __init__(self, prices: Prices, budget_usd: float) -> None:
        self.prices = prices
        self.budget_usd = budget_usd
        self._total = 0.0

    def add(self, room_id: str, component: str, usd: float) -> None:
        # room_id/component aren't broken down yet (only the running total
        # is tested/consumed so far); add() already takes them so a future
        # per-room/per-component breakdown doesn't need a signature change.
        self._total += usd

    def total(self) -> float:
        return self._total

    def ratio(self) -> float:
        if self.budget_usd <= 0:
            return float("inf") if self._total > 0 else 0.0
        return self._total / self.budget_usd

    def alert(self) -> Literal["80%", "exhausted"] | None:
        ratio = self.ratio()
        if ratio >= 1.0:
            return "exhausted"
        if ratio >= 0.8:
            return "80%"
        return None


class RoomHealth:
    """Pure classifier for models.RoomStatus.state, given already-computed
    room signals (RoomWorker/the relay/VAD are responsible for producing
    those signals; this only applies the truth table).

    Precedence: idle (no talk) > red (stall_active / source_down /
    payment_blocked) > yellow (latency, quality, level+talk_active, or a
    recent reconnect) > green. Red always wins over a recent reconnect -
    an active stall/source-down/payment-blocked condition is worse than
    "merely degraded".
    """

    @staticmethod
    def evaluate(
        latency_p50: float | None,
        quality_avg: float | None,
        level_db: float,
        stall_active: bool,
        source_down: bool,
        payment_blocked: bool,
        talk_active: bool,
        recent_reconnect: bool = False,
    ) -> tuple[Literal["green", "yellow", "red", "idle"], str]:
        if not talk_active:
            return "idle", "no talk in progress"

        if stall_active:
            return "red", "stalled: audio present but no engine output"
        if source_down:
            return "red", "source is down"
        if payment_blocked:
            return "red", "payment blocked: budget exhausted"

        if latency_p50 is not None and latency_p50 > _LATENCY_YELLOW_S:
            return "yellow", f"latency {latency_p50:.1f}s exceeds {_LATENCY_YELLOW_S:.1f}s"
        if quality_avg is not None and quality_avg < _QUALITY_YELLOW:
            return "yellow", f"average quality {quality_avg:.2f} below {_QUALITY_YELLOW:.1f}"
        if level_db < _LEVEL_YELLOW_DB:
            return (
                "yellow",
                f"level {level_db:.1f}dB below {_LEVEL_YELLOW_DB:.0f}dB with active talk",
            )
        if recent_reconnect:
            return "yellow", "degraded: recent reconnect in the last 60s"

        return "green", "ok"
