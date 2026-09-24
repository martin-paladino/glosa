"""CaptionAssembler: turns raw engine text deltas into caption segment
events (append/close) for a single (room, language) caption track.

A segment closes when:
  - it reaches terminal punctuation (. ? !),
  - a pause is reported (on_pause), or
  - it would exceed max_chars: it closes at the last word boundary at or
    before the limit, and any leftover text starts a new segment.
"""

from __future__ import annotations

_TERMINAL_PUNCTUATION = ".?!"


class CaptionAssembler:
    """Stateful assembler for one caption track.

    Not thread-safe / not shared across tracks: callers create one instance
    per (room_id, lang) they are assembling.
    """

    def __init__(self, max_chars: int = 200) -> None:
        self.max_chars = max_chars
        self._seg = -1
        self._open = False
        self._buf = ""

    def _open_segment(self) -> None:
        self._seg += 1
        self._open = True
        self._buf = ""

    def on_delta(self, text: str, t: float) -> list[tuple[str, dict]]:
        """Consume one text delta, returning append/close events in order."""
        events: list[tuple[str, dict]] = []
        remaining = text

        while remaining:
            if not self._open:
                self._open_segment()

            eos_index = next(
                (i for i, ch in enumerate(remaining) if ch in _TERMINAL_PUNCTUATION),
                None,
            )
            if eos_index is not None:
                chunk = remaining[: eos_index + 1]
                if len(self._buf) + len(chunk) <= self.max_chars:
                    self._buf += chunk
                    events.append(("append", {"seg": self._seg, "text": chunk}))
                    events.append(("close", {"seg": self._seg}))
                    self._open = False
                    remaining = remaining[eos_index + 1 :]
                    continue
                # Falls through: the sentence up to punctuation doesn't fit;
                # max_chars wins and we cut at a word boundary instead.

            available = self.max_chars - len(self._buf)
            if available <= 0:
                # Nothing more fits; close with no further content.
                events.append(("close", {"seg": self._seg}))
                self._open = False
                continue

            if len(remaining) <= available:
                self._buf += remaining
                events.append(("append", {"seg": self._seg, "text": remaining}))
                remaining = ""
                continue

            # remaining is longer than the room left in this segment: cut at
            # the last space that still fits, *including* the space itself
            # in this segment's chunk (mirrors the punctuation path, which
            # includes the punctuation char) so a delta that starts with a
            # space closes cleanly on it instead of hard-cutting into the
            # following word. cut == 0 is a valid boundary (the delta's own
            # leading space) and must not be confused with "not found".
            space_index = remaining.rfind(" ", 0, available)
            if space_index == -1:
                # No space at all within budget: forced hard cut. Only
                # reachable when a single token is longer than max_chars.
                cut = available
            else:
                cut = space_index + 1
            chunk = remaining[:cut]
            self._buf += chunk
            events.append(("append", {"seg": self._seg, "text": chunk}))
            events.append(("close", {"seg": self._seg}))
            self._open = False
            remaining = remaining[cut:]

        return events

    def on_pause(self, t: float) -> list[tuple[str, dict]]:
        """Close the currently open segment, if any."""
        if not self._open:
            return []
        seg = self._seg
        self._open = False
        return [("close", {"seg": seg})]
