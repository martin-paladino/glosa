"""Translator: turns one committed caption segment into a target-language
translation with gemini-3.5-flash-lite (thinking_level=MINIMAL, per
globals.md), guided by the talk's glossary and the last couple of segments
for continuity.

Known limit: a GlossaryTerm's ``translation`` is ONE string, and the room's
translation lane passes the same glossary for every target language. On a
talk translated into en and pt, a term with ``translation="plano de
control"`` is asked for as "plano de control" in both. Terms kept in
English (``keep_in_english``) are fine in every language; a per-language
translation would need the glossary model to carry one per target.

Retries on 429 (rate limited) / 503 (overloaded) wait a growing amount of
time between attempts. After _MAX_CONSECUTIVE_FAILURES failures in a row on
one model, Translator switches to fallback_model and starts a fresh run of
retries there; if the fallback also exhausts its retries, the last error is
raised. Any other error (e.g. 400) is raised immediately, unretried.

The genai client can be injected (`client=`), which is how tests fake it
without spending API budget; production code leaves it unset and Translator
builds a real `genai.Client(api_key=...)`.

FakeTranslator is what a room uses with ``engine_mode: fake`` (a demo or a
load test with FakeEngine): no API call, no key needed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from google.genai.errors import APIError

from glosa.clock import Clock, RealClock
from glosa.models import GlossaryTerm

_MAX_CONSECUTIVE_FAILURES = 3
_RETRYABLE_CODES = {429, 503}
_BASE_BACKOFF_S = 0.5

# The demo's recorded {segment: real English translation}, built once by
# scripts/build_demo_translations.py from samples/fixtures/tr_es.jsonl (see
# load_demo_translations() below). Resolved relative to cwd first, then to
# this checkout, the same fallback resolve_fake_fixture() (glosa/web/app.py)
# uses, so it is found whatever directory the process is launched from.
_DEMO_TRANSLATIONS_FIXTURE = Path("samples") / "fixtures" / "tr_es_en.json"
_CHECKOUT_DEMO_TRANSLATIONS_FIXTURE = Path(__file__).resolve().parents[2] / _DEMO_TRANSLATIONS_FIXTURE


def load_demo_translations() -> dict[str, str]:
    """FakeTranslator's optional ``lookup``: {} if the fixture is not found
    (a checkout that never ran scripts/build_demo_translations.py), so
    FakeTranslator just falls back to its placeholder for every segment,
    same as before this existed."""
    for path in (Path.cwd() / _DEMO_TRANSLATIONS_FIXTURE, _CHECKOUT_DEMO_TRANSLATIONS_FIXTURE):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


@dataclass
class Translation:
    text: str
    latency_s: float
    usd: float


def _build_system_instruction(
    target: str, glossary: list[GlossaryTerm], context: list[tuple[str, str | None]]
) -> str:
    lines = [
        f"You are a real-time interpreter for a live conference caption feed. "
        f"Translate the user's message into {target}. "
        "Reply with ONLY the translation: no quotes, no notes, no explanations.",
    ]
    # keep_in_english=True: never translated (a customVocabulary term the caption keeps verbatim).
    # translation set (keep_in_english=False): translated to that exact term.
    # translation=None and not keep_in_english: vocabulary for the transcriber only (recognize and
    # spell the term correctly); it is not listed below, so the Translator translates it normally.
    listed_terms = [term for term in glossary if term.keep_in_english or term.translation]
    if listed_terms:
        lines.append("")
        lines.append(
            "Glossary. Use an entry only when its term, or an obvious inflection of it, appears in the "
            "segment you are translating; then use its translation, inflected (number, gender, article "
            "agreement) to fit the sentence -- never paste it in verbatim. Never add a glossary term "
            "that is not in the segment, and never use one to replace a different word (e.g. do not "
            "turn a plain noun into a glossary term):"
        )
        for term in listed_terms:
            if term.keep_in_english:
                lines.append(f'- "{term.term}": leave it as is, untranslated')
            else:
                lines.append(f'- "{term.term}": translate it as "{term.translation}"')
    previous = context[-2:] if context else []
    if previous:
        lines.append("")
        lines.append(
            f"Previous segments and their {target} translation so far, for context/continuity only "
            "(do not translate them again). The segment you are translating now may be a sentence "
            "fragment that continues the last one below: if so, translate it as a continuation, adding "
            "no capitalization or punctuation that the source segment does not have:"
        )
        for source, translation in previous:
            if translation:
                lines.append(f'- "{source}" -> "{translation}"')
            else:
                lines.append(f'- "{source}"')
    return "\n".join(lines)


class Translator:
    """Each call to translate() is independent: no mutable state is shared
    across calls other than the (reusable) client and model configuration,
    so one Translator instance can serve concurrent translate() calls.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        fallback_model: str | None = "gemini-3.1-flash-lite",
        *,
        price_in_per_m: float = 0.30,
        price_out_per_m: float = 2.50,
        client: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model
        self.price_in_per_m = price_in_per_m
        self.price_out_per_m = price_out_per_m
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._clock: Clock = clock if clock is not None else RealClock()

    async def aclose(self) -> None:
        """Release the client's HTTP connections (the owner calls it when it
        is done with this Translator; RoomWorker does at the end of a run)."""
        close = getattr(getattr(self._client, "aio", None), "aclose", None)
        if close is not None:
            await close()

    def _cost_usd(self, usage_metadata: Any) -> float:
        if usage_metadata is None:
            return 0.0
        input_tokens = getattr(usage_metadata, "prompt_token_count", None) or 0
        output_tokens = (getattr(usage_metadata, "candidates_token_count", None) or 0) + (
            getattr(usage_metadata, "thoughts_token_count", None) or 0
        )
        return (input_tokens * self.price_in_per_m + output_tokens * self.price_out_per_m) / 1_000_000

    async def translate(
        self,
        segment: str,
        target: str,
        glossary: list[GlossaryTerm],
        context: list[tuple[str, str | None]],
    ) -> Translation:
        config = types.GenerateContentConfig(
            system_instruction=_build_system_instruction(target, glossary, context),
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        )

        start = self._clock.now()
        last_error: APIError | None = None

        for model in (self.model, self.fallback_model):
            if model is None:
                continue
            failures = 0
            while failures < _MAX_CONSECUTIVE_FAILURES:
                try:
                    response = await self._client.aio.models.generate_content(
                        model=model,
                        contents=segment,
                        config=config,
                    )
                except APIError as exc:
                    if exc.code not in _RETRYABLE_CODES:
                        raise
                    last_error = exc
                    failures += 1
                    if failures >= _MAX_CONSECUTIVE_FAILURES:
                        break  # exhausted this model's retries; try the next one
                    await self._clock.sleep(_BASE_BACKOFF_S * (2 ** (failures - 1)))
                    continue

                latency_s = self._clock.now() - start
                usd = self._cost_usd(response.usage_metadata)
                return Translation(text=(response.text or "").strip(), latency_s=latency_s, usd=usd)

        assert last_error is not None  # every exit path above sets it before falling through
        raise last_error


class FakeTranslator:
    """``engine_mode: fake``: the "translation" is the segment itself tagged
    with the target language ("[en] Hola a todos."), at once and for free,
    so a demo shows the glossary engine's and the extra languages' captions
    without a Gemini key.

    ``lookup`` is an optional {segment: translation} map (e.g. the demo's
    recorded ``samples/fixtures/tr_es_en.json``, see
    ``load_demo_translations()`` below): a segment found there returns its
    real recorded translation verbatim instead of the tagged placeholder.
    Any segment not in ``lookup`` (or when ``lookup`` is None/empty) still
    gets the placeholder, so existing callers/tests are unaffected."""

    def __init__(self, lookup: dict[str, str] | None = None) -> None:
        self._lookup = lookup or {}

    async def translate(
        self,
        segment: str,
        target: str,
        glossary: list[GlossaryTerm],
        context: list[tuple[str, str | None]],
    ) -> Translation:
        await asyncio.sleep(0)
        translation = self._lookup.get(segment)
        if translation is not None:
            return Translation(text=translation, latency_s=0.0, usd=0.0)
        return Translation(text=f"[{target}] {segment}", latency_s=0.0, usd=0.0)
