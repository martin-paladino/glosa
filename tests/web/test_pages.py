"""Tests for the audience pages (glosa.web.pages): the public room list at `/`
and the room view at `/s/{slug}`.

Task 5 owns create_app(); here a minimal FastAPI app provides what the pages
read from app.state (a fake rooms_view and branding) and mounts /static.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.i18n import t
from glosa.web import pages

STATIC_DIR = Path(pages.__file__).parent / "static"

ROOMS = [
    {
        "slug": "r1",
        "name": "Gran sala",
        "langs": ["en", "es"],
        "now": {
            "talk_id": "t1",
            "title": "What your Kubernetes control plane really costs",
            "speakers": ["Priya Raman"],
            "language": "en",
        },
        "next": {
            "talk_id": "t2",
            "title": "Platform engineering sin humo",
            "speakers": ["Diego Ferreyra"],
            "language": "es",
            "start": "14:50",
        },
    },
    {
        "slug": "r2",
        "name": "Sala Comunidad",
        "langs": ["es", "en"],
        "now": None,
        "next": {
            "talk_id": "t3",
            "title": "Cómo se organiza una Nerdearla con 300 voluntarios",
            "speakers": ["Ana Sosa", "Pablo Díaz"],
            "language": "es",
            "start": "15:30",
        },
    },
    {
        "slug": "r3",
        "name": "Sala Talleres",
        "langs": ["es"],
        "now": None,
        "next": None,
    },
]

BRANDING = {"event_name": "Nerdearla 2026", "primary": None, "accent": None, "logo_url": None}

SPANISH = {"Accept-Language": "es-AR,es;q=0.9,en;q=0.8"}
ENGLISH = {"Accept-Language": "en-US,en;q=0.9"}


def _make_app(rooms: list[dict] | None = None, branding: dict | None = None) -> FastAPI:
    app = FastAPI()
    snapshot = ROOMS if rooms is None else rooms
    app.state.rooms_view = lambda: snapshot
    app.state.branding = dict(BRANDING if branding is None else branding)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages.router)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app())


def _html_lang(html: str) -> str:
    match = re.search(r'<html[^>]*\slang="([^"]+)"', html)
    assert match, "the page must declare <html lang>"
    return match.group(1)


def _room_config(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="glosa-room">(.*?)</script>', html, re.S
    )
    assert match, "the room page must embed its JSON config for room.js"
    return json.loads(match.group(1))


# ---- / : public room list --------------------------------------------------


def test_index_lists_rooms_with_now_and_next(client: TestClient) -> None:
    response = client.get("/", headers=SPANISH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "Nerdearla 2026" in html
    for room in ROOMS:
        assert room["name"] in html
        assert f'href="/s/{room["slug"]}"' in html
    assert t("now", "es") in html
    assert t("next", "es") in html
    assert "What your Kubernetes control plane really costs" in html
    assert "Platform engineering sin humo" in html
    assert "14:50" in html
    assert "15:30" in html
    assert "Priya Raman" in html
    assert "Ana Sosa y Pablo Díaz" in html


def test_index_marks_live_and_idle_rooms(client: TestClient) -> None:
    html = client.get("/", headers=SPANISH).text

    assert html.count("status--live") == 1
    assert t("no_talk_now", "es") in html
    assert t("no_more_talks", "es") in html
    assert t("between_talks", "es") in html


def test_index_describes_talk_and_caption_languages(client: TestClient) -> None:
    spanish = client.get("/", headers=SPANISH).text
    english = client.get("/", headers=ENGLISH).text

    assert "En inglés, con subtítulos en español." in spanish
    assert "In English, with Spanish captions." in english


def test_index_follows_accept_language(client: TestClient) -> None:
    html = client.get("/", headers=ENGLISH).text

    assert _html_lang(html) == "en"
    assert t("now", "en") in html
    assert t("next", "en") in html
    assert t("now", "es") not in html


def test_index_lang_query_overrides_accept_language(client: TestClient) -> None:
    html = client.get("/?lang=en", headers=SPANISH).text

    assert _html_lang(html) == "en"
    assert t("index_lede", "en") in html
    # An explicit choice travels with the links to the rooms.
    assert 'href="/s/r1?lang=en"' in html


def test_index_offers_the_other_interface_language(client: TestClient) -> None:
    spanish = client.get("/", headers=SPANISH).text
    english = client.get("/", headers=ENGLISH).text

    assert 'href="/?lang=en"' in spanish
    assert ">English<" in spanish
    assert 'href="/?lang=es"' in english
    assert ">Español<" in english


def test_index_with_no_rooms_explains_what_to_do() -> None:
    client = TestClient(_make_app(rooms=[]))

    html = client.get("/", headers=SPANISH).text

    assert t("rooms_empty", "es") in html


def test_pages_vary_on_accept_language(client: TestClient) -> None:
    assert "accept-language" in client.get("/").headers["vary"].lower()
    assert "accept-language" in client.get("/s/r1").headers["vary"].lower()


# ---- /s/{slug} : room view ---------------------------------------------------


def test_room_renders_in_spanish_by_default(client: TestClient) -> None:
    response = client.get("/s/r1", headers=SPANISH)

    assert response.status_code == 200
    html = response.text
    assert _html_lang(html) == "es"
    assert "Gran sala" in html
    assert "What your Kubernetes control plane really costs" in html
    assert "Priya Raman" in html
    assert t("change_room", "es") in html
    assert t("back_to_live", "es") in html
    assert _room_config(html)["defaultLang"] == "es"


def test_room_without_accept_language_defaults_to_spanish(client: TestClient) -> None:
    html = client.get("/s/r1").text

    assert _html_lang(html) == "es"
    assert _room_config(html)["defaultLang"] == "es"


def test_room_follows_accept_language(client: TestClient) -> None:
    html = client.get("/s/r1", headers=ENGLISH).text

    assert _html_lang(html) == "en"
    assert t("change_room", "en") in html
    assert t("change_room", "es") not in html
    assert _room_config(html)["defaultLang"] == "en"


def test_room_lang_query_forces_english(client: TestClient) -> None:
    html = client.get("/s/r1?lang=en", headers=SPANISH).text

    assert _html_lang(html) == "en"
    assert t("change_room", "en") in html
    assert t("back_to_live", "en") in html
    config = _room_config(html)
    assert config["defaultLang"] == "en"
    assert config["forcedLang"] == "en"


def test_room_ignores_an_unsupported_lang_query(client: TestClient) -> None:
    html = client.get("/s/r1?lang=xx", headers=ENGLISH).text

    assert _html_lang(html) == "en"
    config = _room_config(html)
    assert config["defaultLang"] == "en"
    assert config["forcedLang"] is None


def test_room_caption_default_falls_back_to_an_available_language(
    client: TestClient,
) -> None:
    # r3 only has Spanish captions; an English reader still gets Spanish ones.
    html = client.get("/s/r3", headers=ENGLISH).text

    assert _html_lang(html) == "en"
    assert _room_config(html)["defaultLang"] == "es"


def test_room_config_gives_room_js_what_it_needs(client: TestClient) -> None:
    config = _room_config(client.get("/s/r1", headers=SPANISH).text)

    assert config["slug"] == "r1"
    assert config["streamBase"] == "/api/stream/r1/"
    assert config["langs"] == ["en", "es"]
    assert config["source"] == "en"
    assert config["talkId"] == "t1"
    assert config["endonyms"] == {"en": "English", "es": "Español"}
    assert config["langNames"] == {"en": "inglés", "es": "español"}
    assert config["i18n"]["back_to_live"] == t("back_to_live", "es")


def test_room_page_loads_the_design_system_and_room_js(client: TestClient) -> None:
    html = client.get("/s/r1", headers=SPANISH).text

    assert 'href="/static/css/glosa.css"' in html
    assert 'src="/static/js/room.js"' in html
    assert client.get("/static/css/glosa.css").status_code == 200
    assert client.get("/static/js/room.js").status_code == 200


def test_audience_pages_link_only_assets_that_exist(client: TestClient) -> None:
    # The v2 design system is one stylesheet (glosa.css, room.css was folded
    # into it). Every local stylesheet and script a page links must be served.
    for path in ("/", "/s/r1", "/s/r2", "/s/nope"):
        html = client.get(path, headers=SPANISH).text
        assert 'href="/static/css/glosa.css"' in html
        refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
        assert refs
        for ref in refs:
            assert client.get(ref).status_code == 200, f"{path} links a missing {ref}"


def test_room_lists_every_room_on_the_side(client: TestClient) -> None:
    html = client.get("/s/r2", headers=SPANISH).text

    assert 'class="room-nav"' in html
    for room in ROOMS:
        assert f'href="/s/{room["slug"]}"' in html
    assert re.search(r'href="/s/r2"[^>]*aria-current="page"', html)


def test_room_without_a_talk_says_when_the_next_one_starts(client: TestClient) -> None:
    html = client.get("/s/r2", headers=SPANISH).text

    assert t("no_talk_now", "es") in html
    assert "15:30" in html
    assert _room_config(html)["talkId"] is None


def test_room_labels_the_language_selector_and_controls(client: TestClient) -> None:
    html = client.get("/s/r1", headers=ENGLISH).text

    assert t("caption_language", "en") in html
    assert t("larger", "en") in html
    assert t("smaller", "en") in html
    assert t("fullscreen", "en") in html
    assert f'<span class="control__word">{t("theme_button", "en")}</span>' in html


def test_unknown_room_is_a_404_page(client: TestClient) -> None:
    response = client.get("/s/nope", headers=SPANISH)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert t("room_not_found", "es") in response.text
    assert 'href="/"' in response.text


def test_talk_titles_are_escaped() -> None:
    rooms = [
        {
            "slug": "x",
            "name": "Sala <b>X</b>",
            "langs": ["es"],
            "now": {
                "talk_id": "tx",
                "title": "<script>alert(1)</script>",
                "speakers": ["</script><script>alert(2)</script>"],
                "language": "es",
            },
            "next": None,
        }
    ]
    client = TestClient(_make_app(rooms=rooms))

    for path in ("/", "/s/x"):
        html = client.get(path, headers=SPANISH).text
        assert "<script>alert(1)</script>" not in html
        assert "<script>alert(2)</script>" not in html
        assert "Sala <b>X</b>" not in html


# ---- event branding ----------------------------------------------------------


def test_branding_colors_become_css_variables() -> None:
    branding = {
        "event_name": "PyCon AR",
        "primary": "#007673",
        "accent": "#00ACA8",
        "logo_url": "/static/logo.svg",
    }
    client = TestClient(_make_app(branding=branding))

    for path in ("/", "/s/r1"):
        html = client.get(path, headers=SPANISH).text
        assert "--brand-primary: #007673" in html
        assert "--brand-accent: #00ACA8" in html
        assert "--brand-on-primary: #FFFFFF" in html
        assert "--brand-on-accent: #000000" in html
        assert 'src="/static/logo.svg"' in html
        assert 'alt="PyCon AR"' in html


def test_default_branding_injects_no_colors(client: TestClient) -> None:
    html = client.get("/", headers=SPANISH).text

    assert "--brand-primary:" not in html
    assert "--brand-accent:" not in html


def test_invalid_branding_colors_are_ignored() -> None:
    branding = {
        "event_name": "X",
        "primary": "red; } body { display: none",
        "accent": "#12",
        "logo_url": None,
    }
    client = TestClient(_make_app(branding=branding))

    html = client.get("/", headers=SPANISH).text

    assert "--brand-primary:" not in html
    assert "--brand-accent:" not in html
    assert "display: none" not in html
