"""bench/json3.py: parse YouTube ``json3`` auto-caption files (see
samples/README.md) into word-level timings, and the **progress-lag**
latency metric (Task 15c fix round 1) built on top of them. Pure, no I/O
beyond reading the json3 file; no network.

Progress-lag, in short (see .superpowers/sdd/2026-09-24-glosa/
task-15c-fix1.md for the full rationale): the previous metric
(``match_next``, removed here) paired each reference *utterance end* with
the "earliest unclaimed caption-bus event at or after it" -- with frequent
events (e.g. the glossary engine's fast interim ``set`` updates on the
source track) that measures event density, not latency, and it produced
implausible numbers (a "26 s" source latency where an earlier, independent
measurement put it at ~0.9 s). Progress-lag instead compares two
*cumulative* curves that both only ever go up:

- ``spoken_curve``: S(t), cumulative words spoken by time t (one point per
  json3 word, already offset so t=0 is the clip's start -- see
  samples/README.md).
- ``screen_curve``: C(t), cumulative words visible on a caption track at
  time t, replayed from the recorded ``append``/``set``/``close`` bus
  events (``append`` concatenates onto the segment's current text, ``set``
  *replaces* it -- so a run of ``set`` interim revisions must not be
  double-counted as if each one were new text on top of the last; the
  running max keeps the curve monotone even if an ASR revision briefly
  shortens a segment's text).

Both curves are normalised by their own final total (a translation is not
the same length as its source), then for progress fractions f = 0.05, 0.10,
..., 0.95, ``progress_lag`` reports lag(f) = t_C(f) - t_S(f) -- "how far
behind the screen is once X% of the words have been said/shown" -- as
p50/p90 over those 19 points. A track with no events at all yields
(None, None, 0), rendered as "--" (bench/report.py).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

PROGRESS_POINTS: tuple[float, ...] = tuple(round(0.05 * i, 2) for i in range(1, 20))  # 0.05 .. 0.95


@dataclass(frozen=True)
class Word:
    text: str
    start_s: float


def load_words(path: str | Path) -> list[Word]:
    """Every non-blank word in a json3 file, in speaking order."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    words: list[Word] = []
    for event in data.get("events", []) or []:
        base_ms = event.get("tStartMs") or 0
        for seg in event.get("segs", []) or []:
            text = (seg.get("utf8") or "").strip()
            if not text:
                continue
            start_ms = base_ms + (seg.get("tOffsetMs") or 0)
            words.append(Word(text=text, start_s=start_ms / 1000.0))
    words.sort(key=lambda w: w.start_s)
    return words


def full_text(words: list[Word]) -> str:
    """The whole transcript, words space-joined in speaking order."""
    return " ".join(w.text for w in words)


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 1])."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def spoken_curve(words: list[Word]) -> list[tuple[float, int]]:
    """S(t): cumulative words spoken by time t -- one point per word (in
    speaking order), ``(word.start_s, index + 1)``. Already monotone (each
    word adds exactly one)."""
    return [(w.start_s, i + 1) for i, w in enumerate(words)]


def screen_curve(events: list[dict], lang: str) -> list[tuple[float, int]]:
    """C(t): cumulative words visible on ``lang``'s caption track at time
    t, replayed from ``events`` (bench/bench.py's recorded CaptionBus
    messages: dicts with ``t``/``lang``/``type``/``seg``/``text``).
    ``append`` concatenates onto its segment's current text; ``set``
    *replaces* it (a segment's whole current text, per glosa/room.py's
    ``_set_source`` -- counting it on top of the previous ``set`` would
    double-count words already on screen); ``close`` carries no text and
    only ever finalises a segment already accounted for, so it adds no
    point. One point per append/set event, on the running max of the total
    word count across all of ``lang``'s segments so far (keeps the curve
    monotone even when a ``set`` revises a segment's text shorter, e.g. an
    ASR hypothesis correction)."""
    seg_text: dict[object, str] = {}
    running_max = 0
    points: list[tuple[float, int]] = []
    for event in sorted(events, key=lambda e: e.get("t", 0.0)):
        if event.get("lang") != lang:
            continue
        etype = event.get("type")
        if etype == "append":
            seg_text[event.get("seg")] = seg_text.get(event.get("seg"), "") + (event.get("text") or "")
        elif etype == "set":
            seg_text[event.get("seg")] = event.get("text") or ""
        else:
            continue
        total_words = sum(len(text.split()) for text in seg_text.values())
        running_max = max(running_max, total_words)
        points.append((event["t"], running_max))
    return points


def time_at_fraction(curve: list[tuple[float, int]], fraction: float) -> float | None:
    """The earliest t in ``curve`` (a non-decreasing-count series, e.g.
    from ``spoken_curve``/``screen_curve``) at which the cumulative count
    is >= ``fraction`` of the curve's own final (max) value. ``None`` if
    ``curve`` is empty or its final value is 0 (no words counted)."""
    if not curve:
        return None
    total = curve[-1][1]
    if total <= 0:
        return None
    threshold = fraction * total
    for t, count in curve:
        if count >= threshold:
            return t
    return curve[-1][0]  # unreachable for fraction <= 1: curve[-1]'s count == total


def progress_lag(
    spoken: list[tuple[float, int]], screen: list[tuple[float, int]]
) -> tuple[float | None, float | None, int]:
    """p50/p90/n progress-lag (see module docstring): lag(f) =
    t_screen(f) - t_spoken(f) for f in ``PROGRESS_POINTS``, each curve's
    fractions taken against its own final total. (None, None, 0) if either
    curve is empty (a track with no events at all)."""
    if not spoken or not screen:
        return None, None, 0
    lags: list[float] = []
    for f in PROGRESS_POINTS:
        t_spoken = time_at_fraction(spoken, f)
        t_screen = time_at_fraction(screen, f)
        if t_spoken is None or t_screen is None:
            continue
        lags.append(t_screen - t_spoken)
    if not lags:
        return None, None, 0
    return pct(lags, 0.5), pct(lags, 0.9), len(lags)
