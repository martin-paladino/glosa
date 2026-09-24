"""Tests for CaptionAssembler: turns engine text deltas into append/close
caption events, closing a segment on terminal punctuation, on a pause, or
after max_chars (at a word boundary).
"""

from __future__ import annotations

from glosa.captions.assembler import CaptionAssembler


def test_delta_without_punctuation_only_appends() -> None:
    asm = CaptionAssembler()

    events = asm.on_delta("Hola", t=0.0)

    assert events == [("append", {"seg": 0, "text": "Hola"})]


def test_delta_with_terminal_punctuation_appends_then_closes() -> None:
    asm = CaptionAssembler()
    asm.on_delta("Hola", t=0.0)

    events = asm.on_delta(" mundo.", t=0.5)

    assert events == [
        ("append", {"seg": 0, "text": " mundo."}),
        ("close", {"seg": 0}),
    ]


def test_full_sentence_sequence_is_append_append_close() -> None:
    asm = CaptionAssembler()

    first = asm.on_delta("Hola", t=0.0)
    second = asm.on_delta(" mundo.", t=0.5)

    kinds = [kind for kind, _ in first] + [kind for kind, _ in second]
    assert kinds == ["append", "append", "close"]


def test_pause_closes_open_segment_without_punctuation() -> None:
    asm = CaptionAssembler()
    asm.on_delta("texto sin punto final", t=0.0)

    events = asm.on_pause(t=1.0)

    assert events == [("close", {"seg": 0})]


def test_pause_with_no_open_segment_returns_nothing() -> None:
    asm = CaptionAssembler()

    events = asm.on_pause(t=1.0)

    assert events == []


def test_segment_number_increments_after_close() -> None:
    asm = CaptionAssembler()
    asm.on_delta("Primero.", t=0.0)

    events = asm.on_delta("Segundo", t=1.0)

    assert events == [("append", {"seg": 1, "text": "Segundo"})]


def test_long_delta_without_punctuation_closes_at_word_boundary() -> None:
    asm = CaptionAssembler(max_chars=200)
    long_text = "word " * 50  # 250 chars, no terminal punctuation

    events = asm.on_delta(long_text, t=0.0)

    # It must close (max_chars exceeded) and never split a word in two.
    kinds = [kind for kind, _ in events]
    assert "close" in kinds

    close_index = kinds.index("close")
    appended_before_close = "".join(
        payload["text"] for kind, payload in events[: close_index + 1] if kind == "append"
    )
    assert len(appended_before_close) <= 200
    # No word is split: the closed segment's text ends with a full "word", not a fragment.
    assert appended_before_close.rstrip().endswith("word")

    # Anything left over after the boundary continues in a new segment.
    total_appended = "".join(payload["text"] for kind, payload in events if kind == "append")
    assert total_appended.replace(" ", "") == long_text.replace(" ", "")
