"""Pure export generation: turn a talk's segments into SRT, VTT or TXT text.

ExportSegment is the plain input shape (no dependency on captions/bus.py or
db.py): a flat list of (text, t_start, t_end) with times in seconds since
the start of the talk. Wiring this up to real stored segments (live vs
"corrected" versions) is done elsewhere (room.py / web/public_api.py), not
here.

Line wrapping: SRT/VTT cues are limited to 2 lines of at most 42 characters
each (a conventional subtitle readability limit, given verbatim in the
brief). A segment's text is greedily word-wrapped into lines of that width;
when it needs more than 2 lines, it is split into consecutive cues of (up
to) 2 lines each, and the segment's [t_start, t_end] duration is divided
between those cues in proportion to each cue's character count.

shift_s (Ruling 6) is always explicit - there is no default. It is the
measured caption delay: segment times are caption ARRIVAL times (already late
by the engine latency), so shift_s is SUBTRACTED from every timestamp to move
cues back onto the speech (final-review-A I3); the result is clipped to 0
(subtitles can't start before the file does).

render()/CONTENT_TYPE/slugify()/export_filename() (task-11r-brief.md Ruling 2)
are the small pieces glosa/web/public_api.py's GET /exports/{talk_id}/{lang}.{fmt}
route needs on top of to_srt/to_vtt/to_txt: a format-keyed dispatcher (to_txt
alone takes no shift_s -- a plain transcript has no timestamps to shift), the
response Content-Type per format, and the "slug-titulo-lang-version.ext"
filename the route sets in Content-Disposition.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_MAX_LINE_CHARS = 42
_MAX_LINES_PER_CUE = 2


@dataclass
class ExportSegment:
    text: str
    t_start: float
    t_end: float


@dataclass
class _Cue:
    lines: list[str]
    t_start: float
    t_end: float


def _wrap_lines(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """Greedy word-wrap. A single word longer than max_chars is kept whole
    on its own line (subtitles don't break mid-word)."""
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _shift_and_clip(seg: ExportSegment, shift_s: float) -> tuple[float, float]:
    return max(0.0, seg.t_start - shift_s), max(0.0, seg.t_end - shift_s)


def _segment_to_cues(seg: ExportSegment, shift_s: float) -> list[_Cue]:
    t_start, t_end = _shift_and_clip(seg, shift_s)
    lines = _wrap_lines(seg.text)
    chunks = [lines[i : i + _MAX_LINES_PER_CUE] for i in range(0, len(lines), _MAX_LINES_PER_CUE)]

    if len(chunks) <= 1:
        return [_Cue(lines=chunks[0] if chunks else [""], t_start=t_start, t_end=t_end)]

    weights = [sum(len(line) for line in chunk) or 1 for chunk in chunks]
    total_weight = sum(weights)
    duration = t_end - t_start

    cues: list[_Cue] = []
    t = t_start
    for i, (chunk, weight) in enumerate(zip(chunks, weights)):
        if i == len(chunks) - 1:
            cue_end = t_end
        else:
            cue_end = t + duration * (weight / total_weight)
        cues.append(_Cue(lines=chunk, t_start=t, t_end=cue_end))
        t = cue_end
    return cues


def _format_time(t: float, decimal_sep: str) -> str:
    total_ms = max(0, round(t * 1000))
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    seconds, millis = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{decimal_sep}{millis:03d}"


def _all_cues(segs: list[ExportSegment], shift_s: float) -> list[_Cue]:
    cues: list[_Cue] = []
    for seg in segs:
        cues.extend(_segment_to_cues(seg, shift_s))
    return cues


def to_srt(segs: list[ExportSegment], shift_s: float) -> str:
    """Render segs as SubRip (.srt) text. shift_s is required (Ruling 6)."""
    blocks = []
    for index, cue in enumerate(_all_cues(segs, shift_s), start=1):
        time_line = f"{_format_time(cue.t_start, ',')} --> {_format_time(cue.t_end, ',')}"
        blocks.append(f"{index}\n{time_line}\n" + "\n".join(cue.lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def to_vtt(segs: list[ExportSegment], shift_s: float) -> str:
    """Render segs as WebVTT (.vtt) text. shift_s is required (Ruling 6)."""
    blocks = []
    for cue in _all_cues(segs, shift_s):
        time_line = f"{_format_time(cue.t_start, '.')} --> {_format_time(cue.t_end, '.')}"
        blocks.append(f"{time_line}\n" + "\n".join(cue.lines))
    if not blocks:
        return "WEBVTT\n"
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def to_txt(segs: list[ExportSegment]) -> str:
    """Render segs as a plain-text transcript, one segment per line.

    No shift_s: a plain transcript has no timestamps to align.
    """
    if not segs:
        return ""
    return "\n".join(seg.text for seg in segs) + "\n"


CONTENT_TYPE = {
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "txt": "text/plain; charset=utf-8",
}


def render(fmt: str, segs: list[ExportSegment], shift_s: float) -> str:
    """Dispatch to to_srt/to_vtt/to_txt by extension ("srt"|"vtt"|"txt"):
    what glosa/web/public_api.py's export route needs given just the {fmt}
    path parameter. Raises ValueError for any other fmt (the route turns
    that into a 404, same as an unknown talk/lang)."""
    if fmt == "srt":
        return to_srt(segs, shift_s=shift_s)
    if fmt == "vtt":
        return to_vtt(segs, shift_s=shift_s)
    if fmt == "txt":
        return to_txt(segs)
    raise ValueError(f"unknown export format: {fmt!r}")


def slugify(text: str) -> str:
    """ASCII, lowercase, hyphen-separated slug for a filename component
    (accents and punctuation stripped, runs of anything else collapsed to
    one hyphen); "x" for text that slugifies to nothing."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "x"


def export_filename(room_slug: str, title: str, lang: str, version: str, fmt: str) -> str:
    """"slug-titulo-lang-version.ext" (task-11r-brief.md Ruling 2)."""
    return f"{slugify(room_slug)}-{slugify(title)}-{lang}-{version}.{fmt}"
