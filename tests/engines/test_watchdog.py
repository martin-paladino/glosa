"""StallWatchdog: "voz sin texto de salida durante más de 8 s" (spec §6),
fed with the real per-chunk voice activity (EnergyVad.in_speech)."""

from __future__ import annotations

import pytest

from glosa.engines.watchdog import StallWatchdog


def voice(w: StallWatchdog, start: float, end: float, step: float = 0.1) -> None:
    """Voiced chunks from `start` to `end` inclusive, one per `step`."""
    k0, k1 = round(start / step), round(end / step)
    for k in range(k0, k1 + 1):
        w.on_voice(k * step)


def first_stall(w: StallWatchdog, start: float, end: float, step: float = 0.1) -> float | None:
    """Poll stalled() every `step` s, as the relay does on every feed()."""
    k0, k1 = round(start / step), round(end / step)
    return next((k * step for k in range(k0, k1 + 1) if w.stalled(k * step)), None)


def warmed_up(timeout: float = 8.0) -> StallWatchdog:
    """A watchdog on a session that started at 0 s, past its warm-up, answered at 30 s."""
    w = StallWatchdog(timeout=timeout, first_output_grace_s=15.0)
    w.reset(0.0)
    w.on_output(30.0)
    return w


def test_no_voice_never_stalls() -> None:
    w = warmed_up()
    assert first_stall(w, 30.0, 300.0) is None


def test_continuous_unanswered_voice_stalls_after_timeout() -> None:
    w = warmed_up()
    voice(w, 30.1, 50.0)  # the speaker goes on, the model says nothing
    assert first_stall(w, 30.1, 50.0) == approx(38.1)


def test_output_restarts_the_clock_from_the_next_voice() -> None:
    w = warmed_up()
    voice(w, 30.1, 37.0)
    w.on_output(37.0)
    voice(w, 37.1, 50.0)
    assert first_stall(w, 37.0, 50.0) == approx(45.1)


def test_answered_short_utterance_then_silence_never_stalls() -> None:
    # The reviewer's case: a 0.8 s "Thanks!" (EnergyVad never emits a pause
    # for it), answered 1 s later, then silence.
    w = warmed_up()
    voice(w, 40.0, 40.7)
    w.on_output(41.0)
    assert first_stall(w, 40.0, 200.0) is None


def test_output_during_the_vad_tail_is_not_a_stall() -> None:
    # EnergyVad keeps in_speech True for 400 ms after the voice ends. Output
    # arriving then leaves only that tail "unanswered": not a stall.
    w = warmed_up()
    voice(w, 30.1, 40.0)
    w.on_output(39.7)
    voice(w, 39.8, 40.1)  # the tail, then silence
    assert first_stall(w, 39.7, 200.0) is None


def test_a_stale_tail_does_not_come_back_when_speech_resumes() -> None:
    # The tail left after output at 39.7 s must not make the next utterance
    # look 20 s old when it starts at 60 s.
    w = warmed_up()
    voice(w, 30.1, 39.6)
    w.on_output(39.7)
    voice(w, 39.8, 40.1)  # 400 ms tail
    voice(w, 60.0, 61.5)
    w.on_output(61.6)  # the usual latency
    assert first_stall(w, 60.0, 61.6) is None
    # A tail followed a few seconds later by speech that the model never
    # answers: the clock starts at the speech, not at the tail.
    voice(w, 61.7, 61.9)
    voice(w, 65.0, 80.0)
    assert first_stall(w, 61.7, 80.0) == approx(73.0)


def test_substantial_unanswered_voice_keeps_counting_across_a_pause() -> None:
    w = warmed_up()
    voice(w, 30.1, 33.0)  # 3 s unanswered, then a pause
    voice(w, 34.0, 50.0)
    assert first_stall(w, 30.1, 50.0) == approx(38.1)


def test_old_unanswered_voice_stops_counting_once_it_is_older_than_timeout() -> None:
    w = warmed_up()
    voice(w, 100.0, 102.0)  # 2 s of voice, unanswered
    assert w.stalled(108.0)  # 8 s after it started, voice 6 s ago
    assert not w.stalled(111.0)  # nothing voiced in the last 8 s any more


def test_reset_forgets_stale_voice_until_new_voice_arrives() -> None:
    # After a stall reconnect it must not fire again until new voice arrives.
    w = warmed_up()
    voice(w, 30.1, 40.0)
    assert w.stalled(38.1)
    w.reset(40.0)
    w.on_output(41.0)  # the new session's first words (past warm-up below)
    assert first_stall(w, 40.0, 120.0) is None
    voice(w, 120.0, 140.0)
    assert first_stall(w, 120.0, 140.0) == approx(128.0)


def test_grace_until_first_output() -> None:
    w = StallWatchdog(timeout=8.0, first_output_grace_s=15.0)
    w.reset(0.0)
    voice(w, 0.0, 30.0)  # a session that never says anything
    assert first_stall(w, 0.0, 30.0) == approx(15.0)


def test_grace_covers_a_hiccup_early_in_the_session() -> None:
    # The real recording: first output 5.1 s after connect, then 9.2 s of
    # nothing while the speaker talks on, then normal output.
    w = StallWatchdog(timeout=8.0, first_output_grace_s=15.0)
    w.reset(0.0)
    voice(w, 0.0, 5.0)
    w.on_output(5.1)
    voice(w, 5.2, 14.3)
    w.on_output(14.3)
    assert first_stall(w, 0.0, 14.3) is None
    # Past the 15 s warm-up window, the normal 8 s applies again.
    voice(w, 14.4, 15.9)
    w.on_output(16.0)
    voice(w, 16.1, 40.0)
    assert first_stall(w, 14.4, 40.0) == approx(24.1)


def approx(x: float) -> object:
    return pytest.approx(x, abs=1e-6)
