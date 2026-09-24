"""Tests for Segmenter: the P1 segmentation heuristic from globals.md
("Segmentador P1: corta en . ? ! ; :; en , si hay al menos 5 palabras;
fuerza el corte con 14 palabras o 3.0 s"), covering the four scenarios in
task-10-brief.md's 10.1 plus a few adjacent edge cases.
"""

from __future__ import annotations

from glosa.text.segmenter import Segmenter


def test_terminal_punctuation_closes_a_segment() -> None:
    seg = Segmenter()
    out = seg.feed("We deploy our Kubernetes operator with Helm.", t=0.0)
    assert out == ["We deploy our Kubernetes operator with Helm."]


def test_comma_with_fewer_than_min_words_does_not_cut() -> None:
    seg = Segmenter(comma_min_words=5)
    out = seg.feed("So, the main idea", t=0.0)
    assert out == []


def test_comma_with_at_least_min_words_cuts() -> None:
    seg = Segmenter(comma_min_words=5)
    out = seg.feed("We looked at latency cost and reliability, then decided", t=0.0)
    assert out == ["We looked at latency cost and reliability,"]


def test_fifteen_words_without_punctuation_cuts_at_fourteen() -> None:
    seg = Segmenter(max_words=14)
    words = [f"w{i}" for i in range(1, 16)]
    out = seg.feed(" ".join(words), t=0.0)
    assert out == [" ".join(words[:14])]


def test_tick_forces_a_cut_after_max_wait_s() -> None:
    seg = Segmenter(max_wait_s=3.0)
    assert seg.feed("the deploy is still running", t=0.0) == []
    assert seg.tick(2.9) == []
    assert seg.tick(3.1) == ["the deploy is still running"]


def test_tick_is_a_noop_with_no_open_segment() -> None:
    seg = Segmenter()
    assert seg.tick(100.0) == []


def test_flush_closes_an_open_segment_then_resets() -> None:
    seg = Segmenter()
    seg.feed("hanging thought without punctuation", t=0.0)
    assert seg.flush() == ["hanging thought without punctuation"]
    assert seg.flush() == []


def test_flush_on_an_empty_segmenter_returns_nothing() -> None:
    seg = Segmenter()
    assert seg.flush() == []


def test_multiple_sentences_in_one_feed_call_yield_multiple_segments() -> None:
    seg = Segmenter()
    out = seg.feed("First point. Second point.", t=0.0)
    assert out == ["First point.", "Second point."]


def test_after_a_cut_the_new_segment_starts_its_own_word_count() -> None:
    seg = Segmenter(max_words=14, comma_min_words=5)
    out = seg.feed("We deploy our Kubernetes operator with Helm. So, the main idea", t=0.0)
    # The period cuts the first sentence; "So," then only has 1 preceding
    # word (below comma_min_words), so nothing else closes in this call.
    assert out == ["We deploy our Kubernetes operator with Helm."]


def test_a_cut_reopens_a_fresh_timeout_window() -> None:
    seg = Segmenter(max_wait_s=3.0)
    seg.feed("First sentence.", t=10.0)
    seg.feed(" trailing words", t=10.5)
    # The segment reopened at t=10.0 was closed by the period; the
    # in-progress text since then has only been open ~0.5s.
    assert seg.tick(10.9) == []
    assert seg.tick(13.6) == ["trailing words"]
