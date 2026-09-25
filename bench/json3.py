"""bench/json3.py: parse YouTube ``json3`` auto-caption files (see
samples/README.md) into word-level timings, and derive the reference
"utterance end" timestamps latency is measured against, plus a small
two-pointer matcher pairing those reference times to caption-bus arrival
times. Pure, no I/O beyond reading the json3 file; no network.

Why "utterance end" is estimated, not read directly: json3 gives each
word's *start* time (``event.tStartMs + seg.tOffsetMs``) but never a
duration, so a word's end is not directly available. ``utterances()``
estimates it: a word's own end is its start plus the gap to the next word,
capped at ``word_cap_s`` (a plausible max spoken-word length; the excess,
if any, is silence). A new utterance starts whenever that gap reaches
``gap_s`` (a pause at least this long looks like a sentence/thought
boundary, not just inter-word spacing) -- this mirrors, but is independent
of, the room's own VAD pause (glosa/audio/vad.py, ~400 ms hangover): using
the *reference* transcript's own timing keeps the benchmark's latency
numbers grounded in when the speaker actually stopped talking, not in the
system-under-test's own idea of when that happened.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GAP_S = 0.5  # min. inter-word gap treated as an utterance boundary
DEFAULT_WORD_CAP_S = 0.35  # max. assumed duration of a single spoken word


@dataclass(frozen=True)
class Word:
    text: str
    start_s: float


@dataclass(frozen=True)
class Utterance:
    text: str
    end_s: float  # estimated moment speech stops, see module docstring


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


def utterances(
    words: list[Word], gap_s: float = DEFAULT_GAP_S, word_cap_s: float = DEFAULT_WORD_CAP_S
) -> list[Utterance]:
    """Group ``words`` into utterances at gaps >= ``gap_s``; each
    utterance's ``end_s`` is its last word's estimated end (see module
    docstring)."""
    if not words:
        return []
    ends: list[float] = []
    for i, word in enumerate(words):
        gap = (words[i + 1].start_s - word.start_s) if i + 1 < len(words) else word_cap_s
        ends.append(word.start_s + min(max(gap, 0.0), word_cap_s))

    out: list[Utterance] = []
    group_texts = [words[0].text]
    group_end = ends[0]
    for i in range(1, len(words)):
        gap = words[i].start_s - words[i - 1].start_s
        if gap >= gap_s:
            out.append(Utterance(text=" ".join(group_texts), end_s=group_end))
            group_texts = []
        group_texts.append(words[i].text)
        group_end = ends[i]
    out.append(Utterance(text=" ".join(group_texts), end_s=group_end))
    return out


def match_next(ref_ends: list[float], event_times: list[float]) -> list[float | None]:
    """Pair each (sorted) reference time with the earliest ``event_times``
    value at or after it that no earlier reference already claimed; the
    latency for a matched pair is ``event_time - ref_time``. A reference
    with nothing left to claim (e.g. the clip's last utterance, if no
    caption event followed it before the run ended) gets ``None``."""
    events = sorted(event_times)
    refs = sorted(ref_ends)
    j = 0
    out: list[float | None] = []
    for t in refs:
        while j < len(events) and events[j] < t:
            j += 1
        if j < len(events):
            out.append(events[j] - t)
            j += 1
        else:
            out.append(None)
    return out


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 1])."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def latency_stats(ref_ends: list[float], event_times: list[float]) -> tuple[float | None, float | None, int]:
    """p50/p90/n latency of ``event_times`` (bus-message arrival times) after
    ``ref_ends`` (reference utterance-end times), via ``match_next``. Both
    the "source" and "translation" latency numbers in bench/bench.py are
    this same computation applied to a different track's event times --
    (None, None, 0) when either list is empty (e.g. the "fast" engine's
    source track, which never publishes anything: see bench/bench.py)."""
    if not ref_ends or not event_times:
        return None, None, 0
    deltas = [d for d in match_next(ref_ends, event_times) if d is not None]
    if not deltas:
        return None, None, 0
    return pct(deltas, 0.5), pct(deltas, 0.9), len(deltas)
