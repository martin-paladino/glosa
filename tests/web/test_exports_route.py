"""GET /exports/{talk_id}/{lang}.{srt|vtt|txt}?version=live|corrected
(task-11r-brief.md Ruling 2): formats, filenames, the "corrected" version's
404-until-ready gate, and Settings.exports_public's admin gate (case 11.3b).

Rooms are configured with no source (source_url=None): RoomWorker.has_source
is False, so the boot sequence never tries to start a pipeline or an engine
-- these tests only exercise the export route against a db seeded directly,
no audio/API involved.
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
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, sign_session

ADMIN_PASSWORD = "test-password"
UTC = timezone.utc


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        gemini_api_key="unused",
        admin_password=ADMIN_PASSWORD,
        db_path=str(tmp_path / "glosa.db"),
        rooms=[RoomCfg(id="r1", name="Sala Uno", source_type="file", source_url=None, default_targets=["es"])],
    )
    values.update(overrides)
    return Settings(**values)


def _talk(
    talk_id: str = "t1", *, language: str = "en", targets: tuple[str, ...] = ("es",), status: str = "done"
) -> Talk:
    start = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    return Talk(
        id=talk_id, room_id="r1", title="Charla de prueba", speakers=["Ana"], language=language,
        targets=list(targets), engine="fast", start=start, end=start + timedelta(minutes=20),
        abstract="", tags=[], glossary=[GlossaryTerm(term="x", keep_in_english=True)], status=status,
        actual_start=start, actual_end=start + timedelta(minutes=20),
    )


@asynccontextmanager
async def _open(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(settings, autopilot_interval_s=3600)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


async def test_live_export_renders_stored_segments_as_srt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hello world.", 0.0, 2.0)

        response = await client.get("/exports/t1/en.srt")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-subrip")
        assert 'attachment; filename="r1-charla-de-prueba-en-live.srt"' == response.headers["content-disposition"]
        assert "Hello world." in response.text


async def test_live_export_txt_and_vtt_content_types(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

        txt = await client.get("/exports/t1/en.txt")
        vtt = await client.get("/exports/t1/en.vtt")

        assert txt.headers["content-type"].startswith("text/plain")
        assert txt.text == "Hi.\n"
        assert vtt.headers["content-type"].startswith("text/vtt")
        assert vtt.text.startswith("WEBVTT\n")


async def test_unknown_format_is_404(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        await app.state.db.insert_talks([_talk()])
        assert (await client.get("/exports/t1/en.pdf")).status_code == 404


async def test_unknown_talk_or_lang_is_404(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        await app.state.db.insert_talks([_talk()])
        assert (await client.get("/exports/nope/en.srt")).status_code == 404
        assert (await client.get("/exports/t1/pt.srt")).status_code == 404  # not a spoken or target lang


async def test_corrected_export_is_404_until_ready(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "es", "translation", "corrected", "Hola.", 0.0, 1.0)

        assert (await client.get("/exports/t1/es.srt?version=corrected")).status_code == 404

        await app.state.db.set_export_status("t1", "es", "pending")
        assert (await client.get("/exports/t1/es.srt?version=corrected")).status_code == 404

        await app.state.db.set_export_status("t1", "es", "ready")
        response = await client.get("/exports/t1/es.srt?version=corrected")
        assert response.status_code == 200
        assert "Hola." in response.text
        assert 'filename="r1-charla-de-prueba-es-corrected.srt"' in response.headers["content-disposition"]


async def test_bad_version_is_422(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (app, client):
        await app.state.db.insert_talks([_talk()])
        assert (await client.get("/exports/t1/en.srt?version=nope")).status_code == 422


async def test_exports_public_false_requires_an_admin_session(tmp_path: Path) -> None:
    settings = _settings(tmp_path, exports_public=False)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

        anon = await client.get("/exports/t1/en.srt")
        assert anon.status_code == 401

        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
        admin = await client.get("/exports/t1/en.srt")
        assert admin.status_code == 200


async def test_exports_public_true_needs_no_session(tmp_path: Path) -> None:
    settings = _settings(tmp_path, exports_public=True)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)
        assert (await client.get("/exports/t1/en.srt")).status_code == 200


async def test_qr_only_exports_require_admin_even_with_exports_public(tmp_path: Path) -> None:
    # B-I1: in qr_only, /exports/... must require an admin session
    # regardless of exports_public -- otherwise anyone who guesses a talk
    # id can download captions without ever having the room token.
    settings = _settings(tmp_path, audience_mode="qr_only", exports_public=True)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

        anon = await client.get("/exports/t1/en.srt")
        assert anon.status_code == 401

        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
        admin = await client.get("/exports/t1/en.srt")
        assert admin.status_code == 200


async def test_anonymous_export_of_a_live_talk_is_refused(tmp_path: Path) -> None:
    # B-I1: even in "all" mode with exports_public, anonymous users must
    # only be able to export talks that are "done" -- not the one in
    # progress (talk ids are guessable from public agenda fields).
    settings = _settings(tmp_path, audience_mode="all", exports_public=True)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk(status="live")])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

        anon = await client.get("/exports/t1/en.srt")
        assert anon.status_code == 401

        client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
        admin = await client.get("/exports/t1/en.srt")
        assert admin.status_code == 200


async def test_anonymous_export_of_a_scheduled_talk_is_also_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path, audience_mode="all", exports_public=True)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk(status="scheduled")])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 0.0, 1.0)

        assert (await client.get("/exports/t1/en.srt")).status_code == 401


async def test_shift_s_falls_back_to_the_configured_default_when_the_room_is_idle(tmp_path: Path) -> None:
    """No RoomWorker run means no LatencyTracker samples: the export must
    fall back to Settings.default_export_shift_s (task-11r-brief.md Ruling
    2), not error or default to 0."""
    settings = _settings(tmp_path, default_export_shift_s=5.0)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([_talk()])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 7.0, 8.0)

        response = await client.get("/exports/t1/en.srt")

        # Arrival times 7.0/8.0 moved EARLIER by the 5.0 s delay (final-review-A I3).
        assert "00:00:02,000 --> 00:00:03,200" in response.text


async def test_a_glossary_engine_talk_is_shifted_only_by_the_transcription_lag(tmp_path: Path) -> None:
    """The glossary engine stores captions at their source cut's time, so its
    exports move by exports.GLOSSARY_SHIFT_S (0.8 s), not the fast engine's
    arrival-time default."""
    from dataclasses import replace

    settings = _settings(tmp_path, default_export_shift_s=5.0)
    async with _open(settings) as (app, client):
        await app.state.db.insert_talks([replace(_talk(), engine="glossary")])
        await app.state.db.save_segment("t1", "r1", "en", "source", "live", "Hi.", 7.0, 9.0)

        response = await client.get("/exports/t1/en.srt")

        assert "00:00:06,200 --> 00:00:08,200" in response.text
