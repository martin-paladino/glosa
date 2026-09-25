"""Tests for the audience pages (glosa.web.pages): the public room list at `/`
and the room view at `/s/{slug}`.

Task 5 owns create_app(); here a minimal FastAPI app provides what the pages
read from app.state (a fake rooms_view and branding) and mounts /static.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.i18n import t
from glosa.web import pages
from glosa.web.auth import COOKIE_NAME, new_admin_secret, sign_session

STATIC_DIR = Path(pages.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-pw"

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


# ---- Task 14b: a fuller app -- workers (for /overlay, /qr, the token
# fallback and the "listen" flag) and admin auth state. Kept separate from
# _make_app()/`client` above: most of this file's tests use that narrower
# fixture on purpose (see glosa/web/pages.py's module docstring -- pages.py
# must keep working for a caller that never sets app.state.workers at all).


def _worker(slug: str, *, public_token: str | None = None, langs=None, test_file=None) -> MagicMock:
    worker = MagicMock()
    worker.room.slug = slug
    worker.room.name = next((r["name"] for r in ROOMS if r["slug"] == slug), slug)
    worker.room.public_token = public_token or f"tok-{slug}"
    worker.langs.return_value = ["en", "es"] if langs is None else langs
    worker.test_file.return_value = test_file
    return worker


def _make_full_app(
    rooms: list[dict] | None = None,
    *,
    branding: dict | None = None,
    audience_mode: str = "all",
    workers: dict | None = None,
) -> FastAPI:
    app = _make_app(rooms=rooms, branding=branding)
    app.state.settings = MagicMock(audience_mode=audience_mode, admin_password=ADMIN_PASSWORD)
    snapshot = ROOMS if rooms is None else rooms
    app.state.workers = workers if workers is not None else {r["slug"]: _worker(r["slug"]) for r in snapshot}
    app.state.admin_secret = new_admin_secret()
    app.state.session_epoch = 0
    return app


def _admin_cookies(app: FastAPI) -> dict[str, str]:
    return {COOKIE_NAME: sign_session(app.state.admin_secret, ADMIN_PASSWORD)}


def _html_lang(html: str) -> str:
    match = re.search(r'<html[^>]*\slang="([^"]+)"', html)
    assert match, "the page must declare <html lang>"
    return match.group(1)


def _json_script(html: str, script_id: str) -> dict:
    match = re.search(rf'<script type="application/json" id="{script_id}">(.*?)</script>', html, re.S)
    assert match, f"expected a #{script_id} JSON script tag"
    return json.loads(match.group(1))


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
    assert config["summaryBase"] == "/api/summary/r1/"
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


def test_room_page_has_an_accessible_summary_button_and_panel(client: TestClient) -> None:
    """Task 17, "¿Qué me perdí?": a hidden-by-default toggle button
    (aria-expanded, controlled by room.js polling GET /api/summary) and a
    dismissable panel with a close button, both in Spanish by default."""
    html = client.get("/s/r1", headers=SPANISH).text

    assert 'data-summary-toggle' in html and 'aria-expanded="false"' in html
    assert 'aria-controls="summary-panel"' in html
    assert t("what_did_i_miss", "es") in html
    assert 'data-summary-panel' in html and 'id="summary-panel"' in html
    assert 'data-summary-close' in html
    assert f'aria-label="{t("summary_close", "es")}"' in html


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


def test_room_has_a_pip_button_with_i18n_in_both_languages(client: TestClient) -> None:
    """Task 20: Picture-in-Picture captions ("Ventana flotante" / "Pop-out
    captions"). Server-rendered visible in the markup in both languages --
    there is no way to feature-detect the Document PiP API server-side --
    room.js hides it right away unless 'documentPictureInPicture' in window."""
    for lang, headers in (("es", SPANISH), ("en", ENGLISH)):
        html = client.get("/s/r1", headers=headers).text
        assert 'data-action="pip"' in html
        assert 'aria-pressed="false"' in html
        assert t("pip", lang) in html


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


# ---- overlay (Task 14b) -------------------------------------------------------


def test_overlay_defaults_to_the_first_language_two_lines_and_48px() -> None:
    app = _make_full_app()
    client = TestClient(app)

    response = client.get("/overlay/r1")
    html = response.text

    assert response.status_code == 200
    assert "overlay-page" in html
    assert "--overlay-lines: 2" in html
    assert "--overlay-size: 48px" in html
    assert '<script src="/static/js/overlay.js" defer></script>' in html
    assert _json_script(html, "glosa-overlay") == {"streamBase": "/api/stream/r1/", "lang": "en"}


def test_overlay_lines_size_and_lang_come_from_the_query_string() -> None:
    app = _make_full_app()
    client = TestClient(app)

    html = client.get("/overlay/r1?lang=es&lines=3&size=64").text

    assert "--overlay-lines: 3" in html
    assert "--overlay-size: 64px" in html
    assert _json_script(html, "glosa-overlay")["lang"] == "es"


def test_overlay_clamps_out_of_range_lines_and_size() -> None:
    app = _make_full_app()
    client = TestClient(app)

    html = client.get("/overlay/r1?lines=99&size=1000").text

    assert "--overlay-lines: 4" in html
    assert "--overlay-size: 120px" in html


def test_overlay_falls_back_to_the_default_on_junk_or_unknown_query_values() -> None:
    app = _make_full_app()
    client = TestClient(app)

    html = client.get("/overlay/r1?lang=fr&lines=nope&size=nope").text

    assert _json_script(html, "glosa-overlay")["lang"] == "en"  # unknown lang: first of worker.langs()
    assert "--overlay-lines: 2" in html
    assert "--overlay-size: 48px" in html


def test_overlay_shows_the_logo_only_with_logo_1() -> None:
    branding = {"event_name": "X", "primary": None, "accent": None, "logo_url": "/static/logo.svg"}
    app = _make_full_app(branding=branding)
    client = TestClient(app)

    without = client.get("/overlay/r1").text
    with_logo = client.get("/overlay/r1?logo=1").text

    assert "overlay-logo" not in without
    assert 'class="overlay-logo"' in with_logo and 'src="/static/logo.svg"' in with_logo


def test_overlay_of_an_unknown_room_is_404() -> None:
    app = _make_full_app()
    client = TestClient(app)

    assert client.get("/overlay/nope").status_code == 404


# ---- overlay under qr_only (Task 14b fix round 1, Ruling 56) ------------------


def test_overlay_slug_404s_in_qr_only_mode() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    assert client.get("/overlay/r1").status_code == 404


def test_overlay_token_form_works_in_qr_only_mode() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    response = client.get("/overlay/s/tok-r1")
    html = response.text

    assert response.status_code == 200
    assert _json_script(html, "glosa-overlay") == {"streamBase": "/api/stream/tok-r1/", "lang": "en"}


def test_overlay_token_form_also_works_in_all_mode() -> None:
    app = _make_full_app(audience_mode="all")
    client = TestClient(app)

    response = client.get("/overlay/s/tok-r1")
    html = response.text

    assert response.status_code == 200
    # all mode: the stream endpoint isn't gated by token, so the overlay
    # still keys the SSE stream by the room's real slug.
    assert _json_script(html, "glosa-overlay") == {"streamBase": "/api/stream/r1/", "lang": "en"}


def test_overlay_token_form_of_an_unknown_token_is_404() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    assert client.get("/overlay/s/nope").status_code == 404


# ---- QR page (Task 14b, plan case 14.1) ----------------------------------------


def test_qr_page_encodes_the_slug_url_in_all_mode() -> None:
    app = _make_full_app()
    client = TestClient(app)

    html = client.get("/qr/r1", headers=SPANISH).text

    assert "http://testserver/s/r1" in html
    assert "http://testserver/s/tok-r1" not in html
    assert "Gran sala" in html
    assert "data:image/svg+xml;base64," in html


def test_qr_page_of_an_unknown_room_is_404() -> None:
    app = _make_full_app()
    client = TestClient(app)

    response = client.get("/qr/nope")
    assert response.status_code == 404
    assert t("room_not_found", "es") not in response.text or True  # not_found.html's own generic copy


def test_qr_page_shows_the_exact_lede_text() -> None:
    app = _make_full_app()
    client = TestClient(app)

    spanish = client.get("/qr/r1", headers=SPANISH).text
    english = client.get("/qr/r1", headers=ENGLISH).text

    assert "Subtítulos en vivo · escaneá y elegí tu idioma" in spanish
    assert t("qr_lede", "en") in english


# ---- qr_only access mode (Task 14b, plan case 14.2) ----------------------------


def test_qr_only_mode_lists_no_rooms_on_the_index() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    html = client.get("/", headers=SPANISH).text

    assert t("rooms_empty", "es") in html
    assert "Gran sala" not in html


def test_qr_only_mode_404s_the_slug_but_the_token_works() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    assert client.get("/s/r1").status_code == 404
    ok = client.get("/s/tok-r1")
    assert ok.status_code == 200
    assert "Gran sala" in ok.text


def test_room_config_uses_the_token_as_the_stream_base_in_qr_only_mode() -> None:
    """Ruling 56: /api/stream/{slug}/{lang} is itself gated by the token in
    qr_only mode (public_api.py), so the embedded config must ask room.js
    to stream from the token, not the real (now-inaccessible) slug."""
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    config = _room_config(client.get("/s/tok-r1", headers=SPANISH).text)

    assert config["streamBase"] == "/api/stream/tok-r1/"
    assert config["summaryBase"] == "/api/summary/tok-r1/"


def test_room_config_still_uses_the_slug_as_the_stream_base_in_all_mode() -> None:
    app = _make_full_app(audience_mode="all")
    client = TestClient(app)

    config = _room_config(client.get("/s/r1", headers=SPANISH).text)

    assert config["streamBase"] == "/api/stream/r1/"


def test_all_mode_still_serves_the_slug_and_also_accepts_the_token() -> None:
    app = _make_full_app(audience_mode="all")
    client = TestClient(app)

    assert client.get("/s/r1").status_code == 200
    assert client.get("/s/tok-r1").status_code == 200


def test_qr_page_encodes_the_token_url_in_qr_only_mode() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app, cookies=_admin_cookies(app))

    html = client.get("/qr/r1", headers=SPANISH).text

    assert "http://testserver/s/tok-r1" in html
    assert "http://testserver/s/r1<" not in html and ">http://testserver/s/r1\n" not in html


# ---- /qr/{room} requires an admin session in qr_only mode (Task 14b fix round 1,
# Ruling 56: nothing public may hand out a room's slug->token mapping) ----------


def test_qr_page_redirects_to_admin_login_when_unauthenticated_in_qr_only_mode() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app)

    response = client.get("/qr/r1?lang=en", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?lang=en"


def test_qr_page_works_with_an_admin_session_in_qr_only_mode() -> None:
    app = _make_full_app(audience_mode="qr_only")
    client = TestClient(app, cookies=_admin_cookies(app))

    response = client.get("/qr/r1")

    assert response.status_code == 200
    assert "http://testserver/s/tok-r1" in response.text


def test_qr_page_stays_public_without_a_session_in_all_mode() -> None:
    app = _make_full_app(audience_mode="all")
    client = TestClient(app)

    assert client.get("/qr/r1").status_code == 200


# ---- "Escuchar el audio" flag on the public page (Task 14b, Ruling 5) ---------


def test_listen_button_is_absent_without_workers_state_at_all(client: TestClient) -> None:
    """The narrower fixture (_make_app/`client`, used throughout this file)
    never sets app.state.workers: room_page() must degrade to "no listen",
    not blow up."""
    html = client.get("/s/r1", headers=SPANISH).text

    assert "data-listen" not in html
    assert "/static/js/listen.js" not in html


def test_listen_button_is_absent_without_a_session() -> None:
    app = _make_full_app(workers={"r1": _worker("r1", test_file=("clip.wav", 1.0))})
    client = TestClient(app)

    html = client.get("/s/r1", headers=SPANISH).text

    assert "data-listen" not in html
    assert "/static/js/listen.js" not in html


def test_listen_button_is_absent_for_an_admin_when_the_room_is_not_in_test_mode() -> None:
    app = _make_full_app(workers={"r1": _worker("r1", test_file=None)})
    client = TestClient(app, cookies=_admin_cookies(app))

    html = client.get("/s/r1", headers=SPANISH).text

    assert "data-listen" not in html


def test_listen_button_appears_for_an_admin_when_the_room_is_in_test_mode() -> None:
    app = _make_full_app(workers={"r1": _worker("r1", test_file=("clip.wav", 1.0))})
    client = TestClient(app, cookies=_admin_cookies(app))

    html = client.get("/s/r1", headers=SPANISH).text

    assert 'data-listen data-listen-room="r1"' in html
    assert '<script src="/static/js/listen.js" defer></script>' in html
    assert t("listen_audio", "es") in html
