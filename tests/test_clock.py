"""FakeClock: deterministic time for tests (no wall-clock/asyncio.sleep)."""

from __future__ import annotations

import pytest

from glosa.clock import FakeClock


def test_fake_clock_starts_at_given_value() -> None:
    clock = FakeClock(start=5.0)
    assert clock.now() == 5.0


def test_fake_clock_advance_moves_now() -> None:
    clock = FakeClock()
    clock.advance(1.5)
    clock.advance(2.5)
    assert clock.now() == 4.0


def test_fake_clock_wall_moves_with_advance() -> None:
    clock = FakeClock()
    before = clock.wall()
    clock.advance(60.0)
    after = clock.wall()
    assert (after - before).total_seconds() == 60.0


@pytest.mark.asyncio
async def test_fake_clock_sleep_advances_without_delay() -> None:
    clock = FakeClock()
    await clock.sleep(10.0)
    assert clock.now() == 10.0
