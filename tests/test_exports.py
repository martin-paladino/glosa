"""Tests for glosa/exports.py: SRT/VTT/TXT generation from ExportSegments.

Covers spec case 11.1: SRT numbering, HH:MM:SS,mmm formatting, <=42-char
lines wrapped into at most 2 lines per cue (splitting into extra cues with
time distributed proportionally to characters when needed), VTT's WEBVTT
header and decimal-point timestamps, and shift_s (always explicit, the
measured delay SUBTRACTED from caption-arrival times, negative times clipped
to 0).
"""

from __future__ import annotations

import pytest

from glosa.exports import CONTENT_TYPE, ExportSegment, export_filename, render, slugify, to_srt, to_txt, to_vtt


def test_to_srt_basic_numbering_and_time_format() -> None:
    segs = [ExportSegment(text="Hello world", t_start=1.0, t_end=4.0)]

    out = to_srt(segs, shift_s=0.0)

    assert out == "1\n00:00:01,000 --> 00:00:04,000\nHello world\n"


def test_to_srt_numbers_multiple_segments_sequentially() -> None:
    segs = [
        ExportSegment(text="First", t_start=0.0, t_end=1.0),
        ExportSegment(text="Second", t_start=1.0, t_end=2.0),
    ]

    out = to_srt(segs, shift_s=0.0)

    assert out == (
        "1\n00:00:00,000 --> 00:00:01,000\nFirst\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond\n"
    )


def test_to_srt_wraps_text_into_at_most_two_lines_of_42_chars() -> None:
    # Wraps into exactly two lines, each <= 42 chars, as a single cue (fits
    # the 2-line limit): verified against _wrap_lines directly.
    text = "This sentence needs wrapping across two lines of subtitles"
    segs = [ExportSegment(text=text, t_start=0.0, t_end=5.0)]

    out = to_srt(segs, shift_s=0.0)

    lines = out.strip("\n").split("\n")
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:05,000"
    subtitle_lines = lines[2:]
    assert len(subtitle_lines) == 2
    for line in subtitle_lines:
        assert len(line) <= 42
    assert "\n".join(subtitle_lines).replace("\n", " ") == text


def test_to_srt_splits_into_multiple_cues_with_proportional_timing() -> None:
    # Long enough to need 3 wrapped lines -> 2 cues (2 lines + 1 line), with
    # time split proportionally to each cue's character count: verified
    # against _wrap_lines directly.
    text = (
        "This sentence is quite a bit longer than the others and will "
        "certainly need three lines of text"
    )
    segs = [ExportSegment(text=text, t_start=10.0, t_end=20.0)]

    out = to_srt(segs, shift_s=0.0)
    blocks = out.strip("\n").split("\n\n")
    assert len(blocks) == 2

    first_index, first_time, *first_text_lines = blocks[0].split("\n")
    second_index, second_time, *second_text_lines = blocks[1].split("\n")
    assert (first_index, second_index) == ("1", "2")
    assert len(first_text_lines) == 2
    assert len(second_text_lines) == 1
    for line in [*first_text_lines, *second_text_lines]:
        assert len(line) <= 42

    # Reconstructed text matches the original (nothing lost in the split).
    all_words = " ".join(first_text_lines + second_text_lines).split()
    assert all_words == text.split()

    def parse_srt_time(s: str) -> float:
        hms, ms = s.split(",")
        h, m, sec = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

    first_start, first_end = (parse_srt_time(t) for t in first_time.split(" --> "))
    second_start, second_end = (parse_srt_time(t) for t in second_time.split(" --> "))

    assert first_start == pytest.approx(10.0)
    assert second_end == pytest.approx(20.0)
    # Contiguous: the second cue starts exactly where the first ends.
    assert first_end == pytest.approx(second_start)

    # Proportional to characters: the first cue (2 lines) covers more text
    # than the second (1 line), so it should get more time.
    first_chars = sum(len(line) for line in first_text_lines)
    second_chars = sum(len(line) for line in second_text_lines)
    first_duration = first_end - first_start
    second_duration = second_end - second_start
    total_duration = first_duration + second_duration
    assert first_duration / total_duration == pytest.approx(
        first_chars / (first_chars + second_chars), abs=0.01
    )


def test_to_srt_applies_shift() -> None:
    """Segment times are caption ARRIVAL times (already late by the engine
    latency); shift_s is that measured delay, so cues move EARLIER by it
    (final-review-A I3)."""
    segs = [ExportSegment(text="Shifted", t_start=12.4, t_end=14.4)]

    out = to_srt(segs, shift_s=2.4)

    assert "00:00:10,000 --> 00:00:12,000" in out


def test_to_vtt_word_spoken_at_10s_arriving_at_12_4s_gets_a_cue_at_10s() -> None:
    """The demo video's subtitles are this VTT: a word spoken at t=10 s whose
    caption arrived at 12.4 s (latency 2.4 s) must be cued at ~10 s, not
    at 14.8 s."""
    segs = [ExportSegment(text="Spoken at ten", t_start=12.4, t_end=13.9)]

    out = to_vtt(segs, shift_s=2.4)

    assert "00:00:10.000 --> 00:00:11.500" in out


def test_to_srt_clips_negative_times_to_zero() -> None:
    segs = [ExportSegment(text="Starts early", t_start=-3.0, t_end=1.0)]

    out = to_srt(segs, shift_s=0.0)

    assert "00:00:00,000 --> 00:00:01,000" in out


def test_to_srt_clips_negative_times_after_shift() -> None:
    segs = [ExportSegment(text="Negative shift", t_start=1.0, t_end=3.0)]

    out = to_srt(segs, shift_s=2.5)

    # 1.0 - 2.5 = -1.5 -> clipped to 0; 3.0 - 2.5 = 0.5.
    assert "00:00:00,000 --> 00:00:00,500" in out


def test_to_vtt_has_webvtt_header_and_decimal_point_times() -> None:
    segs = [ExportSegment(text="Hello world", t_start=1.0, t_end=4.0)]

    out = to_vtt(segs, shift_s=0.0)

    assert out.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:04.000" in out
    assert "," not in out.split("\n\n", 1)[1]
    assert "Hello world" in out


def test_to_vtt_applies_shift_and_clips_negative_to_zero() -> None:
    segs = [ExportSegment(text="Clipped", t_start=-1.0, t_end=1.0)]

    out = to_vtt(segs, shift_s=0.0)

    assert "00:00:00.000 --> 00:00:01.000" in out


def test_to_txt_joins_segment_text_one_per_line() -> None:
    segs = [
        ExportSegment(text="First line", t_start=0.0, t_end=1.0),
        ExportSegment(text="Second line", t_start=1.0, t_end=2.0),
    ]

    out = to_txt(segs)

    assert out == "First line\nSecond line\n"


def test_to_txt_empty_segments_returns_empty_string() -> None:
    assert to_txt([]) == ""


def test_to_srt_empty_segments_returns_empty_string() -> None:
    assert to_srt([], shift_s=0.0) == ""


def test_to_vtt_empty_segments_returns_just_header() -> None:
    assert to_vtt([], shift_s=0.0) == "WEBVTT\n"


# ---- render / slugify / export_filename (task-11r-brief.md Ruling 2) -----------


def test_render_dispatches_to_the_matching_format() -> None:
    segs = [ExportSegment(text="Hi", t_start=0.0, t_end=1.0)]
    assert render("srt", segs, shift_s=0.0) == to_srt(segs, shift_s=0.0)
    assert render("vtt", segs, shift_s=0.0) == to_vtt(segs, shift_s=0.0)
    assert render("txt", segs, shift_s=0.0) == to_txt(segs)  # shift_s ignored for txt


def test_render_rejects_an_unknown_format() -> None:
    with pytest.raises(ValueError):
        render("pdf", [], shift_s=0.0)


def test_content_type_covers_every_format() -> None:
    assert set(CONTENT_TYPE) == {"srt", "vtt", "txt"}


def test_slugify_strips_accents_and_punctuation() -> None:
    assert slugify("¿Qué Onda, Kubernetes?!") == "que-onda-kubernetes"


def test_slugify_never_returns_empty() -> None:
    assert slugify("!!!") == "x"


def test_export_filename_matches_the_slug_titulo_lang_version_pattern() -> None:
    name = export_filename("gran-sala", "¿Qué es Kubernetes?", "es", "corrected", "srt")
    assert name == "gran-sala-que-es-kubernetes-es-corrected.srt"
