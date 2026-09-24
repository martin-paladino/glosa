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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from glosa.clock import Clock
from glosa.config import SegmenterCfg, Settings
from glosa.models import GlossaryTerm
from glosa.text.pipeline import LivePipeline, TranslatedSegment, TranslateFn
from glosa.text.segmenter import Segmenter
from glosa.text.translator import Translation

EngineKind = Literal["fast", "glossary"]
ENGINES: tuple[EngineKind, ...] = ("fast", "glossary")
DRAIN_S = 10.0  # a run's end waits this long for its pending translations


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
