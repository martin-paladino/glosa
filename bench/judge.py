"""bench/judge.py: the ONE reference translation per clip, and the LLM-judge
score for an engine's translated output, both with gemini-3.8-flash
(thinking_level="LOW", per globals.md -- gemini-3.8-flash does not accept
MINIMAL), mirroring glosa/text/glossary.py's client pattern: structured
JSON output (response_schema), a genai client injectable for tests so
nothing here ever needs the network to be importable/testable.

Real calls only ever happen from bench/bench.py --live; both functions take
an already-built ``client`` so this module itself makes no network call at
import time or by accident.
"""

from __future__ import annotations

import json
from typing import Any

from google.genai import types
from pydantic import BaseModel

JUDGE_MODEL = "gemini-3.8-flash"
# gemini-3.8-flash pricing wasn't listed anywhere in this repo's notes at
# write time (see glosa/text/glossary.py's identical comment) -- same
# placeholder used there; the judge/reference-translation spend is a few
# cents regardless (short prompts, LOW thinking).
PRICE_IN_PER_M = 0.50
PRICE_OUT_PER_M = 3.00

REFERENCE_PROMPT = """You are a professional conference interpreter. Translate the following \
{source_lang} conference-talk transcript into {target_lang}.

This is an auto-generated transcript (YouTube's auto-captions): it has no \
punctuation-perfect sentence breaks and may contain a few transcription \
errors or run-on phrasing. Translate the evident intended meaning fluently \
into natural {target_lang}; do not preserve transcription artifacts \
(garbled words, missing punctuation) in your translation. Reply with ONLY \
the translation: no notes, no quotes, no preamble.

Transcript:
{transcript}"""

JUDGE_PROMPT = """You are grading a live-captioning system's machine translation of a \
conference talk, spoken in {source_lang} and translated live into \
{target_lang}. Score the ENGINE OUTPUT below on two 1-5 integer scales:

- fidelity (1-5): does it preserve the source's meaning and technical terms? \
1 = major errors or omissions that change the meaning, 5 = fully faithful.
- fluency (1-5): is it natural, well-formed {target_lang} on its own? \
1 = broken or garbled, 5 = reads naturally.

Judge the engine output against BOTH the source transcript (the ground-truth \
meaning, though it is itself an imperfect auto-caption transcript with some \
noise) and the reference translation (a fluent, human-quality benchmark \
translation of that same source). Give a one-line justification.

Source transcript ({source_lang}):
{source}

Reference translation ({target_lang}):
{reference}

Engine output to grade ({target_lang}):
{engine_output}"""


class JudgeScore(BaseModel):
    fidelity: int
    fluency: int
    justification: str


def _cost_usd(usage_metadata: Any) -> float:
    if usage_metadata is None:
        return 0.0
    input_tokens = getattr(usage_metadata, "prompt_token_count", None) or 0
    output_tokens = (getattr(usage_metadata, "candidates_token_count", None) or 0) + (
        getattr(usage_metadata, "thoughts_token_count", None) or 0
    )
    return (input_tokens * PRICE_IN_PER_M + output_tokens * PRICE_OUT_PER_M) / 1_000_000


async def build_reference_translation(
    client: Any, transcript: str, source_lang: str, target_lang: str
) -> tuple[str, float]:
    """The ONE full-text reference translation for a clip. Returns
    (translation, usd)."""
    config = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="LOW"))
    prompt = REFERENCE_PROMPT.format(source_lang=source_lang, target_lang=target_lang, transcript=transcript)
    response = await client.aio.models.generate_content(model=JUDGE_MODEL, contents=prompt, config=config)
    usd = _cost_usd(getattr(response, "usage_metadata", None))
    return (response.text or "").strip(), usd


async def judge_translation(
    client: Any,
    source: str,
    reference: str,
    engine_output: str,
    source_lang: str,
    target_lang: str,
) -> tuple[int, int, str, float]:
    """Returns (fidelity, fluency, justification, usd)."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=JudgeScore,
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
    )
    prompt = JUDGE_PROMPT.format(
        source_lang=source_lang, target_lang=target_lang, source=source, reference=reference,
        engine_output=engine_output or "(empty -- the engine produced no output)",
    )
    response = await client.aio.models.generate_content(model=JUDGE_MODEL, contents=prompt, config=config)
    usd = _cost_usd(getattr(response, "usage_metadata", None))
    parsed = JudgeScore.model_validate(json.loads(response.text))
    return parsed.fidelity, parsed.fluency, parsed.justification, usd
