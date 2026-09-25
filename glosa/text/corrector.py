"""Corrector: re-translates a talk's ORIGINAL source segments in blocks with
gemini-3.8-flash (thinking_level="LOW"; gemini-3.8-flash does not accept
MINIMAL, per globals.md), guided by the talk's glossary and abstract, to
build the "corrected" (post-talk) translation. task-11-brief.md's
build_corrected (Task 11-rest) saves the result with version="corrected" and
the SAME timings as the live segments -- which only works if this module
returns exactly one entry per input segment, in order.

The glossary line format (`_build_system_instruction`) matches
glosa/text/translator.py's (task-11r-brief.md's closing note, 223df70): the
Translator no longer emits the old "term → keep" line, and this module must
not either, or a stray "keep" could leak into the corrected export text.

Blocks are `block_size` sentences (default 20, per task-11a-brief.md). Each
block after the first carries the last 1-2 ORIGINAL sentences of the
previous block as read-only context (mirrors Translator's `context` in
glosa/text/translator.py): shown to the model for continuity, but never to
be translated or echoed back.

Return type: task-11a-brief.md gives correct_segments's signature as
`-> list[str]`, but its own next sentence requires a per-block fallback ("Si
el modelo devuelve otra cantidad para un bloque ... devolve None para ese
bloque"). Both requirements can only hold together if the return value is
list[str | None]: same length and order as `sources` always (so timings stay
aligned), with None marking a segment whose block failed even after a retry
(the caller -- Task 11-rest's build_corrected -- keeps the live version for
those). See task-11a-report.md's self-review for this deliberate,
documented deviation from the literal `list[str]` annotation.

Cost: usd_total accumulates usage_metadata-derived cost across every block
call (successful or not -- tokens are billed either way), using
price_in_per_m/price_out_per_m injected at construction (same pattern as
glosa/text/translator.py's Translator._cost_usd).

The genai client can be injected (`client=`) for tests, mirroring
glosa/text/translator.py; production code leaves it unset and Corrector
builds a real genai.Client(api_key=...).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from glosa.models import GlossaryTerm

if TYPE_CHECKING:
    from glosa.db import Database, Segment

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2  # 1 try + 1 retry per block, per task-11a-brief.md
_CONTEXT_SENTENCES = 2


class _BlockOut(BaseModel):
    translations: list[str]


def _build_system_instruction(
    source_lang: str,
    target_lang: str,
    glossary: list[GlossaryTerm],
    abstract: str,
    context: list[str],
) -> str:
    lines = [
        f"You are producing a polished, corrected {target_lang} translation of a "
        f"conference talk originally spoken in {source_lang}, for subtitle export. "
        "Retranslate ONLY the numbered sentences given by the user, from "
        f"{source_lang} to {target_lang}, one translation per sentence, same order.",
        'Reply as JSON: {"translations": [...]} with EXACTLY one string per input '
        "sentence -- never merge, split, omit, or add sentences.",
    ]
    if abstract:
        lines.append("")
        lines.append(f"Talk abstract (context only): {abstract}")
    if glossary:
        lines.append("")
        # Same wording as glosa/text/translator.py's _build_system_instruction
        # (223df70): the Translator moved off the old "term → keep" format,
        # and this module must match it -- otherwise "keep" (a literal English
        # word meaning "leave untranslated") could get echoed into the
        # corrected export whenever the model treats the glossary line as
        # part of the text, instead of being understood as an instruction.
        lines.append(
            "Glossary. Use an entry only when its term, or an obvious inflection of it, appears in the "
            "sentences you are translating; then apply it exactly. Never add a glossary term that is not "
            "in the sentences, and never use one to replace a different word (e.g. do not turn a plain "
            "noun into a glossary term):"
        )
        for term in glossary:
            if term.keep_in_english or not term.translation:
                lines.append(f'- "{term.term}": leave it as is, untranslated')
            else:
                lines.append(f'- "{term.term}": translate it as "{term.translation}"')
    if context:
        lines.append("")
        lines.append(
            "Previous sentences (read-only context/continuity only; do NOT "
            "translate or include them in the output):"
        )
        for sentence in context:
            lines.append(f"- {sentence}")
    return "\n".join(lines)


def _format_block(block: list[str]) -> str:
    return "\n".join(f"{i}. {sentence}" for i, sentence in enumerate(block, start=1))


class Corrector:
    """Each call to correct() is independent: no mutable state is shared
    across calls other than the (reusable) client, model configuration and
    the running usd_total.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-flash",
        *,
        block_size: int = 20,
        price_in_per_m: float = 0.50,
        price_out_per_m: float = 3.00,
        # gemini-3.8-flash pricing wasn't listed in
        # transcripcion-v0/notas/modelos-gemini.md (only latency/quality
        # notes, no $/M tokens) -- 0.50/3.00 is the task-11a-brief.md
        # fallback placeholder, not a verified price.
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.block_size = block_size
        self.price_in_per_m = price_in_per_m
        self.price_out_per_m = price_out_per_m
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self.usd_total = 0.0

    def _cost_usd(self, usage_metadata: Any) -> float:
        if usage_metadata is None:
            return 0.0
        input_tokens = getattr(usage_metadata, "prompt_token_count", None) or 0
        output_tokens = (getattr(usage_metadata, "candidates_token_count", None) or 0) + (
            getattr(usage_metadata, "thoughts_token_count", None) or 0
        )
        return (input_tokens * self.price_in_per_m + output_tokens * self.price_out_per_m) / 1_000_000

    async def _call_block(
        self,
        block: list[str],
        *,
        source_lang: str,
        target_lang: str,
        glossary: list[GlossaryTerm],
        abstract: str,
        context: list[str],
    ) -> list[str] | None:
        config = types.GenerateContentConfig(
            system_instruction=_build_system_instruction(source_lang, target_lang, glossary, abstract, context),
            response_mime_type="application/json",
            response_schema=_BlockOut,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=_format_block(block),
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - a block failure falls back to None, see module docstring
            logger.info("correct_segments: API call failed for a block: %s", exc)
            return None

        self.usd_total += self._cost_usd(getattr(response, "usage_metadata", None))

        try:
            parsed = _BlockOut.model_validate(json.loads(response.text))
        except Exception as exc:  # noqa: BLE001
            logger.info("correct_segments: invalid JSON for a block: %s", exc)
            return None
        return parsed.translations

    async def correct(
        self,
        sources: list[str],
        *,
        source_lang: str,
        target_lang: str,
        glossary: list[GlossaryTerm],
        abstract: str,
    ) -> list[str | None]:
        out: list[str | None] = []
        for start in range(0, len(sources), self.block_size):
            block = sources[start : start + self.block_size]
            context = sources[max(0, start - _CONTEXT_SENTENCES) : start]

            result: list[str] | None = None
            for _attempt in range(_MAX_ATTEMPTS):
                result = await self._call_block(
                    block,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    glossary=glossary,
                    abstract=abstract,
                    context=context,
                )
                if result is not None and len(result) == len(block):
                    break
                result = None

            if result is None:
                logger.warning(
                    "correct_segments: giving up on block [%d:%d] (size %d) after %d attempt(s); "
                    "the caller should keep the live version for these segments",
                    start,
                    start + len(block),
                    len(block),
                    _MAX_ATTEMPTS,
                )
                out.extend([None] * len(block))
            else:
                out.extend(result)

        return out


async def correct_segments(
    sources: list[str],
    *,
    source_lang: str,
    target_lang: str,
    glossary: list[GlossaryTerm],
    abstract: str,
    api_key: str,
    model: str = "gemini-3.8-flash",
    block_size: int = 20,
) -> list[str | None]:
    """Retranslate sources (the talk's ORIGINAL text) in blocks with
    gemini-3.8-flash. See Corrector for the testable, client-injectable
    class this delegates to, and the module docstring for why the return
    type is list[str | None] rather than task-11a-brief.md's literal
    list[str].
    """
    corrector = Corrector(api_key, model=model, block_size=block_size)
    return await corrector.correct(
        sources,
        source_lang=source_lang,
        target_lang=target_lang,
        glossary=glossary,
        abstract=abstract,
    )


def _fallback_text(dest_live: list["Segment"], t_start: float, t_end: float) -> str:
    """The text build_corrected keeps for a source segment whose block
    failed even after a retry (correct_segments returned None for it):
    task-11r-brief.md Ruling 1 says to "copy the live text", but the source
    and destination segments are not 1:1 by id (the glossary engine cuts by
    the Segmenter; Live Translate produces its own destination text -- see
    the module docstring and build_corrected below), so there is no single
    destination segment that "is" this source segment's translation.

    This module's reading (documented here per the ruling's own
    "Documentalo"): the destination's own (uncorrected) live segments whose
    time range overlaps [t_start, t_end] the most, concatenated in order;
    if none overlaps, the closest one by t_start; "" if the destination has
    no live segments at all for this talk/lang (e.g. Live Translate never
    produced output there).
    """
    if not dest_live:
        return ""
    overlapping = [s for s in dest_live if s.t_start < t_end and s.t_end > t_start]
    if overlapping:
        return " ".join(s.text for s in overlapping)
    nearest = min(dest_live, key=lambda s: abs(s.t_start - t_start))
    return nearest.text


async def build_corrected(
    talk_id: str,
    lang: str,
    *,
    db: "Database",
    api_key: str,
    model: str = "gemini-3.8-flash",
    block_size: int = 20,
    client: Any | None = None,
) -> str:
    """Task-11r-brief.md Ruling 1: rebuild talk_id's "corrected" export for
    one target language ``lang`` (never the talk's own spoken language, and
    never a free session -- the caller, glosa/web/app.py's on_talk_end hook,
    enforces that before calling this).

    Reads the talk's ORIGINAL source segments (version="live", the talk's
    own language) and re-translates them with correct_segments (blocks of
    ``block_size``, guided by the talk's glossary and abstract), then saves
    one segment per SOURCE segment, in ``lang``, version="corrected", with
    the SAME [t_start, t_end] as the source segment it came from -- not the
    destination's own live segments, which the Ruling explicitly says not
    to build from (they don't line up 1:1 with the source: the glossary
    engine cuts by the Segmenter, Live Translate produces its own
    destination text). Where correct_segments returns None for a source
    segment (its block failed even after a retry), the fallback is the
    destination's own live text for that time range -- see _fallback_text's
    docstring for exactly how, and why that's this module's reading of
    Ruling 1's "copy the live text".

    Any previously stored "corrected" segments for (talk_id, lang) are
    cleared first, so re-running this (e.g. Ruling 5's stale-boot retry) is
    idempotent rather than appending duplicates.

    Status: writes db's exports row "pending" at the start and "ready" or
    "failed" at the end (glosa.db Database.set_export_status), and returns
    that final status ("ready"/"failed") so the caller (the on_talk_end
    hook) knows which admin event to publish -- never raises: a missing
    talk, empty source segments, or any correct_segments/db failure is
    caught, logged, and reported as "failed" (mirrors
    glosa/text/glossary.py's "an optional, background nicety must never
    break its caller").
    """
    await db.set_export_status(talk_id, lang, "pending")
    try:
        talk = await db.get_talk(talk_id)
        if talk is None:
            raise LookupError(f"no talk {talk_id!r}")
        sources = await db.get_segments(talk_id, talk.language, "live")
        dest_live = await db.get_segments(talk_id, lang, "live")
        if sources:
            corrector = Corrector(api_key, model=model, block_size=block_size, client=client)
            translations = await corrector.correct(
                [s.text for s in sources],
                source_lang=talk.language,
                target_lang=lang,
                glossary=talk.glossary,
                abstract=talk.abstract,
            )
        else:
            translations = []
        await db.delete_segments(talk_id, lang, "corrected")
        for src, text in zip(sources, translations, strict=True):
            final_text = text if text is not None else _fallback_text(dest_live, src.t_start, src.t_end)
            await db.save_segment(talk_id, talk.room_id, lang, "translation", "corrected", final_text, src.t_start, src.t_end)
    except Exception:
        logger.exception("build_corrected: talk %r lang %r failed", talk_id, lang)
        await db.set_export_status(talk_id, lang, "failed")
        return "failed"
    await db.set_export_status(talk_id, lang, "ready")
    return "ready"
