"""QualityMeter: a rolling translation-fidelity score backed by Jev (TypeSafe
AI's "System One" decision model).

For each closed translated segment, the room pairs it with the closed source
segment whose time window overlaps (find_matching_source), then asks Jev a
single yes/no (Noul) question about faithfulness. Jev never generates text:
it answers a fixed question with a probability, which keeps this call fast
(~0.37s p50, validated pre-hackathon) and cheap.

When no TYPESAFE_API_KEY is configured the meter is a no-op: score() always
returns None, no HTTP call is ever made (client is never touched, even if
one was injected), and avg() stays None so the dashboard shows "-".

Wiring this into RoomWorker (calling score() without blocking the pipeline,
feeding add() from there) is a later task; this module is pure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Sequence

from typesafe_sdk import AsyncTypeSafeClient, Noul

logger = logging.getLogger(__name__)

FIDELITY_QUESTION = (
    "The Spanish text is a faithful translation of the English text: "
    "no wrong technical terms, no inverted meaning, no omissions."
)

_QUESTION_KEY = "fidelity"
_MODEL = "jev-latest"
_WARNING_INTERVAL_S = 60.0  # rate-limit "Jev call failed" warnings to at most one per this long

# Indirection so tests can control just this clock (e.g. to test the warning
# rate limit) without also skewing asyncio's own internal timing, which reads
# time.monotonic() directly for the event loop clock that asyncio.timeout()
# relies on.
_monotonic = time.monotonic


@dataclass
class ClosedSegment:
    """A finished (closed) caption segment, source or target language.

    t_start/t_end are seconds since the start of the room's audio stream
    (same clock as AudioChunk.t / ExportSegment).
    """

    text: str
    t_start: float
    t_end: float


def find_matching_source(target: ClosedSegment, sources: Sequence[ClosedSegment]) -> ClosedSegment | None:
    """Pair a closed translated segment with the closed source segment to
    score it against.

    Per spec: take the source segment whose own [t_start, t_end] overlaps
    the target's window [target.t_start - 1.0, target.t_end + 0.5] (the
    translation lags the source, so the window looks slightly back and a
    bit forward). When several sources qualify, the one with the greatest
    overlap wins. Returns None if nothing overlaps.
    """
    lo = target.t_start - 1.0
    hi = target.t_end + 0.5
    best: ClosedSegment | None = None
    best_overlap = 0.0
    for src in sources:
        overlap = min(src.t_end, hi) - max(src.t_start, lo)
        if overlap > best_overlap:
            best_overlap = overlap
            best = src
    return best


class QualityMeter:
    """Rolling average of Jev fidelity scores for one room.

    api_key: TYPESAFE_API_KEY, or None to disable the meter entirely.
    window: how many recent scores avg() averages over.
    client: dependency injection for tests (a TypeSafeClient-shaped object
        with an async system_one()); ignored when api_key is falsy, so a
        no-key meter never calls out even if a client is passed in.

    failures: count of score() calls that errored or timed out (Jev is
        optional, so these never raise into the caller; they just return
        None and bump this counter for observability).
    """

    def __init__(self, api_key: str | None, window: int = 10, *, client: object | None = None) -> None:
        self._owns_client = bool(api_key) and client is None
        if api_key:
            self._client = client if client is not None else AsyncTypeSafeClient(api_key=api_key)
        else:
            self._client = None
        self._scores: deque[float] = deque(maxlen=window)
        self.failures = 0
        self._last_warning_at: float | None = None

    async def score(self, src: str, tgt: str, *, timeout_s: float = 3.0) -> float | None:
        """Ask Jev whether tgt is a faithful translation of src. Returns the
        Noul probability (0..1), or None when the meter has no key.

        Jev is optional: "si falla, el medidor de calidad se apaga y todo lo
        demás sigue". Any error from the call, or a response slower than
        timeout_s (the meter is only useful fast; a slow answer is as good
        as none), is swallowed, counted in `failures`, and logged (at most
        one warning per _WARNING_INTERVAL_S, to avoid flooding logs during
        an outage). Cancellation (the caller shutting the room down) is not
        a "failure" and always propagates.
        """
        if self._client is None:
            return None
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._client.system_one(
                    state={"english": src, "spanish": tgt},
                    questions={_QUESTION_KEY: Noul(instructions=FIDELITY_QUESTION)},
                    model=_MODEL,
                )
            return response.answers[_QUESTION_KEY].noul
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Jev is optional: never break the pipeline
            self.failures += 1
            self._log_failure(exc)
            return None

    def _log_failure(self, exc: Exception) -> None:
        now = _monotonic()
        if self._last_warning_at is None or now - self._last_warning_at >= _WARNING_INTERVAL_S:
            logger.warning("Jev quality check failed, quality meter degraded: %s", exc)
            self._last_warning_at = now

    def add(self, p: float | None) -> None:
        """Feed one score into the rolling window. None (no key, or no
        matching source segment) is ignored."""
        if p is None:
            return
        self._scores.append(p)

    def avg(self) -> float | None:
        """Rolling average over the last `window` scores, or None if the
        meter has no key or nothing has been added yet."""
        if self._client is None or not self._scores:
            return None
        return sum(self._scores) / len(self._scores)

    async def aclose(self) -> None:
        """Release the underlying HTTP client, if this meter created one
        (an injected client, e.g. in tests, is left for its owner to close).
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
