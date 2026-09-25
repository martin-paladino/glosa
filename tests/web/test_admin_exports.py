"""Tests for the two task-11r-brief.md admin endpoints:

  - GET /api/admin/exports (Ruling 2): finished agenda talks with their
    live/corrected export links and the corrected version's build status.
  - POST /api/admin/talks/{id}/suggest-glossary (Ruling 3): suggests a
    glossary without saving it, 502 (not 200+[]) when the model call itself
    failed every attempt (GlossarySuggester.last_error), cost recorded
    under the "glossary_suggest" component.

Both run the real app (create_app + lifespan, SQLite in tmp_path) with a
room that has no audio source, so no pipeline runs and nothing is billed by
the pipeline itself; GlossarySuggester is monkeypatched so these tests never
touch the network either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from glosa.config import RoomCfg, Settings
from glosa.models import GlossaryTerm, Talk
from glosa.web import admin_api
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, sign_session

ADMIN_PASSWORD = "test-password"
CSRF = {"X-Glosa-Admin": "1"}
UTC = timezone.utc


def _settings(tmp_path: Path, **overrides) -> Settings:
    values: dict = dict(
        gemini_api_key="unused",
        admin_password=ADMIN_PASSWORD,
        db_path=str(tmp_path / "glosa.db"),
        rooms=[RoomCfg(id="r1", name="Sala Uno", source_type="file", source_url=None, default_targets=["es"])],
    )
    values.update(overrides)
    return Settings(**values)


def _talk(
    talk_id: str = "t1", *, status: str = "done", language: str = "en", targets: tuple[str, ...] = ("es", "pt")
) -> Talk:
    start = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    return Talk(
        id=talk_id, room_id="r1", title="Charla", speakers=["Ana"], language=language, targets=list(targets),
        engine="fast", start=start, end=start + timedelta(minutes=20), abstract="Un resumen.", tags=[],
        glossary=[], status=status, actual_start=start if status != "scheduled" else None,
        actual_end=start + timedelta(minutes=20) if status == "done" else None,
    )


@asynccontextmanager
async def _open(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(settings, autopilot_interval_s=3600)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as client:
            client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
            yield app, client


@pytest.fixture
async def admin(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    async with _open(_settings(tmp_path)) as pair:
        yield pair


# ---------------------------------------------------------------- GET /exports


async def test_exports_empty_when_no_talk_has_finished(admin) -> None:
    _, client = admin
    response = await client.get("/api/admin/exports")
    assert response.status_code == 200
    assert response.json() == []


async def test_exports_lists_a_done_talks_languages_with_links_and_status(admin) -> None:
    app, client = admin
    await app.state.db.insert_talks([_talk()])

    body = (await client.get("/api/admin/exports")).json()

    assert len(body) == 1
    entry = body[0]
    assert entry["talk_id"] == "t1" and entry["room_id"] == "r1" and entry["title"] == "Charla"
    by_lang = {e["lang"]: e for e in entry["exports"]}
    assert set(by_lang) == {"en", "es", "pt"}  # source + both targets

    assert by_lang["en"]["corrected"] is None  # the source language never gets a corrected entry
    assert by_lang["en"]["live"] == {
        "srt": "/exports/t1/en.srt", "vtt": "/exports/t1/en.vtt", "txt": "/exports/t1/en.txt",
    }
    assert by_lang["es"]["corrected"] == {"status": "pending", "links": None}  # no exports row yet

    await app.state.db.set_export_status("t1", "es", "ready")
    body = (await client.get("/api/admin/exports")).json()
    by_lang = {e["lang"]: e for e in {e2["lang"]: e2 for e2 in body[0]["exports"]}.values()}
    assert by_lang["es"]["corrected"] == {
        "status": "ready",
        "links": {
            "srt": "/exports/t1/es.srt?version=corrected",
            "vtt": "/exports/t1/es.vtt?version=corrected",
            "txt": "/exports/t1/es.txt?version=corrected",
        },
    }
    assert by_lang["pt"]["corrected"] == {"status": "pending", "links": None}  # unaffected


async def test_exports_excludes_unfinished_talks_and_live_free_sessions(admin) -> None:
    app, client = admin
    await app.state.db.insert_talks([_talk("scheduled-t", status="scheduled"), _talk("free-r1-x", status="live")])

    assert (await client.get("/api/admin/exports")).json() == []


async def test_exports_requires_admin_auth(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        client.cookies.clear()
        response = await client.get("/api/admin/exports")
        assert response.status_code == 401


# ---------------------------------------------------------------- POST /suggest-glossary


class _FakeSuggester:
    """Stands in for glosa.text.glossary.GlossarySuggester (monkeypatched
    onto admin_api.GlossarySuggester) so these tests never touch the
    network."""

    def __init__(self, terms=None, last_error=None, usd_total=0.0):
        self._terms = terms if terms is not None else []
        self.last_error = last_error
        self.usd_total = usd_total

    def __call__(self, *, api_key: str) -> "_FakeSuggester":
        return self

    async def suggest(self, talk: Talk) -> list:
        return self._terms


async def test_suggest_glossary_returns_the_suggestion_without_saving_it(
    admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = admin
    await app.state.db.insert_talks([_talk()])
    terms = [
        GlossaryTerm(term="Kubernetes", keep_in_english=True),
        GlossaryTerm(term="control plane", keep_in_english=False, translation="plano de control"),
    ]
    monkeypatch.setattr(admin_api, "GlossarySuggester", _FakeSuggester(terms=terms, usd_total=0.001))

    response = await client.post("/api/admin/talks/t1/suggest-glossary")

    assert response.status_code == 200
    assert response.json() == [
        {"term": "Kubernetes", "keep_in_english": True, "translation": None},
        {"term": "control plane", "keep_in_english": False, "translation": "plano de control"},
    ]
    stored = await app.state.db.get_talk("t1")
    assert stored.glossary == []  # never saved: the admin reviews and PUTs it themselves
    costs = await app.state.db.cost_by_room()
    assert costs.get("r1", 0.0) == pytest.approx(0.001)


async def test_suggest_glossary_502s_when_the_model_call_failed_not_when_its_just_empty(
    admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = admin
    await app.state.db.insert_talks([_talk()])

    monkeypatch.setattr(admin_api, "GlossarySuggester", _FakeSuggester(terms=[]))  # legitimately nothing to suggest
    ok = await client.post("/api/admin/talks/t1/suggest-glossary")
    assert ok.status_code == 200 and ok.json() == []

    monkeypatch.setattr(
        admin_api, "GlossarySuggester", _FakeSuggester(terms=[], last_error=RuntimeError("boom"))
    )
    failed = await client.post("/api/admin/talks/t1/suggest-glossary")
    assert failed.status_code == 502

    events = [e for e in await app.state.db.recent_events(10) if e.type == "suggest_glossary_failed"]
    assert len(events) == 1 and events[0].level == "error" and "boom" in events[0].message


async def test_suggest_glossary_404s_for_an_unknown_talk(admin) -> None:
    _, client = admin
    response = await client.post("/api/admin/talks/nope/suggest-glossary")
    assert response.status_code == 404


async def test_a_finished_free_session_with_captions_lists_its_live_exports_only(admin) -> None:
    app, client = admin
    await app.state.db.insert_talks([_talk("free-r1-a"), _talk("free-r1-empty")])
    await app.state.db.save_segment("free-r1-a", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

    listed = (await client.get("/api/admin/exports")).json()

    assert [entry["talk_id"] for entry in listed] == ["free-r1-a"]  # no captions, nothing to export
    entry = listed[0]
    assert entry["free"] is True and entry["started_at_text"]
    assert all(row["corrected"] is None and row["live"]["srt"].endswith(".srt") for row in entry["exports"])
