"""Tests for Corrector / correct_segments: re-translates a talk's ORIGINAL
source segments in blocks of block_size (default 20) with gemini-3.8-flash
(thinking_level=LOW), guided by the glossary and abstract, to build the
"corrected" (post-talk) translation task-11-brief.md's build_corrected saves
with version="corrected" and the same timings as the live segments.

The genai client is faked throughout, mirroring tests/text/test_translator.py.
correct_segments (and Corrector.correct) return one entry PER INPUT SEGMENT --
same order and count as `sources` -- so segment timings stay aligned; a block
that comes back the wrong size even after one retry contributes None for
each of its segments instead (task-11a-brief.md: "el llamador usa la version
live"), which is why the return type here is list[str | None] rather than the
brief's literal list[str] (see task-11a-report.md self-review).

The one exception is test_live_corrects_english_segments_to_spanish at the
bottom: @pytest.mark.live, excluded by default, spends real API budget and is
meant to be run once (see task-11a-report.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

import pytest

from glosa.config import Settings
from glosa.models import GlossaryTerm
from glosa.text.corrector import Corrector, correct_segments

ENV_PATH = str(Path(__file__).resolve().parents[2] / ".env")

GLOSSARY = [
    GlossaryTerm(term="Kubernetes", keep_in_english=True),
    GlossaryTerm(term="control plane", keep_in_english=False, translation="plano de control"),
]


@dataclass
class _FakeUsage:
    prompt_token_count: int = 300
    candidates_token_count: int = 150
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


class FakeGenAIClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = _FakeModels(script)
        self.aio = _FakeAio(self.models)


def _block_response(translations: list[str]) -> _FakeResponse:
    return _FakeResponse(text=json.dumps({"translations": translations}))


def _corrector(script: list[Any], **kwargs: Any) -> tuple[Corrector, FakeGenAIClient]:
    client = FakeGenAIClient(script)
    corrector = Corrector(api_key="test-key", client=client, **kwargs)
    return corrector, client


def _sentences(n: int, prefix: str = "Sentence") -> list[str]:
    return [f"{prefix} {i}." for i in range(1, n + 1)]


async def test_returns_one_translation_per_segment_same_order() -> None:
    sources = _sentences(3)
    corrector, client = _corrector([_block_response(["Frase 1.", "Frase 2.", "Frase 3."])])

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert result == ["Frase 1.", "Frase 2.", "Frase 3."]
    assert len(client.models.calls) == 1


async def test_splits_into_blocks_of_block_size() -> None:
    sources = _sentences(45)
    corrector, client = _corrector(
        [
            _block_response([f"T{i}" for i in range(1, 21)]),
            _block_response([f"T{i}" for i in range(21, 41)]),
            _block_response([f"T{i}" for i in range(41, 46)]),
        ],
        block_size=20,
    )

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert len(result) == 45
    assert result == [f"T{i}" for i in range(1, 46)]
    assert len(client.models.calls) == 3


async def test_second_block_gets_last_two_sentences_of_first_block_as_context() -> None:
    sources = _sentences(22)
    corrector, client = _corrector(
        [
            _block_response([f"T{i}" for i in range(1, 21)]),
            _block_response([f"T{i}" for i in range(21, 23)]),
        ],
        block_size=20,
    )

    await corrector.correct(sources, source_lang="en", target_lang="es", glossary=[], abstract="")

    second_call_instruction = client.models.calls[1]["config"].system_instruction
    assert "Sentence 19." in second_call_instruction
    assert "Sentence 20." in second_call_instruction
    # Only the read-only context, not the whole first block:
    assert "Sentence 1." not in second_call_instruction
    # The context sentences must not appear in what's sent to be translated:
    assert "Sentence 19." not in client.models.calls[1]["contents"]
    assert "Sentence 20." not in client.models.calls[1]["contents"]


async def test_first_block_has_no_context() -> None:
    sources = _sentences(5)
    corrector, client = _corrector([_block_response([f"T{i}" for i in range(1, 6)])])

    await corrector.correct(sources, source_lang="en", target_lang="es", glossary=[], abstract="")

    assert "Previous sentences" not in client.models.calls[0]["config"].system_instruction


async def test_prompt_includes_glossary_and_abstract() -> None:
    sources = _sentences(2)
    corrector, client = _corrector([_block_response(["A", "B"])])

    await corrector.correct(
        sources,
        source_lang="en",
        target_lang="es",
        glossary=GLOSSARY,
        abstract="A talk about container orchestration.",
    )

    instruction = client.models.calls[0]["config"].system_instruction
    # Same wording as glosa/text/translator.py (223df70): the old "term →
    # keep" format must not leak into the corrected version's prompt.
    assert '"Kubernetes": leave it as is, untranslated' in instruction
    assert '"control plane": translate it as "plano de control"' in instruction
    assert "→ keep" not in instruction
    assert "A talk about container orchestration." in instruction


async def test_wrong_count_retries_once_then_succeeds() -> None:
    sources = _sentences(3)
    corrector, client = _corrector(
        [
            _block_response(["only one"]),  # wrong count: 1 != 3
            _block_response(["Frase 1.", "Frase 2.", "Frase 3."]),
        ]
    )

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert result == ["Frase 1.", "Frase 2.", "Frase 3."]
    assert len(client.models.calls) == 2


async def test_wrong_count_after_retry_returns_none_for_that_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sources = _sentences(3)
    corrector, client = _corrector(
        [
            _block_response(["only one"]),
            _block_response(["still", "just two"]),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="glosa.text.corrector"):
        result = await corrector.correct(
            sources, source_lang="en", target_lang="es", glossary=[], abstract=""
        )

    assert result == [None, None, None]
    assert len(client.models.calls) == 2
    assert any("block" in r.getMessage().lower() for r in caplog.records)


async def test_invalid_json_after_retry_returns_none_for_that_block() -> None:
    sources = _sentences(2)
    corrector, client = _corrector([_FakeResponse(text="not json"), _FakeResponse(text="also not json")])

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert result == [None, None]
    assert len(client.models.calls) == 2


async def test_api_error_after_retry_returns_none_for_that_block() -> None:
    sources = _sentences(2)
    corrector, client = _corrector([RuntimeError("boom"), RuntimeError("boom again")])

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert result == [None, None]
    assert len(client.models.calls) == 2


async def test_one_bad_block_does_not_affect_other_blocks() -> None:
    sources = _sentences(25)
    corrector, client = _corrector(
        [
            RuntimeError("boom"),
            RuntimeError("boom again"),
            _block_response([f"T{i}" for i in range(21, 26)]),
        ],
        block_size=20,
    )

    result = await corrector.correct(
        sources, source_lang="en", target_lang="es", glossary=[], abstract=""
    )

    assert result == [None] * 20 + [f"T{i}" for i in range(21, 26)]


async def test_empty_sources_returns_empty_list_without_calling_the_api() -> None:
    corrector, client = _corrector([])
    result = await corrector.correct([], source_lang="en", target_lang="es", glossary=[], abstract="")
    assert result == []
    assert client.models.calls == []


async def test_thinking_level_is_low_not_minimal() -> None:
    sources = _sentences(1)
    corrector, client = _corrector([_block_response(["X"])])
    await corrector.correct(sources, source_lang="en", target_lang="es", glossary=[], abstract="")
    assert client.models.calls[0]["config"].thinking_config.thinking_level == "LOW"


async def test_accumulates_usd_cost_from_usage_metadata() -> None:
    sources = _sentences(3)
    corrector, client = _corrector(
        [
            _FakeResponse(
                text=json.dumps({"translations": ["A", "B", "C"]}),
                usage_metadata=_FakeUsage(prompt_token_count=100, candidates_token_count=40, thoughts_token_count=0),
            )
        ],
        price_in_per_m=0.50,
        price_out_per_m=3.00,
    )

    await corrector.correct(sources, source_lang="en", target_lang="es", glossary=[], abstract="")

    assert corrector.usd_total == pytest.approx((100 * 0.50 + 40 * 3.00) / 1_000_000)


async def test_correct_segments_function_delegates_to_corrector(monkeypatch: pytest.MonkeyPatch) -> None:
    """correct_segments(sources, ..., api_key=...) is the plain-function
    entry point task-11a-brief.md names; it should behave the same as
    Corrector(...).correct(sources, ...)."""
    client = FakeGenAIClient([_block_response(["Uno.", "Dos."])])

    import glosa.text.corrector as corrector_module

    monkeypatch.setattr(corrector_module.genai, "Client", lambda api_key: client)

    result = await correct_segments(
        ["One.", "Two."],
        source_lang="en",
        target_lang="es",
        glossary=[],
        abstract="",
        api_key="test-key",
    )

    assert result == ["Uno.", "Dos."]
    assert client.models.calls[0]["model"] == "gemini-3.8-flash"


@pytest.mark.live
async def test_live_corrects_english_segments_to_spanish() -> None:
    """Runs once against the real gemini-3.8-flash API: corrects 25 EN
    segments (from samples/fixtures/lt_en.jsonl's source_delta text, joined
    into sentences) to ES, measuring real latency and cost (task-11a-brief.md
    11.2's budget: < US$0.05). Excluded by default; run explicitly with:
        uv run pytest -m live -k corrector
    """
    import time

    settings = Settings.load(env_path=ENV_PATH, config_path="config.yaml")

    sentences: list[str] = []
    buf = ""
    with open("samples/fixtures/lt_en.jsonl", encoding="utf-8") as f:
        for line in f:
            event = json.loads(line)
            if event.get("kind") != "source_delta":
                continue
            buf += event.get("text", "")
            while any(p in buf for p in ".?!"):
                for p in ".?!":
                    if p in buf:
                        idx = buf.index(p)
                        sentence = buf[: idx + 1].strip()
                        if sentence:
                            sentences.append(sentence)
                        buf = buf[idx + 1 :]
                        break
            if len(sentences) >= 25:
                break
    sentences = sentences[:25]
    assert len(sentences) == 25, f"fixture only yielded {len(sentences)} sentences"

    corrector = Corrector(api_key=settings.gemini_api_key, block_size=20)

    start = time.monotonic()
    result = await corrector.correct(
        sentences,
        source_lang="en",
        target_lang="es",
        glossary=[],
        abstract="A live demo of an AI provisioning agent.",
    )
    elapsed_s = time.monotonic() - start

    assert len(result) == 25
    assert sum(1 for r in result if r) > 0, "expected at least some segments to be corrected"
    assert corrector.usd_total < 0.05

    print(
        f"\nLIVE correct_segments: {len(sentences)} segments, "
        f"elapsed={elapsed_s:.2f}s, usd={corrector.usd_total:.6f}"
    )
    for src, corrected in list(zip(sentences, result))[:5]:
        print(f"  EN: {src}\n  ES: {corrected}")
