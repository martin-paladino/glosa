"""Tests for GlossarySuggester / suggest_glossary: suggests a glossary of
technical terms for a talk with gemini-3.8-flash (thinking_level=LOW, per
globals.md -- gemini-3.8-flash does not accept MINIMAL), from the talk's
title/abstract/tags/language/targets (task-11a-brief.md).

The genai client is faked throughout, mirroring tests/text/test_translator.py
(FakeGenAIClient exposes just client.aio.models.generate_content(model=,
contents=, config=)); responses carry .text (the raw JSON the SDK would
return for response_mime_type="application/json") and .usage_metadata.

The one exception is test_live_suggests_glossary_for_a_real_talk at the
bottom: @pytest.mark.live, excluded by default (pytest -m "not live"), spends
real API budget and is meant to be run once (see task-11a-report.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from glosa.config import Settings
from glosa.models import Talk
from glosa.text.glossary import GlossarySuggester, suggest_glossary

ENV_PATH = "/Users/mpaladino/repos/glosa/.env"


def _talk(**overrides: Any) -> Talk:
    defaults: dict[str, Any] = dict(
        id="t1",
        room_id="main",
        title="Kubernetes at the Edge with Helm and eBPF",
        speakers=["A Speaker"],
        language="en",
        targets=["es"],
        engine="fast",
        start=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 24, 10, 40, tzinfo=timezone.utc),
        abstract="We deploy Kubernetes operators at the edge, using Helm charts "
        "and eBPF for observability, and discuss control plane cost.",
        tags=["kubernetes", "devops"],
        glossary=[],
        status="scheduled",
        actual_start=None,
        actual_end=None,
    )
    defaults.update(overrides)
    return Talk(**defaults)


@dataclass
class _FakeUsage:
    prompt_token_count: int = 200
    candidates_token_count: int = 80
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


def _json_response(terms: list[dict[str, Any]]) -> _FakeResponse:
    return _FakeResponse(text=json.dumps({"terms": terms}))


def _suggester(script: list[Any], **kwargs: Any) -> tuple[GlossarySuggester, FakeGenAIClient]:
    client = FakeGenAIClient(script)
    suggester = GlossarySuggester(api_key="test-key", client=client, **kwargs)
    return suggester, client


async def test_parses_json_into_glossary_terms() -> None:
    suggester, client = _suggester(
        [
            _json_response(
                [
                    {"term": "Kubernetes", "keep_in_english": True, "translation": None},
                    {"term": "control plane", "keep_in_english": False, "translation": "plano de control"},
                ]
            )
        ]
    )

    terms = await suggester.suggest(_talk())

    assert len(terms) == 2
    assert terms[0].term == "Kubernetes"
    assert terms[0].keep_in_english is True
    assert terms[0].translation is None
    assert terms[1].term == "control plane"
    assert terms[1].keep_in_english is False
    assert terms[1].translation == "plano de control"
    assert len(client.models.calls) == 1
    assert client.models.calls[0]["model"] == "gemini-3.8-flash"


async def test_discards_duplicate_terms_case_insensitively() -> None:
    """Case 11.2 (task-11-brief.md): parse the JSON, discard duplicates."""
    suggester, _ = _suggester(
        [
            _json_response(
                [
                    {"term": "Kubernetes", "keep_in_english": True},
                    {"term": " kubernetes ", "keep_in_english": True},
                    {"term": "KUBERNETES", "keep_in_english": True},
                    {"term": "Helm", "keep_in_english": True},
                ]
            )
        ]
    )

    terms = await suggester.suggest(_talk())

    assert [t.term for t in terms] == ["Kubernetes", "Helm"]


async def test_discards_empty_and_whitespace_only_terms() -> None:
    suggester, _ = _suggester(
        [
            _json_response(
                [
                    {"term": "", "keep_in_english": True},
                    {"term": "   ", "keep_in_english": True},
                    {"term": "Helm", "keep_in_english": True},
                ]
            )
        ]
    )

    terms = await suggester.suggest(_talk())

    assert [t.term for t in terms] == ["Helm"]


async def test_caps_at_max_terms() -> None:
    suggester, _ = _suggester(
        [_json_response([{"term": f"term{i}", "keep_in_english": True} for i in range(10)])]
    )

    terms = await suggester.suggest(_talk(), max_terms=3)

    assert len(terms) == 3
    assert [t.term for t in terms] == ["term0", "term1", "term2"]


async def test_invalid_json_retries_once_then_succeeds() -> None:
    suggester, client = _suggester(
        [
            _FakeResponse(text="not json"),
            _json_response([{"term": "Helm", "keep_in_english": True}]),
        ]
    )

    terms = await suggester.suggest(_talk())

    assert [t.term for t in terms] == ["Helm"]
    assert len(client.models.calls) == 2


async def test_invalid_json_after_retry_returns_empty_list_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    suggester, client = _suggester([_FakeResponse(text="not json"), _FakeResponse(text="still not json")])

    with caplog.at_level(logging.WARNING, logger="glosa.text.glossary"):
        terms = await suggester.suggest(_talk())

    assert terms == []
    assert len(client.models.calls) == 2
    assert any("glossary" in r.getMessage().lower() for r in caplog.records)


async def test_api_error_retries_once_then_returns_empty_list() -> None:
    suggester, client = _suggester([RuntimeError("boom"), RuntimeError("boom again")])

    terms = await suggester.suggest(_talk())

    assert terms == []
    assert len(client.models.calls) == 2


async def test_thinking_level_is_low_not_minimal() -> None:
    suggester, client = _suggester([_json_response([])])
    await suggester.suggest(_talk())
    assert client.models.calls[0]["config"].thinking_config.thinking_level == "LOW"


async def test_prompt_includes_title_abstract_tags_and_primary_target() -> None:
    suggester, client = _suggester([_json_response([])])
    talk = _talk(
        title="My Special Talk Title",
        abstract="An abstract mentioning eBPF and control planes.",
        tags=["ebpf", "networking"],
        targets=["pt"],
    )
    await suggester.suggest(talk)

    contents = client.models.calls[0]["contents"]
    assert "My Special Talk Title" in contents
    assert "eBPF" in contents
    assert "ebpf" in contents or "networking" in contents
    assert "pt" in contents


async def test_suggest_glossary_function_delegates_to_suggester(monkeypatch: pytest.MonkeyPatch) -> None:
    """suggest_glossary(talk, api_key=..., model=..., max_terms=...) is the
    plain-function entry point task-11-brief.md names; it should behave the
    same as GlossarySuggester(...).suggest(talk, max_terms=...)."""
    client = FakeGenAIClient([_json_response([{"term": "Helm", "keep_in_english": True}])])

    import glosa.text.glossary as glossary_module

    monkeypatch.setattr(glossary_module.genai, "Client", lambda api_key: client)

    terms = await suggest_glossary(_talk(), api_key="test-key", max_terms=5)

    assert [t.term for t in terms] == ["Helm"]
    assert client.models.calls[0]["model"] == "gemini-3.8-flash"


@pytest.mark.live
async def test_live_suggests_glossary_for_a_real_talk() -> None:
    """Runs once against the real gemini-3.8-flash API: suggests a glossary
    for a real Nerdearla talk (tests/fixtures/nerdearla_sessions.json),
    measuring real latency and cost (task-11a-brief.md). Excluded by default
    (pytest -m "not live"); run explicitly with:
        uv run pytest -m live -k glossary
    """
    import time

    from glosa.agenda.nerdearla_import import parse_nerdearla

    settings = Settings.load(env_path=ENV_PATH, config_path="config.yaml")

    with open("tests/fixtures/nerdearla_sessions.json", encoding="utf-8") as f:
        sessions = json.load(f)
    talks = parse_nerdearla(sessions, room_map={"auditorio": "main"}, tz="America/Argentina/Buenos_Aires")
    talk = next(t for t in talks if "Paciencia" in t.title)
    talk.targets = ["en"]

    suggester = GlossarySuggester(api_key=settings.gemini_api_key)

    start = time.monotonic()
    terms = await suggester.suggest(talk)
    elapsed_s = time.monotonic() - start

    assert terms, "expected at least one suggested term for a technical talk"
    assert suggester.usd_total < 0.05

    print(
        f"\nLIVE suggest_glossary: talk={talk.title!r}, {len(terms)} terms, "
        f"elapsed={elapsed_s:.2f}s, usd={suggester.usd_total:.6f}"
    )
    for term in terms[:10]:
        rhs = "keep" if term.keep_in_english else term.translation
        print(f"  {term.term} -> {rhs}")
