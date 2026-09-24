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

            cut = remaining.rfind(" ", 0, available + 1)
            if cut <= 0:
                cut = available  # no word boundary in range: hard cut
            chunk = remaining[:cut]
            self._buf += chunk
            events.append(("append", {"seg": self._seg, "text": chunk}))
            events.append(("close", {"seg": self._seg}))
            self._open = False
            remaining = remaining[cut:].lstrip(" ")

        return events

    def on_pause(self, t: float) -> list[tuple[str, dict]]:
        """Close the currently open segment, if any."""
        if not self._open:
            return []
        seg = self._seg
        self._open = False
        return [("close", {"seg": seg})]
