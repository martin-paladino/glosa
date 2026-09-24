"""Translator: turns one committed caption segment into a target-language
translation with gemini-3.5-flash-lite (thinking_level=MINIMAL, per
globals.md), guided by the talk's glossary and the last couple of segments
for continuity.

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
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types
from google.genai.errors import APIError

from glosa.clock import Clock, RealClock
from glosa.models import GlossaryTerm

_MAX_CONSECUTIVE_FAILURES = 3
_RETRYABLE_CODES = {429, 503}
_BASE_BACKOFF_S = 0.5


@dataclass
class Translation:
    text: str
    latency_s: float
    usd: float


def _build_system_instruction(target: str, glossary: list[GlossaryTerm], context: list[str]) -> str:
    lines = [
        f"You are a real-time interpreter for a live conference caption feed. "
        f"Translate the user's message into {target}. "
        "Reply with ONLY the translation: no quotes, no notes, no explanations.",
    ]
    if glossary:
        lines.append("")
        lines.append("Glossary (apply exactly; do not deviate):")
        for term in glossary:
            rhs = "keep" if term.keep_in_english else (term.translation or "")
            lines.append(f"{term.term} → {rhs}")
    previous = context[-2:] if context else []
    if previous:
        lines.append("")
        lines.append("Previous segments (context/continuity only; do not re-translate them):")
        for segment in previous:
            lines.append(f"- {segment}")
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
        context: list[str],
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
    without a Gemini key."""

    async def translate(
        self,
        segment: str,
        target: str,
        glossary: list[GlossaryTerm],
        context: list[str],
    ) -> Translation:
        await asyncio.sleep(0)
        return Translation(text=f"[{target}] {segment}", latency_s=0.0, usd=0.0)
