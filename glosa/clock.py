"""Clock: a monotonic time source that can be swapped for a deterministic fake
in tests. Every component that needs "now" (relay timers, VAD, latency
tracking, ...) takes a Clock instead of calling time.monotonic()/datetime.now()
directly.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds, arbitrary origin."""
        ...

    def wall(self) -> datetime:
        """Current wall-clock time (timezone-aware, UTC)."""
        ...

    async def sleep(self, s: float) -> None:
        """Suspend for s seconds (or, for FakeClock, just advance time)."""
        ...


class RealClock:
    """Clock backed by the real system clock, for production use."""

    def __init__(self) -> None:
        self._origin = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._origin

    def wall(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)


class FakeClock:
    """Deterministic Clock for tests: time only moves when advance() (or
    sleep(), which just calls advance()) is called.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._wall_origin = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> float:
        return self._t

    def wall(self) -> datetime:
        return self._wall_origin + timedelta(seconds=self._t)

    def advance(self, s: float) -> None:
        self._t += s

    async def sleep(self, s: float) -> None:
        self.advance(s)
