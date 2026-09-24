"""The agenda admin API (Task 8): import a CSV or Nerdearla's sessions JSON
(uploaded, or fetched by the server from a URL), list the talks of a room and
day, and edit one talk.

Every test runs the real app (create_app + lifespan, SQLite in tmp_path) with
rooms that have no audio source, so no pipeline runs and nothing is billed.

The admin cookie lives in the client's own cookie jar and every request
carries the ``X-Glosa-Admin`` CSRF header (glosa/web/auth.py).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from glosa.config import RoomCfg, Settings
from glosa.models import Talk
from glosa.web import admin_api
from glosa.web.app import create_app
from glosa.web.auth import COOKIE_NAME, sign_session

ROOT = Path(__file__).resolve().parents[2]
NERDEARLA = ROOT / "tests" / "fixtures" / "nerdearla_sessions.json"
ADMIN_PASSWORD = "test-password"
CSRF = {"X-Glosa-Admin": "1"}
TZ = "America/Argentina/Buenos_Aires"
ART = ZoneInfo(TZ)

CSV_HEADER = "sala,inicio,fin,titulo,speakers,idioma,destinos,motor,abstract,tags,glosario\n"
CSV = CSV_HEADER + (
    "main,2030-09-24 14:00,2030-09-24 15:00,Charla uno,Ana;Beto,es,en,,Resumen uno,k8s;sre,Kubernetes;nube=cloud\n"
    "Auditorio,2030-09-24 16:00,2030-09-24 16:40,Talk two,Carla,en,es,,Abstract two,ai,\n"
    "sala-x,2030-09-24 17:00,2030-09-24 17:30,Somewhere else,Dan,en,es,,,,\n"
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    values: dict = dict(
        gemini_api_key="unused-in-fake-mode",
        admin_password=ADMIN_PASSWORD,
        timezone=TZ,
        engine_mode="fake",
        fake_fixture=str(ROOT / "tests" / "fixtures" / "fake_lt.jsonl"),
        db_path=str(tmp_path / "glosa.db"),
        rooms=[
            RoomCfg(id="main", name="Main Stage", agenda_names=["gran-sala"], default_targets=["en"]),
            RoomCfg(id="track-2", name="Auditorio", default_targets=["es"]),
        ],
    )
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def _open(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as client:
            client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
            yield app, client


@pytest.fixture
async def admin(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    async with _open(_settings(tmp_path)) as pair:
        yield pair


async def _import_csv(client: httpx.AsyncClient, text: str = CSV) -> httpx.Response:
    return await client.post("/api/admin/agenda/import", files={"file": ("agenda.csv", text.encode(), "text/csv")})


async def _talks(client: httpx.AsyncClient, room: str, day: str = "2030-09-24") -> list[dict]:
    response = await client.get("/api/admin/talks", params={"room": room, "day": day})
    assert response.status_code == 200, response.text
    return response.json()


# ---- import: CSV ----------------------------------------------------------------


async def test_csv_import_maps_rooms_by_id_and_name_and_reports_the_rest(admin) -> None:
    app, client = admin

    response = await _import_csv(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 2
    assert body["skipped"] == [
        {"source_id": None, "title": "Somewhere else", "reason": "row 4: unmapped room: 'sala-x'"}
    ]
    (uno,) = await _talks(client, "main")
    assert uno["title"] == "Charla uno" and uno["room_id"] == "main"
    assert uno["speakers"] == ["Ana", "Beto"] and uno["targets"] == ["en"]
    assert uno["language"] == "es" and uno["engine"] == "glossary"  # es -> glossary
    assert uno["start"] == "2030-09-24T14:00:00-03:00" and uno["end"] == "2030-09-24T15:00:00-03:00"
    assert uno["glossary"] == [
        {"term": "Kubernetes", "keep_in_english": True, "translation": None},
        {"term": "nube", "keep_in_english": False, "translation": "cloud"},
    ]
    assert uno["status"] == "scheduled" and uno["actual_start"] is None
    (two,) = await _talks(client, "track-2")  # "Auditorio" is track-2's name
    assert two["title"] == "Talk two" and two["engine"] == "fast"
    types = [e.type for e in await app.state.db.recent_events(5)]
    assert "agenda_import" in types


async def test_csv_import_takes_a_utf8_bom_and_the_configured_english_engine(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path, default_engine_en="glossary")) as (_, client):
        response = await _import_csv(client, "﻿" + CSV)
        assert response.status_code == 200, response.text
        (two,) = await _talks(client, "track-2")
        assert two["engine"] == "glossary"


async def test_a_bad_csv_row_is_a_422_with_its_row_and_nothing_is_imported(admin) -> None:
    _, client = admin
    bad = CSV_HEADER + (
        "main,2030-09-24 14:00,2030-09-24 15:00,Fine,Ana,es,en,,,,\n"
        "main,2030-09-24 16:00,2030-09-24 17:00,Broken,Ana,pt,en,,,,\n"
    )

    response = await _import_csv(client, bad)

    assert response.status_code == 422
    assert response.json()["detail"]["row"] == 3
    assert "idioma" in response.json()["detail"]["reason"]
    assert await _talks(client, "main") == []


async def test_a_talk_that_ends_before_it_starts_is_skipped(admin) -> None:
    _, client = admin
    text = CSV_HEADER + "main,2030-09-24 15:00,2030-09-24 14:00,Backwards,Ana,es,en,,,,\n"

    body = (await _import_csv(client, text)).json()

    assert body["imported"] == 0
    assert body["skipped"][0]["title"] == "Backwards" and "end" in body["skipped"][0]["reason"]


# ---- import: Nerdearla ---------------------------------------------------------------


async def test_nerdearla_upload_maps_rooms_with_agenda_names(admin) -> None:
    _, client = admin

    response = await client.post(
        "/api/admin/agenda/import",
        files={"file": ("sessions.json", NERDEARLA.read_bytes(), "application/json")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 2  # both gran-sala sessions -> main
    assert body["skipped"] == [
        {
            "source_id": "1250744",
            "title": "Cuando mi Agente Perdió la Paciencia: Seguridad en IA",
            "reason": "unmapped room: 'auditorio'",
        }
    ]
    talks = await _talks(client, "main", "2026-09-24")
    assert [(t["id"], t["language"], t["engine"]) for t in talks] == [
        ("1341066", "es", "glossary"),
        ("1286278", "en", "fast"),
    ]
    assert talks[0]["start"] == "2026-09-24T09:55:00-03:00"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


async def test_nerdearla_url_is_fetched_by_the_server_with_an_explicit_room_map(
    admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = admin
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout: float):
        calls.append((request.full_url, timeout))
        return _FakeResponse(NERDEARLA.read_bytes())

    monkeypatch.setattr(admin_api.urllib.request, "urlopen", fake_urlopen)
    url = "https://backstage.nerdearla.com/api/sessions/?event_id=abc"

    response = await client.post(
        "/api/admin/agenda/import",
        data={"url": url, "room_map": json.dumps({"gran-sala": "main", "auditorio": "track-2"})},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"imported": 3, "skipped": []}
    assert calls == [(url, 15)]
    assert [t["id"] for t in await _talks(client, "track-2", "2026-09-26")] == ["1250744"]


async def test_an_explicit_room_map_replaces_the_configured_names(admin) -> None:
    _, client = admin

    body = (
        await client.post(
            "/api/admin/agenda/import",
            files={"file": ("sessions.json", NERDEARLA.read_bytes(), "application/json")},
            data={"room_map": json.dumps({"auditorio": "track-2"})},
        )
    ).json()

    assert body["imported"] == 1
    assert {s["reason"] for s in body["skipped"]} == {"unmapped room: 'gran-sala'"}


@pytest.mark.parametrize(
    "room_map",
    ["not json", json.dumps(["main"]), json.dumps({"gran-sala": "no-such-room"}), json.dumps({"a": 1})],
)
async def test_a_bad_room_map_is_a_422(admin, room_map: str) -> None:
    _, client = admin

    response = await client.post(
        "/api/admin/agenda/import",
        files={"file": ("sessions.json", NERDEARLA.read_bytes(), "application/json")},
        data={"room_map": room_map},
    )

    assert response.status_code == 422


async def test_only_http_urls_are_fetched(admin, monkeypatch: pytest.MonkeyPatch) -> None:
    _, client = admin
    monkeypatch.setattr(admin_api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("fetched"))

    for url in ("file:///etc/passwd", "ftp://example.com/agenda.csv", "not a url"):
        response = await client.post("/api/admin/agenda/import", data={"url": url})
        assert response.status_code == 422, url


async def test_a_failing_fetch_is_a_502(admin, monkeypatch: pytest.MonkeyPatch) -> None:
    _, client = admin

    def unreachable(request, timeout: float):
        raise OSError("connection refused")

    monkeypatch.setattr(admin_api.urllib.request, "urlopen", unreachable)

    response = await client.post("/api/admin/agenda/import", data={"url": "https://example.com/sessions"})

    assert response.status_code == 502


async def test_an_import_needs_exactly_one_source(admin) -> None:
    _, client = admin

    neither = await client.post("/api/admin/agenda/import", data={})
    both = await client.post(
        "/api/admin/agenda/import",
        files={"file": ("agenda.csv", CSV.encode(), "text/csv")},
        data={"url": "https://example.com/agenda.csv"},
    )

    assert neither.status_code == 422 and both.status_code == 422


async def test_invalid_json_is_a_422(admin) -> None:
    _, client = admin

    response = await client.post(
        "/api/admin/agenda/import", files={"file": ("sessions.json", b"{not json", "application/json")}
    )

    assert response.status_code == 422


# ---- re-import --------------------------------------------------------------------------


async def test_a_reimport_updates_scheduled_talks_and_leaves_live_and_done_ones_alone(admin) -> None:
    app, client = admin
    await _import_csv(client)
    db = app.state.db
    (uno,) = await _talks(client, "main")
    (two,) = await _talks(client, "track-2")
    began = datetime(2030, 9, 24, 16, 1, tzinfo=ART)
    await db.update_talk(two["id"], status="live", actual_start=began)

    renamed = CSV.replace("Resumen uno", "Resumen nuevo").replace("Abstract two", "Changed while live")
    body = (await _import_csv(client, renamed)).json()

    assert body["imported"] == 1
    assert {"source_id": two["id"], "title": "Talk two", "reason": "talk is live: left unchanged"} in body["skipped"]
    assert (await db.get_talk(uno["id"])).abstract == "Resumen nuevo"
    kept = await db.get_talk(two["id"])
    assert (kept.abstract, kept.status, kept.actual_start) == ("Abstract two", "live", began)


# ---- listing ----------------------------------------------------------------------------


def _free_talk(room_id: str, start: datetime) -> Talk:
    return Talk(
        id=f"free-{room_id}-{start:%Y%m%dT%H%M%S}", room_id=room_id, title="Sesión libre", speakers=[],
        language="en", targets=["es"], engine="fast", start=start, end=start + timedelta(hours=12),
        abstract="", tags=[], glossary=[], status="live", actual_start=start, actual_end=None,
    )


async def test_the_agenda_ignores_free_sessions_and_defaults_to_today(admin) -> None:
    app, client = admin
    now = datetime.now(ART).replace(microsecond=0)
    today = now.date().isoformat()
    text = CSV_HEADER + f"main,{today} 00:01,{today} 00:02,Early bird,Ana,es,en,,,,\n"
    await _import_csv(client, text)
    await app.state.db.insert_talks([_free_talk("main", now)])

    listed = (await client.get("/api/admin/talks", params={"room": "main"})).json()

    assert [t["title"] for t in listed] == ["Early bird"]


async def test_the_agenda_of_every_room_at_once(admin) -> None:
    _, client = admin
    await _import_csv(client)

    listed = (await client.get("/api/admin/talks", params={"day": "2030-09-24"})).json()

    assert [(t["room_id"], t["title"]) for t in listed] == [("main", "Charla uno"), ("track-2", "Talk two")]


async def test_listing_an_unknown_room_or_a_bad_day(admin) -> None:
    _, client = admin

    assert (await client.get("/api/admin/talks", params={"room": "nope"})).status_code == 404
    assert (await client.get("/api/admin/talks", params={"day": "yesterday"})).status_code == 422


async def test_one_talk_by_id(admin) -> None:
    _, client = admin
    await _import_csv(client)
    (uno,) = await _talks(client, "main")

    got = await client.get(f"/api/admin/talks/{uno['id']}")

    assert got.status_code == 200 and got.json() == uno
    assert (await client.get("/api/admin/talks/missing")).status_code == 404


# ---- editing (8.3) ----------------------------------------------------------------------


async def test_editing_a_talk_updates_the_db_and_emits_an_admin_event(admin) -> None:  # 8.3
    app, client = admin
    await _import_csv(client)
    (uno,) = await _talks(client, "main")
    sub = app.state.admin_events.subscribe()

    response = await client.put(
        f"/api/admin/talks/{uno['id']}",
        json={
            "title": "Charla uno (renovada)",
            "targets": ["EN", "es"],
            "engine": "fast",
            "start": "2030-09-24T14:10:00",  # naive: the event's timezone
            "glossary": [{"term": "SRE", "keep_in_english": True}],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Charla uno (renovada)" and body["targets"] == ["en", "es"]
    assert body["start"] == "2030-09-24T14:10:00-03:00"
    stored = await app.state.db.get_talk(uno["id"])
    assert (stored.title, stored.engine, stored.targets) == ("Charla uno (renovada)", "fast", ["en", "es"])
    assert stored.start == datetime(2030, 9, 24, 14, 10, tzinfo=ART)
    assert [(g.term, g.keep_in_english) for g in stored.glossary] == [("SRE", True)]
    event = sub.get_nowait()
    assert event.kind == "talk_updated"
    assert event.data["talk"]["id"] == uno["id"] and event.data["talk"]["title"] == "Charla uno (renovada)"
    assert event.data["fields"] == ["engine", "glossary", "start", "targets", "title"]
    logged = await app.state.db.recent_events(3)
    assert logged[0].type == "talk_updated" and logged[0].room_id == "main"


async def test_an_edit_that_changes_nothing_emits_nothing(admin) -> None:
    app, client = admin
    await _import_csv(client)
    (uno,) = await _talks(client, "main")
    sub = app.state.admin_events.subscribe()

    response = await client.put(f"/api/admin/talks/{uno['id']}", json={"title": uno["title"]})

    assert response.status_code == 200
    assert sub.empty()


@pytest.mark.parametrize(
    "edit",
    [
        {"language": "pt"},
        {"targets": ["en", "xx"]},
        {"engine": "turbo"},
        {"start": "2030-09-24T15:30:00-03:00"},  # after the stored end
        {"start": "2030-09-24T16:00:00-03:00", "end": "2030-09-24T16:00:00-03:00"},
        {"title": "   "},
        {"title": None},
        {"status": "done"},  # not an editable field
        {"room_id": "track-2"},
        {"glossary": [{"term": ""}]},
    ],
)
async def test_invalid_edits_are_a_422(admin, edit: dict) -> None:
    app, client = admin
    await _import_csv(client)
    (uno,) = await _talks(client, "main")

    response = await client.put(f"/api/admin/talks/{uno['id']}", json=edit)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    assert (await app.state.db.get_talk(uno["id"])).title == "Charla uno"


async def test_editing_a_missing_talk_is_a_404(admin) -> None:
    _, client = admin
    assert (await client.put("/api/admin/talks/missing", json={"title": "x"})).status_code == 404


async def test_a_live_talk_only_takes_title_targets_and_glossary(admin) -> None:
    app, client = admin
    await _import_csv(client)
    (uno,) = await _talks(client, "main")
    await app.state.db.update_talk(uno["id"], status="live", actual_start=datetime(2030, 9, 24, 14, 0, tzinfo=ART))

    moved = await client.put(f"/api/admin/talks/{uno['id']}", json={"end": "2030-09-24T15:30:00-03:00"})
    same_start = await client.put(f"/api/admin/talks/{uno['id']}", json={"start": uno["start"], "title": "Nuevo"})

    assert moved.status_code == 409 and "end" in moved.json()["detail"]
    assert same_start.status_code == 200  # an unchanged field is not an edit
    assert (await app.state.db.get_talk(uno["id"])).title == "Nuevo"


async def test_free_sessions_are_not_editable(admin) -> None:
    app, client = admin
    free = _free_talk("main", datetime(2030, 9, 24, 9, 0, tzinfo=ART))
    await app.state.db.insert_talks([free])

    response = await client.put(f"/api/admin/talks/{free.id}", json={"title": "x"})

    assert response.status_code == 409


# ---- auth ------------------------------------------------------------------------------------


async def test_the_agenda_endpoints_need_the_admin_cookie_and_the_csrf_header(tmp_path: Path) -> None:
    async with _open(_settings(tmp_path)) as (_, client):
        requests = [
            ("GET", "/api/admin/talks", {}),
            ("GET", "/api/admin/talks/x", {}),
            ("PUT", "/api/admin/talks/x", {"json": {"title": "y"}}),
            ("POST", "/api/admin/agenda/import", {"data": {"url": "https://example.com"}}),
        ]
        for method, path, kwargs in requests:
            no_csrf = await client.request(method, path, headers={"X-Glosa-Admin": ""}, **kwargs)
            assert no_csrf.status_code == 403, path
        client.cookies.clear()
        for method, path, kwargs in requests:
            anonymous = await client.request(method, path, **kwargs)
            assert anonymous.status_code == 401, path
