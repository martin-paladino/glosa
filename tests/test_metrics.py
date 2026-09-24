"""Tests for glosa/metrics.py: LatencyTracker, CostTracker, RoomHealth.

Covers spec cases 12.1-12.3:
  - 12.1 LatencyTracker with a synthetic sequence: pause at 10.0, last
    output at 12.4 -> 2.4.
  - 12.2 CostTracker: 80% -> alert; reaching/exceeding the budget (what a
    real 402 from the provider means for us) -> "exhausted".
  - 12.3 RoomHealth: a truth table of states, one case per rule.
"""

from __future__ import annotations

from glosa.config import Prices
from glosa.metrics import CostTracker, LatencyTracker, RoomHealth


# --- LatencyTracker (12.1) -------------------------------------------------


def test_latency_tracker_pause_then_last_output_before_gap_is_2_4() -> None:
    # This is the exact scenario from case 12.1: a pause at 10.0, a stream
    # of outputs whose last one before a >=0.7s gap lands at 12.4. The gap
    # is what tells the tracker that stretch of output is finished; it is
    # detected by the next on_output() call arriving late.
    tracker = LatencyTracker()
    tracker.on_pause(10.0)
    tracker.on_output(10.9)  # gaps between these stay < 0.7s: same stretch
    tracker.on_output(11.3)
    tracker.on_output(11.9)
    tracker.on_output(12.4)
    tracker.on_output(13.2)  # 0.8s since 12.4 >= 0.7s: closes the stretch

    assert tracker.p50() == 12.4 - 10.0


def test_latency_tracker_returns_none_with_no_closed_samples() -> None:
    tracker = LatencyTracker()
    assert tracker.p50() is None

    tracker.on_pause(1.0)
    tracker.on_output(1.5)
    # No gap yet: the stretch that started at 1.5 hasn't closed.
    assert tracker.p50() is None


def test_latency_tracker_pairs_each_utterance_with_its_own_pause() -> None:
    tracker = LatencyTracker()

    # Utterance 1: pause at 0.0, closes at 1.0 (gap before 2.0).
    tracker.on_pause(0.0)
    tracker.on_output(1.0)
    tracker.on_output(2.0)  # gap 1.0s >= 0.7s closes utterance 1 (1.0 - 0.0)

    # Utterance 2: pause at 5.0, closes at 6.0 (gap before 8.0).
    tracker.on_pause(5.0)
    tracker.on_output(6.0)
    tracker.on_output(8.0)  # gap 2.0s >= 0.7s closes utterance 2 (6.0 - 5.0)

    # Median of [1.0, 1.0] is 1.0.
    assert tracker.p50() == 1.0


def test_latency_tracker_p50_is_the_median_of_closed_samples() -> None:
    tracker = LatencyTracker()

    for pause_t, output_t in [(0.0, 1.0), (10.0, 13.0), (20.0, 22.0)]:
        tracker.on_pause(pause_t)
        tracker.on_output(output_t)
        tracker.on_output(output_t + 1.0)  # forces the gap-close

    # Closed latencies: 1.0, 3.0, 2.0 -> sorted [1.0, 2.0, 3.0] -> median 2.0
    assert tracker.p50() == 2.0


def test_latency_tracker_excludes_samples_outside_the_window() -> None:
    tracker = LatencyTracker(window_s=10.0)

    tracker.on_pause(0.0)
    tracker.on_output(1.0)
    tracker.on_output(2.0)  # closes sample latency=1.0 at t=1.0

    tracker.on_pause(100.0)
    tracker.on_output(105.0)
    tracker.on_output(106.0)  # closes sample latency=5.0 at t=105.0

    # 1.0's closing time (t=1.0) is far outside the 10s window relative to
    # the latest timestamp seen (106.0), so only the second sample counts.
    assert tracker.p50() == 5.0


# --- CostTracker (12.2) -----------------------------------------------------


def _prices() -> Prices:
    return Prices()


def test_cost_tracker_total_sums_all_additions() -> None:
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)

    tracker.add("main", "live_translate", 1.5)
    tracker.add("main", "transcribe", 0.5)
    tracker.add("stage2", "live_translate", 2.0)

    assert tracker.total() == 4.0


def test_cost_tracker_ratio_is_total_over_budget() -> None:
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)
    tracker.add("main", "live_translate", 5.0)

    assert tracker.ratio() == 0.5


def test_cost_tracker_alert_is_none_below_80_percent() -> None:
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)
    tracker.add("main", "live_translate", 7.9)

    assert tracker.alert() is None


def test_cost_tracker_alert_80_percent() -> None:
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)
    tracker.add("main", "live_translate", 8.0)

    assert tracker.alert() == "80%"


def test_cost_tracker_alert_exhausted_when_budget_reached() -> None:
    # Reaching (or exceeding) the budget is what a real 402 from the
    # provider means for us: no more spend is possible.
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)
    tracker.add("main", "live_translate", 10.0)

    assert tracker.alert() == "exhausted"


def test_cost_tracker_alert_exhausted_when_budget_exceeded() -> None:
    tracker = CostTracker(prices=_prices(), budget_usd=10.0)
    tracker.add("main", "live_translate", 12.0)

    assert tracker.alert() == "exhausted"
    assert tracker.ratio() == 1.2


# --- RoomHealth (12.3) -------------------------------------------------------


def test_room_health_idle_when_no_talk_active() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=None,
        quality_avg=None,
        level_db=-60.0,
        stall_active=False,
        source_down=False,
        payment_blocked=False,
        talk_active=False,
    )
    assert state == "idle"
    assert detail


def test_room_health_green_when_everything_is_fine() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.5,
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=False,
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "green"
    assert detail


def test_room_health_red_when_stall_active_with_voice() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.0,
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=True,
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "red"
    assert detail


def test_room_health_red_when_source_down() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.0,
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=False,
        source_down=True,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "red"
    assert detail


def test_room_health_red_when_payment_blocked() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.0,
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=False,
        source_down=False,
        payment_blocked=True,
        talk_active=True,
    )
    assert state == "red"
    assert detail


def test_room_health_yellow_when_latency_over_5s() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=5.1,
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=False,
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "yellow"
    assert detail


def test_room_health_yellow_when_quality_below_0_5() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.0,
        quality_avg=0.4,
        level_db=-20.0,
        stall_active=False,
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "yellow"
    assert detail


def test_room_health_yellow_when_level_below_minus_50db_with_active_talk() -> None:
    state, detail = RoomHealth.evaluate(
        latency_p50=1.0,
        quality_avg=0.9,
        level_db=-55.0,
        stall_active=False,
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "yellow"
    assert detail


def test_room_health_red_takes_priority_over_yellow() -> None:
    state, _detail = RoomHealth.evaluate(
        latency_p50=999.0,  # would also be yellow
        quality_avg=0.9,
        level_db=-20.0,
        stall_active=True,  # red
        source_down=False,
        payment_blocked=False,
        talk_active=True,
    )
    assert state == "red"


def test_room_health_idle_takes_priority_over_red() -> None:
    state, _detail = RoomHealth.evaluate(
        latency_p50=None,
        quality_avg=None,
        level_db=-20.0,
        stall_active=True,  # would be red if a talk were active
        source_down=False,
        payment_blocked=False,
        talk_active=False,
    )
    assert state == "idle"
