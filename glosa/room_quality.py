"""QualityFeed: wires glosa/quality.py's QualityMeter (Task 13a, pure) into
RoomWorker, without ever paying typesafe_sdk's import cost unless a Jev key
is actually configured.

glosa.quality imports typesafe_sdk (the optional "jev" extra) at module
level, so nothing here imports it eagerly: build_quality_meter() imports it
lazily, inside the function, only once RoomWorker has confirmed
settings.typesafe_api_key is set. Likewise QualityFeed only reaches for
glosa.quality.find_matching_source inside on_target() -- by the time a
QualityFeed exists at all, build_quality_meter() has already imported
glosa.quality successfully, so that import is just a (cheap, cached)
sys.modules lookup, never a fresh attempt.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

# At most one score started this often, per room (a module constant per spec).
QUALITY_MIN_INTERVAL_S = 15.0

_SOURCE_WINDOW = 20  # keep the last N closed source segments to pair against
_MIN_WORDS = 4  # skip a pair where either text is shorter than this

# The Jev question is fixed to state keys english/spanish (see
# glosa.quality.FIDELITY_QUESTION); score only these two directions, and
# "swap" means the source text is Spanish, the target text is English, so
# they land in the opposite state keys from the en->es case.
_DIRECTIONS = {("en", "es"): "direct", ("es", "en"): "swap"}


class MeterLike(Protocol):
    """The shape QualityFeed needs from a meter: glosa.quality.QualityMeter,
    or a fake for tests."""

    async def score(self, src: str, tgt: str) -> float | None: ...
    def add(self, p: float | None) -> None: ...
    def avg(self) -> float | None: ...
    def reset(self) -> None: ...
    async def aclose(self) -> None: ...


def build_quality_meter(api_key: str) -> MeterLike | None:
    """Build a QualityMeter for ``api_key`` (the caller -- RoomWorker --
    already checked settings.typesafe_api_key is truthy before calling
    this). Imports glosa.quality lazily: that module imports typesafe_sdk
    (the "jev" extra) at module level, so importing it in here, rather
    than at glosa.room's module level, is what keeps Glosa usable without
    the extra installed.

    On ImportError (the extra isn't installed) logs one warning and
    returns None: the meter stays off exactly like without a key at all.
    """
    try:
        from glosa.quality import QualityMeter
    except ImportError:
        logger.warning(
            "TYPESAFE_API_KEY is set but the 'jev' extra is not installed: quality stays unmeasured"
        )
        return None
    return QualityMeter(api_key)


@dataclass
class _Seg:
    text: str
    t_start: float
    t_end: float


def _word_count(text: str) -> int:
    return len(text.split())


class QualityFeed:
    """Per-room glue: pairs closed source/translation segments, rate-limits
    and backgrounds the Jev call, and holds the rolling average that
    RoomWorker.status() surfaces as ``quality``.

    meter: a MeterLike (glosa.quality.QualityMeter, or a fake for tests).
    now: the room's Clock.now (monotonic seconds) -- NOT glosa.quality's
        own internal clock, which only paces its failure-warning log.
    spawn: RoomWorker._spawn_aux (fire-and-forget; stop() waits for it).
    """

    def __init__(self, meter: MeterLike, *, now: Callable[[], float], spawn: Callable[..., None]) -> None:
        self._meter = meter
        self._now = now
        self._spawn = spawn
        self._sources: deque[_Seg] = deque(maxlen=_SOURCE_WINDOW)
        self._in_flight = False
        self._last_started: float | None = None
        self._closed = False

    def reset(self) -> None:
        """A new talk: forget the last run's source window and score
        average (window 10 starts empty per talk)."""
        self._sources.clear()
        self._meter.reset()

    def on_source(self, text: str, t_start: float, t_end: float) -> None:
        """Feed one closed, non-empty source segment (kept: the last
        ``_SOURCE_WINDOW`` of them)."""
        self._sources.append(_Seg(text, t_start, t_end))

    def on_target(self, source_lang: str, target_lang: str, text: str, t_start: float, t_end: float) -> None:
        """Feed one closed, non-empty translation segment of the run's
        first target. Pairs it with the closest-overlapping source segment
        (find_matching_source), skips anything that isn't en<->es or is
        too short, and -- at most one in flight, at most one started every
        QUALITY_MIN_INTERVAL_S s -- scores it in the background."""
        direction = _DIRECTIONS.get((source_lang, target_lang))
        if direction is None:
            return  # not en<->es: the Jev question is fixed to those two
        if not self._sources:
            return
        from glosa.quality import find_matching_source  # see module docstring: already imported

        target_seg = _Seg(text, t_start, t_end)
        source_seg = find_matching_source(target_seg, tuple(self._sources))
        if source_seg is None:
            return
        if _word_count(source_seg.text) < _MIN_WORDS or _word_count(text) < _MIN_WORDS:
            return
        if not self._may_start():
            return  # one in flight, or started one too recently: drop, don't queue
        english, spanish = (source_seg.text, text) if direction == "direct" else (text, source_seg.text)
        self._in_flight = True
        self._spawn(self._score(english, spanish), "quality")

    def _may_start(self) -> bool:
        if self._in_flight:
            return False
        now = self._now()
        if self._last_started is not None and now - self._last_started < QUALITY_MIN_INTERVAL_S:
            return False
        self._last_started = now
        return True

    async def _score(self, english: str, spanish: str) -> None:
        p: float | None = None
        try:
            p = await self._meter.score(english, spanish)
        except asyncio.CancelledError:
            raise
        except Exception:  # a misbehaving injected meter must not break the room either
            logger.exception("quality: score() failed unexpectedly")
        finally:
            self._in_flight = False
        self._meter.add(p)

    def avg(self) -> float | None:
        return self._meter.avg()

    async def aclose(self) -> None:
        """Release the meter's HTTP client. Idempotent: RoomWorker.stop()
        may be called more than once (already-stopped is a no-op)."""
        if self._closed:
            return
        self._closed = True
        await self._meter.aclose()
