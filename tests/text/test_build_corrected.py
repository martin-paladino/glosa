"""Tests for glosa/text/corrector.py's build_corrected: task-11r-brief.md
Ruling 1 -- rebuilds a talk's "corrected" export for one target language
from its ORIGINAL source segments, saved with the source segments' own
timings, db's export status pending -> ready|failed.

The genai client is faked throughout (mirrors test_corrector.py); build_corrected
takes an optional ``client=`` (not in the brief's one-line mention of the
function, but consistent with Corrector/GlossarySuggester's own
client-injection pattern elsewhere in glosa/text/ -- see the function's
docstring) so these tests never touch the network.

The one exception is test_live_builds_a_corrected_export_for_20_real_segments
at the bottom: @pytest.mark.live, excluded by default (pytest -m "not live"),
spends real API budget (task-11r-brief.md: <= US$0.01) and is meant to be run
once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from glosa.db import init_db
from glosa.models import GlossaryTerm, Talk
from glosa.text.corrector import build_corrected

ENV_PATH = str(Path(__file__).resolve().parents[2] / ".env")
UTC = timezone.utc


def _talk(talk_id: str = "t1", room_id: str = "r1", language: str = "en", targets: list[str] | None = None) -> Talk:
    start = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    return Talk(
        id=talk_id, room_id=room_id, title="A Talk", speakers=["A"], language=language,
        targets=targets if targets is not None else ["es"], engine="fast", start=start,
        end=start + timedelta(minutes=20), abstract="An abstract.", tags=[],
        glossary=[GlossaryTerm(term="Kubernetes", keep_in_english=True)],
        status="done", actual_start=start, actual_end=start + timedelta(minutes=20),
    )


@dataclass
class _FakeUsage:
    prompt_token_count: int = 100
    candidates_token_count: int = 50
    thoughts_token_count: int = 0


@dataclass
class _FakeResponse:
    text: str
    usage_metadata: _FakeUsage = field(default_factory=_FakeUsage)


class _FakeModels:
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


@pytest.fixture
async def db(tmp_path):
    database = init_db(tmp_path / "glosa.db")
    yield database
    database.close()


async def test_saves_corrected_segments_with_the_sources_own_timings(db) -> None:
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello one.", 0.0, 2.0)
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello two.", 2.0, 4.0)

    client = FakeGenAIClient([_block_response(["Hola uno.", "Hola dos."])])
    status = await build_corrected("t1", "es", db=db, api_key="unused", client=client)

    assert status == "ready"
    assert await db.get_export_status("t1", "es") == "ready"
    saved = await db.get_segments("t1", "es", "corrected")
    assert [(s.text, s.t_start, s.t_end) for s in saved] == [("Hola uno.", 0.0, 2.0), ("Hola dos.", 2.0, 4.0)]
    assert all(s.kind == "translation" and s.room_id == "r1" for s in saved)


async def test_a_failed_block_falls_back_to_the_overlapping_destination_live_text(db) -> None:
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello one.", 0.0, 2.0)
    # The destination's own (uncorrected) live translation for that time range:
    await db.save_segment("t1", "r1", "es", "translation", "live", "Hola vieja.", 0.5, 1.5)

    # Both attempts for the one block fail -> correct_segments contributes None.
    client = FakeGenAIClient([RuntimeError("boom"), RuntimeError("boom again")])
    status = await build_corrected("t1", "es", db=db, api_key="unused", client=client)

    assert status == "ready"  # a per-segment fallback is not a build failure
    saved = await db.get_segments("t1", "es", "corrected")
    assert [(s.text, s.t_start, s.t_end) for s in saved] == [("Hola vieja.", 0.0, 2.0)]


async def test_fallback_is_empty_when_the_destination_has_no_live_segments(db) -> None:
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello one.", 0.0, 2.0)

    client = FakeGenAIClient([RuntimeError("boom"), RuntimeError("boom again")])
    status = await build_corrected("t1", "es", db=db, api_key="unused", client=client)

    assert status == "ready"
    saved = await db.get_segments("t1", "es", "corrected")
    assert [s.text for s in saved] == [""]


async def test_rerun_clears_previous_corrected_segments_instead_of_appending(db) -> None:
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello one.", 0.0, 2.0)

    first = FakeGenAIClient([_block_response(["Hola uno v1."])])
    await build_corrected("t1", "es", db=db, api_key="unused", client=first)
    second = FakeGenAIClient([_block_response(["Hola uno v2."])])
    await build_corrected("t1", "es", db=db, api_key="unused", client=second)

    saved = await db.get_segments("t1", "es", "corrected")
    assert [s.text for s in saved] == ["Hola uno v2."]


async def test_no_source_segments_is_ready_with_nothing_saved(db) -> None:
    await db.insert_talks([_talk()])

    status = await build_corrected("t1", "es", db=db, api_key="unused", client=FakeGenAIClient([]))

    assert status == "ready"
    assert await db.get_segments("t1", "es", "corrected") == []


async def test_missing_talk_reports_failed_without_raising(db) -> None:
    status = await build_corrected("nope", "es", db=db, api_key="unused", client=FakeGenAIClient([]))

    assert status == "failed"
    assert await db.get_export_status("nope", "es") == "failed"


async def test_status_goes_through_pending_before_the_final_status(db) -> None:
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello.", 0.0, 1.0)
    seen: list[str | None] = []
    real_set = db.set_export_status

    async def spy(talk_id: str, lang: str, status: str) -> None:
        seen.append(status)
        await real_set(talk_id, lang, status)

    db.set_export_status = spy  # type: ignore[method-assign]
    await build_corrected("t1", "es", db=db, api_key="unused", client=FakeGenAIClient([_block_response(["Hola."])]))

    assert seen == ["pending", "ready"]


async def test_a_failing_initial_pending_write_is_reported_as_failed_not_raised(db) -> None:
    """task-11r-fix1.md item 2: the initial set_export_status("pending")
    call used to sit outside the try/except, so a DB error there
    propagated uncaught instead of being handled like every other failure
    in this function (logged, reported as "failed")."""
    await db.insert_talks([_talk()])
    await db.save_segment("t1", "r1", "en", "source", "live", "Hello.", 0.0, 1.0)
    real_set = db.set_export_status
    calls: list[str] = []

    async def flaky(talk_id: str, lang: str, status: str) -> None:
        calls.append(status)
        if status == "pending":
            raise RuntimeError("db is locked")
        await real_set(talk_id, lang, status)

    db.set_export_status = flaky  # type: ignore[method-assign]

    status = await build_corrected("t1", "es", db=db, api_key="unused", client=FakeGenAIClient([]))

    assert status == "failed"
    assert calls == ["pending", "failed"]
    assert await db.get_export_status("t1", "es") == "failed"


@pytest.mark.live
async def test_live_builds_a_corrected_export_for_20_real_segments(db) -> None:
    """Runs once against the real gemini-3.8-flash API: 20 EN source
    segments (samples/fixtures/lt_en.jsonl's source_delta text, joined into
    sentences, same fixture test_corrector.py's live test uses) saved as a
    real talk's live segments, corrected to ES via build_corrected end to
    end (db read/write included). Budget: task-11r-brief.md gives <= $0.01
    for this test. Excluded by default; run explicitly with:
        uv run pytest -m live -k build_corrected
    """
    from glosa.config import Settings

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
            if len(sentences) >= 20:
                break
    sentences = sentences[:20]
    assert len(sentences) == 20, f"fixture only yielded {len(sentences)} sentences"

    await db.insert_talks([_talk(targets=["es"])])
    for i, text in enumerate(sentences):
        await db.save_segment("t1", "r1", "en", "source", "live", text, float(i), float(i) + 1.0)

    status = await build_corrected("t1", "es", db=db, api_key=settings.gemini_api_key, block_size=20)

    assert status == "ready"
    saved = await db.get_segments("t1", "es", "corrected")
    assert len(saved) == 20
    assert [(s.t_start, s.t_end) for s in saved] == [(float(i), float(i) + 1.0) for i in range(20)]
    assert sum(1 for s in saved if s.text) > 0, "expected at least some segments to be corrected"

    print("\nLIVE build_corrected: 20 segments")
    for s in saved[:5]:
        print(f"  ES: {s.text}")
