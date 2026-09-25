"""The text side of a room: which engine a talk gets, which languages it is
captioned in, and the translation lane that turns the source text into
captions for the languages the engine does not produce itself.

Engines (``Talk.engine``)
    ``fast``: Live Translate speaks the talk's language and translates into
    ONE language, the talk's first target (``target_lang``). ``glossary``:
    transcribe-live (verbatim, with the glossary as vocabulary) gives only
    the source text; every target is translated from it. A free session
    (no agenda) gets ``default_engine``: glossary for Spanish, else
    ``Settings.default_engine_en`` (as the agenda import does).

Translation lane
    A LivePipeline (glosa/text/pipeline.py) with the Translator, fed with the
    engine's source text by the RoomWorker, which publishes and stores what
    it delivers. Its targets: every translation language of a glossary talk,
    or the extra ones (after the first) of a fast talk. It is created per
    run and ``close()``d when the run is torn down: that waits (up to
    ``DRAIN_S``) for the pending translations, then cancels whatever is
    left, so no pipeline task outlives its run.

    The glossary of every translation is the talk's glossary at that moment
    (an admin edit of a live talk applies at once).

Fallback (case 10.5, Rulings 48-49)
    A fast talk whose Live Translate keeps failing (``FlapDetector``: 3
    incidents within 2 min) or halts (a non-retryable error, except a
    refused API key) goes on with the glossary engine, hot and for good
    (glosa/room.py).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from glosa.clock import Clock
from glosa.config import SegmenterCfg, Settings
from glosa.models import GlossaryTerm
from glosa.text.pipeline import LivePipeline, TranslatedSegment, TranslateFn
from glosa.text.segmenter import Segmenter
from glosa.text.translator import Translation

EngineKind = Literal["fast", "glossary"]
ENGINES: tuple[EngineKind, ...] = ("fast", "glossary")
DRAIN_S = 10.0  # a run's end waits this long for its pending translations
FALLBACK_INCIDENTS = 3  # Live Translate failures...
FALLBACK_WINDOW_S = 120.0  # ...within this long switch a fast talk to the glossary engine
PAYMENT_CODE = 402


def target_lang(language: str, targets: list[str]) -> str:
    """The translation language: the first target that is not the spoken
    language (Live Translate takes one target per session). With none,
    Spanish, or English for a Spanish talk."""
    for code in targets:
        if code != language:
            return code
    return "en" if language == "es" else "es"


def translation_langs(language: str, targets: list[str]) -> list[str]:
    """Every language a talk is translated into: ``target_lang`` first, then
    the other targets that are not the spoken language, in order."""
    first = target_lang(language, targets)
    return [first, *dict.fromkeys(code for code in targets if code not in (language, first))]


def default_engine(language: str, settings: Settings) -> EngineKind:
    """The engine of a talk that does not say (the free session)."""
    return "glossary" if language == "es" else settings.default_engine_en


def engine_of(talk_engine: str) -> EngineKind:
    return talk_engine if talk_engine in ENGINES else "fast"  # type: ignore[return-value]


class TranslationLane:
    """One run's LivePipeline. ``interim/delta/final/tick`` pass straight to
    it (see LivePipeline); ``close()`` drains it, then closes it."""

    def __init__(
        self,
        *,
        targets: list[str],
        translate: TranslateFn,
        glossary: Callable[[], list[GlossaryTerm]],
        clock: Clock,
        segmenter: SegmenterCfg,
        on_segment: Callable[[TranslatedSegment], Awaitable[None]],
    ) -> None:
        async def translate_now(segment: str, target: str, _: list[GlossaryTerm], context: list[str]) -> Translation:
            return await translate(segment, target, glossary(), context)

        self.targets = list(targets)
        self.pipeline = LivePipeline(
            targets=self.targets,
            translate=translate_now,
            glossary=[],  # read at each call instead: translate_now
            clock=clock,
            segmenter_factory=lambda: Segmenter(**segmenter.model_dump()),
            on_segment=on_segment,
        )
        self.interim = self.pipeline.interim
        self.delta = self.pipeline.delta
        self.final = self.pipeline.final
        self.tick = self.pipeline.tick

    async def close(self, timeout_s: float = DRAIN_S) -> None:
        try:
            await self.pipeline.drain(timeout_s=timeout_s)
        finally:
            await self.pipeline.aclose()


class FlapDetector:
    """Whether an engine keeps failing: ``limit`` incidents within
    ``window_s`` s, read from the relay's cumulative ``stats`` on each
    ``update()`` (the worker's tick).

    An incident is an error (``stats["errors"]``: a failed connect counts
    only there) or an unplanned session replacement (``stats["reconnects"]``:
    a stall, a session that closed on its own). An active session that
    dies with an error shows up in both counters at once, so each update
    counts ``max(new errors, new reconnects)``: two different incidents in
    the same update (0.5 s apart at most) count as one. Not incidents: the
    admin's manual reconnects (``manual``, counted by the worker) and
    payment errors (402), since another engine would not help with an
    exhausted credit; a 402 death of the active session is not counted as
    a reconnect either.
    """

    def __init__(self, limit: int = FALLBACK_INCIDENTS, window_s: float = FALLBACK_WINDOW_S) -> None:
        self.limit = limit
        self.window_s = window_s
        self._reconnects = 0
        self._errors = 0
        self._payment = 0
        self._manual = 0
        self._at: deque[float] = deque()

    def update(self, stats: dict[str, Any], manual: int, now: float) -> bool:
        errors: dict[int, int] = stats["errors"]
        payment = errors.get(PAYMENT_CODE, 0)
        other = sum(errors.values()) - payment
        reconnects = int(stats["reconnects"])
        new_errors = other - self._errors
        new_reconnects = (reconnects - self._reconnects) - (manual - self._manual) - (payment - self._payment)
        self._reconnects, self._errors, self._payment, self._manual = reconnects, other, payment, manual
        for _ in range(max(new_errors, new_reconnects, 0)):
            self._at.append(now)
        while self._at and now - self._at[0] >= self.window_s:
            self._at.popleft()
        return len(self._at) >= self.limit
