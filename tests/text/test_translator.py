"""Tests for Translator: flash-lite translation guided by the talk's
glossary and the last couple of segments, with growing-wait retries on
429/503 that fall back to gemini-3.1-flash-lite after 3 consecutive
failures (task-10-brief.md 10.2). The genai client is faked throughout;
FakeGenAIClient below mirrors just the surface Translator calls
(client.aio.models.generate_content(model=, contents=, config=)).

The one exception is test_live_translates_three_technical_phrases_en_to_es
at the bottom: it is @pytest.mark.live (excluded by default, see
pytest.ini's `-m "not live"`), spends real API budget, and is meant to be
run once to measure real latency (see task-10a-report.md for the result).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

import pytest
from google.genai.errors import ClientError, ServerError

from glosa.clock import FakeClock
from glosa.config import Settings
from glosa.models import GlossaryTerm
from glosa.text.translator import Translation, Translator, _build_system_instruction

ENV_PATH = str(Path(__file__).resolve().parents[2] / ".env")

GLOSSARY = [
    GlossaryTerm(term="Kubernetes", keep_in_english=True),
    GlossaryTerm(term="control plane", keep_in_english=False, translation="plano de control"),
]


@dataclass
class _FakeUsage:
    prompt_token_count: int = 40
    candidates_token_count: int = 12
    thoughts_token_count: int = 0


@dataclass
class _FakeResponse:
    text: str
    usage_metadata: _FakeUsage = field(default_factory=_FakeUsage)


class _FakeModels:
    """Records every generate_content() call and plays back a fixed script
    of responses/exceptions, one per call, in order."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: str, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeAio:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class FakeGenAIClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = _FakeModels(script)
        self.aio = _FakeAio(self.models)


def _translator(script: list[Any], **kwargs: Any) -> tuple[Translator, FakeGenAIClient]:
    client = FakeGenAIClient(script)
    translator = Translator(
        api_key="test-key",
        client=client,
        clock=FakeClock(),
        price_in_per_m=0.30,
        price_out_per_m=2.50,
        **kwargs,
    )
    return translator, client


async def test_translate_returns_text_latency_and_cost_from_usage_metadata() -> None:
    translator, client = _translator([_FakeResponse(text=" Desplegamos con Helm. ")])

    result = await translator.translate("We deploy with Helm.", "es", GLOSSARY, [])

    assert result.text == "Desplegamos con Helm."
    assert result.latency_s >= 0.0
    assert result.usd == pytest.approx((40 * 0.30 + 12 * 2.50) / 1_000_000)
    assert len(client.models.calls) == 1
    assert client.models.calls[0]["model"] == "gemini-3.5-flash-lite"
    assert client.models.calls[0]["contents"] == "We deploy with Helm."


async def test_system_instruction_includes_glossary_and_last_two_segments() -> None:
    translator, client = _translator([_FakeResponse(text="ok")])

    context = ["Segment one.", "Segment two.", "Segment three."]
    await translator.translate("Segment four.", "es", GLOSSARY, context)

    system_instruction = client.models.calls[0]["config"].system_instruction
    assert '"Kubernetes": leave it as is, untranslated' in system_instruction
    assert '"control plane": translate it as "plano de control"' in system_instruction
    assert "Segment two." in system_instruction
    assert "Segment three." in system_instruction
    assert "Segment one." not in system_instruction  # only the last 2 of context


def test_the_glossary_applies_only_to_terms_in_the_segment() -> None:
    """Live run 4: "en este particular cluster" came out as "in this
    particular Kubernetes control plane": the model put glossary terms where
    the segment had none. The instruction scopes every entry to the segment."""
    text = _build_system_instruction("en", GLOSSARY, [])

    rules = text.split("Glossary")[1].lower()
    assert "only when" in rules and "appears in the segment" in rules and "inflection" in rules
    assert "never add" in rules and "not in the segment" in rules
    assert "apply exactly; do not deviate" not in text  # the old, unscoped wording
    assert text.index('"Kubernetes"') > text.index("Glossary")


def test_a_term_kept_in_english_is_never_written_as_keep() -> None:
    """Live run 5: with "Kubernetes → keep" in the prompt, "en este particular
    cluster" came out as "in this particular keep". No entry reads as a
    translation into the word "keep" any more."""
    text = _build_system_instruction("en", GLOSSARY, [])

    assert "→ keep" not in text and "-> keep" not in text
    assert '"Kubernetes": leave it as is, untranslated' in text


def test_glossary_translations_are_inflected_to_fit_the_sentence() -> None:
    """Live run: a glossary target pasted verbatim gave "los agente", "las
    capacidad" (task-16q-brief.md). The rule now tells the model to inflect
    the translation (number, gender, article agreement) instead."""
    text = _build_system_instruction("es", GLOSSARY, [])

    rules = text.split("Glossary")[1].lower()
    assert "inflected" in rules
    assert "number" in rules and "gender" in rules and "agreement" in rules
    assert '"control plane": translate it as "plano de control"' in text  # still names the term


def test_a_term_with_no_translation_and_not_kept_in_english_is_translated_normally() -> None:
    """translation=None + keep_in_english=False means "vocabulary for the
    transcriber only" (customVocabulary): the bench found it was treated
    like "keep as is" instead. It must not be listed as keep-verbatim (or
    at all) in the translation prompt."""
    glossary = [GlossaryTerm(term="agents", keep_in_english=False, translation=None)]
    text = _build_system_instruction("es", glossary, [])

    assert '"agents"' not in text
    assert "Glossary" not in text  # nothing left to list: transcriber-only vocabulary


async def test_thinking_level_is_minimal() -> None:
    translator, client = _translator([_FakeResponse(text="ok")])
    await translator.translate("hi", "es", [], [])
    assert client.models.calls[0]["config"].thinking_config.thinking_level == "MINIMAL"


async def test_retries_with_growing_wait_on_503_then_succeeds() -> None:
    translator, client = _translator(
        [
            ServerError(503, {"message": "overloaded"}),
            _FakeResponse(text="ok"),
        ]
    )
    result = await translator.translate("hi", "es", [], [])
    assert result.text == "ok"
    assert len(client.models.calls) == 2
    assert all(c["model"] == "gemini-3.5-flash-lite" for c in client.models.calls)


async def test_switches_to_fallback_model_after_three_consecutive_503s() -> None:
    translator, client = _translator(
        [
            ServerError(503, {"message": "overloaded"}),
            ServerError(503, {"message": "overloaded"}),
            ServerError(503, {"message": "overloaded"}),
            _FakeResponse(text="ok"),
        ]
    )
    result = await translator.translate("hi", "es", [], [])
    assert result.text == "ok"
    assert [c["model"] for c in client.models.calls] == [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


async def test_retries_on_429_too() -> None:
    translator, client = _translator(
        [
            ClientError(429, {"message": "rate limited"}),
            _FakeResponse(text="ok"),
        ]
    )
    result = await translator.translate("hi", "es", [], [])
    assert result.text == "ok"
    assert len(client.models.calls) == 2


async def test_non_retryable_error_is_raised_immediately() -> None:
    translator, client = _translator([ClientError(400, {"message": "bad request"})])
    with pytest.raises(ClientError):
        await translator.translate("hi", "es", [], [])
    assert len(client.models.calls) == 1


async def test_raises_last_error_when_fallback_also_exhausts_retries() -> None:
    translator, client = _translator([ServerError(503, {"message": "x"})] * 6)
    with pytest.raises(ServerError):
        await translator.translate("hi", "es", [], [])
    assert len(client.models.calls) == 6


async def test_aclose_releases_the_client_connections() -> None:
    translator, client = _translator([])
    await translator.aclose()
    assert client.aio.closed == 1


@pytest.mark.live
async def test_live_translates_three_technical_phrases_en_to_es() -> None:
    """Runs once against the real gemini-3.5-flash-lite API: 3 technical EN
    phrases -> es, with a glossary, measuring real latency (task-10a-brief.md).
    Excluded by default (pytest -m "not live"); run explicitly with:
        uv run pytest -m live -k translator
    """
    settings = Settings.load(env_path=ENV_PATH, config_path="config.yaml")
    translator = Translator(
        api_key=settings.gemini_api_key,
        price_in_per_m=settings.prices.flash_lite_in_per_m,
        price_out_per_m=settings.prices.flash_lite_out_per_m,
    )
    glossary = [
        GlossaryTerm(term="Kubernetes", keep_in_english=True),
        GlossaryTerm(term="control plane", keep_in_english=False, translation="plano de control"),
        GlossaryTerm(term="Helm", keep_in_english=True),
    ]
    phrases = [
        "We deploy our Kubernetes operator with Helm.",
        "The control plane cost went up tenfold last quarter.",
        "Our ingress controller terminates TLS at the edge.",
    ]

    context: list[str] = []
    results: list[Translation] = []
    for phrase in phrases:
        result = await translator.translate(phrase, "es", glossary, context)
        results.append(result)
        context.append(phrase)

    for phrase, result in zip(phrases, results):
        assert result.text, f"empty translation for: {phrase}"
        assert result.latency_s > 0
        assert result.usd > 0

    latencies = sorted(r.latency_s for r in results)
    p50 = latencies[len(latencies) // 2]
    total_usd = sum(r.usd for r in results)
    print(
        "\nLIVE translator latency_s per call: "
        f"{[round(r.latency_s, 3) for r in results]}, p50={p50:.3f}s, "
        f"total_usd={total_usd:.6f}"
    )
    for phrase, result in zip(phrases, results):
        print(f"  EN: {phrase}\n  ES: {result.text}")
