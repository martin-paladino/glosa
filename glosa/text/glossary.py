"""GlossarySuggester: suggests a glossary of technical terms for a talk with
gemini-3.8-flash (thinking_level="LOW"; gemini-3.8-flash does not accept
MINIMAL, per globals.md), from the talk's title/abstract/tags/language and
translation targets (glosa/models.py Talk).

Structured output (response_schema) constrains the model to JSON matching
_GlossaryOut; the response text is then parsed and validated by hand too
(never trusted blindly), which keeps this module's tests simple (a fake
client just needs to return .text) and robust whether or not the API honors
response_schema. Invalid/unparseable JSON, or any other error talking to the
API -> one retry -> [] plus a logged warning: suggesting a glossary is an
optional, admin-triggered nicety (task-11-brief.md's
POST /api/admin/talks/{id}/suggest-glossary, wired up in Task 11-rest), so
this function must never raise.

GlossaryTerm (glosa/models.py) carries a single `translation` field, not one
per target language, so a multi-target talk gets a best-effort translation
into talk.targets[0] only (its primary destination); keep_in_english applies
regardless of target. A talk with no targets gets every term
keep_in_english=True.

The genai client can be injected (`client=`), which is how tests fake it
without spending API budget -- mirrors glosa/text/translator.py exactly;
production code leaves it unset and GlossarySuggester builds a real
genai.Client(api_key=...).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from glosa.models import GlossaryTerm, Talk

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2  # 1 try + 1 retry, per task-11a-brief.md


class _TermOut(BaseModel):
    term: str
    keep_in_english: bool
    translation: str | None = None


class _GlossaryOut(BaseModel):
    terms: list[_TermOut]


def _build_prompt(talk: Talk, max_terms: int, target: str | None) -> str:
    lines = [
        "You help caption a live conference talk. Suggest a glossary of "
        "technical terms (jargon, acronyms, product/library/framework names, "
        "proper nouns) that a live speech-to-text and machine-translation "
        "pipeline is likely to mis-transcribe or mis-translate.",
        "",
        f"Source language: {talk.language}",
        f"Title: {talk.title}",
    ]
    if talk.abstract:
        lines.append(f"Abstract: {talk.abstract}")
    if talk.tags:
        lines.append(f"Tags: {', '.join(talk.tags)}")
    lines.append("")
    if target:
        lines.append(
            f"For each term, decide whether it should stay in English "
            f"(keep_in_english=true, translation=null) or be translated into "
            f"'{target}' (keep_in_english=false, translation=<the {target} "
            "translation>)."
        )
    else:
        lines.append(
            "This talk has no translation targets configured: set "
            "keep_in_english=true for every term and leave translation null."
        )
    lines.append(f"Return at most {max_terms} terms. Do not repeat near-duplicate terms.")
    return "\n".join(lines)


class GlossarySuggester:
    """Each call to suggest() is independent: no mutable state is shared
    across calls other than the (reusable) client, model configuration and
    the running usd_total, so one GlossarySuggester instance can serve
    concurrent suggest() calls (usd_total additions are simple += on a
    float, from single-threaded asyncio tasks).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-flash",
        *,
        price_in_per_m: float = 0.50,
        price_out_per_m: float = 3.00,
        # gemini-3.8-flash pricing wasn't listed in
        # transcripcion-v0/notas/modelos-gemini.md (only latency/quality
        # notes, no $/M tokens) -- 0.50/3.00 is the task-11a-brief.md
        # fallback placeholder, not a verified price.
        client: Any | None = None,
    ) -> None:
        self.model = model
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

    async def _call(self, talk: Talk, max_terms: int, target: str | None) -> list[_TermOut]:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_GlossaryOut,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        )
        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=_build_prompt(talk, max_terms, target),
            config=config,
        )
        self.usd_total += self._cost_usd(getattr(response, "usage_metadata", None))
        parsed = _GlossaryOut.model_validate(json.loads(response.text))
        return parsed.terms

    @staticmethod
    def _postprocess(raw: list[_TermOut], max_terms: int) -> list[GlossaryTerm]:
        seen: set[str] = set()
        out: list[GlossaryTerm] = []
        for term in raw:
            key = term.term.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                GlossaryTerm(
                    term=term.term.strip(),
                    keep_in_english=term.keep_in_english,
                    translation=None if term.keep_in_english else (term.translation or None),
                )
            )
            if len(out) >= max_terms:
                break
        return out

    async def suggest(self, talk: Talk, max_terms: int = 60) -> list[GlossaryTerm]:
        target = talk.targets[0] if talk.targets else None
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                raw = await self._call(talk, max_terms, target)
            except Exception as exc:  # noqa: BLE001 - never break the caller, see module docstring
                last_exc = exc
                logger.info(
                    "suggest_glossary: attempt %d/%d failed for talk %r: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    talk.id,
                    exc,
                )
                continue
            return self._postprocess(raw, max_terms)

        logger.warning(
            "suggest_glossary: giving up after %d attempt(s) for talk %r, returning an empty glossary: %s",
            _MAX_ATTEMPTS,
            talk.id,
            last_exc,
        )
        return []


async def suggest_glossary(
    talk: Talk,
    *,
    api_key: str,
    model: str = "gemini-3.8-flash",
    max_terms: int = 60,
) -> list[GlossaryTerm]:
    """Suggest a glossary for talk with gemini-3.8-flash.

    Plain-function entry point matching task-11-brief.md's interface; builds
    a one-shot GlossarySuggester (no client reuse across calls -- fine for an
    admin-triggered, infrequent action). See GlossarySuggester for the
    testable, client-injectable class this delegates to.
    """
    return await GlossarySuggester(api_key, model=model).suggest(talk, max_terms=max_terms)
