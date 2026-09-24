"""Segmenter: the P1 caption-segmentation heuristic (globals.md
"Segmentador P1"). It turns a running transcript (fed incrementally as text
arrives, e.g. from Engine source_delta/source_final events) into short
segments that are cheap and fast to hand off to the Translator.

Cut rules, in the order they're checked as each character arrives:
  - terminal punctuation (. ? ! ; :) closes the current segment, except a
    "." followed right away (no space) by a letter or digit in the same
    text: "3.5", "Node.js", "k8s.io" are one token. A "." that ends the
    text fed so far still cuts at once (what follows is not known yet;
    waiting would delay every sentence end);
  - a comma only closes it once the segment already has at least
    comma_min_words words (so short lead-ins like "So," aren't cut early);
  - reaching max_words words forces a close even with no punctuation.
A segment can also be force-closed by elapsed time: call tick(t)
periodically (there is no background timer here) so a segment open for
>= max_wait_s gets closed even if nothing above ever triggers. flush()
force-closes whatever is open, e.g. at end of stream.
"""

from __future__ import annotations

_TERMINAL_PUNCTUATION = ".?!;:"


class Segmenter:
    """Stateful per-utterance-stream segmenter.

    feed()/tick()/flush() are meant to be called in arrival order for a
    single running transcript; an instance is not safe to share across
    independent transcript streams or across threads/tasks.
    """

    def __init__(
        self,
        comma_min_words: int = 5,
        max_words: int = 14,
        max_wait_s: float = 3.0,
    ) -> None:
        self.comma_min_words = comma_min_words
        self.max_words = max_words
        self.max_wait_s = max_wait_s

        self._buf = ""
        self._seg_start_t: float | None = None

    def _word_count(self) -> int:
        return len(self._buf.split())

    def _cut(self) -> str:
        segment = self._buf.strip()
        self._buf = ""
        self._seg_start_t = None
        return segment

    def feed(self, text: str, t: float) -> list[str]:
        """Consume one chunk of newly arrived text, returning any segments
        it closed (in order; a single call can close more than one, e.g. a
        final transcript containing several sentences).
        """
        completed: list[str] = []

        for i, ch in enumerate(text):
            if not self._buf and ch.isspace():
                continue  # never start a segment with whitespace
            if not self._buf:
                self._seg_start_t = t
            self._buf += ch

            if ch == "." and i + 1 < len(text) and text[i + 1].isalnum():
                continue  # inside a token: "3.5", "Node.js"

            if ch in _TERMINAL_PUNCTUATION:
                segment = self._cut()
                if segment:
                    completed.append(segment)
                continue

            if ch == "," and self._word_count() >= self.comma_min_words:
                segment = self._cut()
                if segment:
                    completed.append(segment)
                continue

            if ch.isspace() and self._word_count() >= self.max_words:
                segment = self._cut()
                if segment:
                    completed.append(segment)

        return completed

    def tick(self, t: float) -> list[str]:
        """Force-close the open segment if it has been open for at least
        max_wait_s (relative to when it started). Call periodically; a
        no-op when nothing is open or the timeout hasn't elapsed yet.
        """
        if self._seg_start_t is None:
            return []
        if t - self._seg_start_t >= self.max_wait_s:
            segment = self._cut()
            return [segment] if segment else []
        return []

    def flush(self) -> list[str]:
        """Force-close whatever is open, regardless of word count or
        elapsed time (e.g. at end of stream).
        """
        segment = self._cut()
        return [segment] if segment else []
