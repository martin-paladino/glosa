"""Audience pages: the public room list (`/`), the room view (`/s/{slug}`),
the OBS/vMix overlay (`/overlay/{room}`) and the printable QR page
(`/qr/{room}`). Task 14b.

Task 5's create_app() includes `router` and provides on app.state:
  - rooms_view(): list[dict] with {"slug", "name", "langs", "now", "next"}
    (now/next: {"talk_id", "title", "speakers", "language"}, next also "start": "HH:MM");
  - branding: {"event_name", "primary", "accent", "logo_url"};
  - workers: room id -> RoomWorker (Task 14b: the overlay, the QR page's
    `?token=` in `qr_only` mode, and the room page's admin-only "Escuchar
    el audio" flag all need the worker directly, not just its audience-safe
    `view()`). Accessed defensively (``getattr``/``or {}``): a handful of
    older, narrower test fixtures (tests/web/test_pages.py) build a bare
    app with only ``rooms_view`` and ``branding`` set, and must keep working
    unchanged -- with no ``workers``, this module just falls back to the
    pre-14b behaviour (slug-only room lookup, no token fallback, "listen"
    always off).

Interface language: `?lang=es|en` wins, then Accept-Language, then Spanish.
Live captions arrive over SSE (`/api/stream/{slug}/{lang}`), handled by
static/js/room.js; the page embeds what room.js needs as JSON.

``Settings.audience_mode`` (glosa/config.py): "all" (default) or "qr_only".
In `qr_only`, `/` lists no rooms and `/s/{slug}` 404s -- only `/s/{token}`
(a room's ``Room.public_token``) works, which is what `/qr/{room}` then
encodes instead of the slug (plan case 14.2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from glosa.i18n import (
    STRINGS,
    SUPPORTED,
    Lang,
    detect_lang,
    endonym,
    join_names,
    lang_name,
    t,
)
from glosa.web.admin_api import public_base, qr_data_uri
from glosa.web.auth import is_authenticated

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

SOURCE_URL = "https://github.com/martin-paladino/glosa"

_VARY = {"Vary": "Accept-Language"}
_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


# ---- routes ------------------------------------------------------------------


@router.get("/", include_in_schema=False)
def index(request: Request):
    ui, forced = _ui_lang(request)
    # qr_only: "/" no lista salas (plan case 14.2) -- the empty state
    # ("rooms_empty") already reads "scan the QR code on the screen".
    rooms = (
        []
        if _audience_mode(request) == "qr_only"
        else [_room_summary(room, ui, forced) for room in _rooms(request)]
    )
    context = _base_context(request, ui, forced) | {"rooms": rooms}
    return templates.TemplateResponse(request, "index.html", context, headers=_VARY)


@router.get("/s/{slug}", include_in_schema=False)
def room_page(slug: str, request: Request):
    ui, forced = _ui_lang(request)
    all_rooms = _rooms(request)
    workers = _workers(request)
    qr_only = _audience_mode(request) == "qr_only"

    room = None if qr_only else next((r for r in all_rooms if r.get("slug") == slug), None)
    worker = None
    if room is None and workers:
        # qr_only: `/s/{slug}` 404s, `/s/{token}` works (plan case 14.2).
        # Tried in "all" mode too, harmlessly: a token is never a valid
        # slug, so this only ever matches when the slug lookup above did not.
        worker = next((w for w in workers.values() if getattr(w.room, "public_token", None) == slug), None)
        if worker is not None:
            room = next((r for r in all_rooms if r.get("slug") == worker.room.slug), None)

    base = _base_context(request, ui, forced)
    if room is None:
        return templates.TemplateResponse(
            request, "not_found.html", base, status_code=404, headers=_VARY
        )

    summary = _room_summary(room, ui, forced)
    now = room.get("now")
    langs = list(room.get("langs") or [])
    requested = request.query_params.get("lang")
    forced_caption = requested if requested in langs else None
    caption_lang = forced_caption or _default_caption_lang(ui, langs, now)
    source = now["language"] if now else None

    config = {
        "slug": room["slug"],
        "streamBase": f"/api/stream/{quote(room['slug'], safe='')}/",
        "langs": langs,
        "defaultLang": caption_lang,
        "forcedLang": forced_caption,
        "source": source,
        "talkId": now["talk_id"] if now else None,
        "endonyms": {code: endonym(code) for code in langs},
        "langNames": {code: lang_name(code, ui) for code in langs},
        "i18n": STRINGS[ui],
    }

    # Task 14b, Ruling 5: "Escuchar el audio" on the public page -- only
    # ever true with a valid admin session AND the room already playing a
    # test file at this render. Both the markup and listen.js are left out
    # entirely otherwise (room.html), not just hidden.
    if worker is None and workers:
        worker = _find_worker(request, room["slug"])
    listen = worker is not None and is_authenticated(request) and worker.test_file() is not None

    context = base | {
        "room": summary,
        "nav": [
            _room_summary(r, ui, forced) | {"current": r is room} for r in all_rooms
        ],
        "caption_lang": caption_lang,
        "source": source,
        "direction": _direction(source, caption_lang, ui),
        "lang_options": _lang_options(langs, source, ui),
        "config": config,
        "listen": listen,
    }
    return templates.TemplateResponse(request, "room.html", context, headers=_VARY)


@router.get("/overlay/{slug}", include_in_schema=False)
def overlay_page(slug: str, request: Request):
    """`/overlay/{room}?lang=&lines=&size=&logo=1`: the transparent,
    chrome-less page a vMix browser input or an OBS browser source reads
    (docs/design/overlay.html; static/js/overlay.js). Not gated by
    ``qr_only`` -- it's for the production booth, not the audience."""
    worker = _find_worker(request, slug)
    if worker is None:
        raise HTTPException(status_code=404)
    langs = worker.langs()
    requested = request.query_params.get("lang")
    lang = requested if requested in langs else (langs[0] if langs else "es")
    branding = getattr(request.app.state, "branding", None) or {}
    context = {
        "lang": lang,
        "lines": _clamp_int(request.query_params.get("lines"), default=2, lo=1, hi=4),
        "size": _clamp_int(request.query_params.get("size"), default=48, lo=16, hi=120),
        "show_logo": request.query_params.get("logo") == "1",
        "logo_url": branding.get("logo_url"),
        "brand_css": _brand_css(branding),
        "config": {"streamBase": f"/api/stream/{quote(worker.room.slug, safe='')}/", "lang": lang},
    }
    return templates.TemplateResponse(request, "overlay.html", context)


@router.get("/qr/{slug}", include_in_schema=False)
def qr_page(slug: str, request: Request):
    """`/qr/{room}`: a printable/projectable page with the room's QR --
    `/s/{slug}`, or `/s/{token}` in `qr_only` mode (plan case 14.1).

    Deliberately public, no admin session required, both to print this from
    a kiosk browser and because the brief's own interface lists it as a
    plain page (not one of the `/api/admin/*` routes). One consequence in
    `qr_only` mode worth flagging for anyone tightening the threat model
    later: this route is still keyed by the room's plain `slug`, so a
    caller who can guess or already knows a room's slug (its config.yaml
    id, not usually treated as secret) gets straight to that room's
    unguessable `public_token` here -- `qr_only`'s guarantee is "not
    listed and not guessable from the room's own link", not "the token
    is unobtainable by anyone who knows the room exists"."""
    ui, forced = _ui_lang(request)
    worker = _find_worker(request, slug)
    base = _base_context(request, ui, forced)
    if worker is None:
        return templates.TemplateResponse(request, "not_found.html", base, status_code=404, headers=_VARY)
    key = worker.room.public_token if _audience_mode(request) == "qr_only" else worker.room.slug
    url = f"{public_base(request)}/s/{quote(key, safe='')}"
    context = base | {
        "room_name": worker.room.name,
        "qr_data_uri": qr_data_uri(url),
        "qr_url": url,
    }
    return templates.TemplateResponse(request, "qr.html", context, headers=_VARY)


# ---- helpers -------------------------------------------------------------------


def _rooms(request: Request) -> list[dict]:
    return list(request.app.state.rooms_view())


def _workers(request: Request) -> dict:
    """room id -> RoomWorker, or {} for the narrower test fixtures that
    don't set ``app.state.workers`` at all (see the module docstring)."""
    return getattr(request.app.state, "workers", None) or {}


def _find_worker(request: Request, slug: str):
    return next((w for w in _workers(request).values() if w.room.slug == slug), None)


def _audience_mode(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    return getattr(settings, "audience_mode", "all") if settings is not None else "all"


def _clamp_int(raw: str | None, *, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _ui_lang(request: Request) -> tuple[Lang, Lang | None]:
    """(interface language, the one forced by ?lang or None)."""
    requested = request.query_params.get("lang")
    forced = cast(Lang, requested) if requested in SUPPORTED else None
    return forced or detect_lang(request.headers.get("accept-language")), forced


def _base_context(request: Request, ui: Lang, forced: Lang | None) -> dict:
    branding = getattr(request.app.state, "branding", None) or {}
    other = "en" if ui == "es" else "es"
    return {
        "ui": ui,
        "tr": lambda key: t(key, ui),
        "lang_suffix": f"?lang={forced}" if forced else "",
        "other_lang": other,
        "other_lang_name": endonym(other),
        "event_name": branding.get("event_name") or "Glosa",
        "logo_url": branding.get("logo_url"),
        "brand_css": _brand_css(branding),
        "source_url": SOURCE_URL,
    }


def _room_summary(room: dict, ui: Lang, forced: Lang | None) -> dict:
    """A room as the templates show it: status, now and next, ready-made texts."""
    langs = list(room.get("langs") or [])
    now, nxt = room.get("now"), room.get("next")
    return {
        "slug": room["slug"],
        "name": room["name"],
        "href": f"/s/{quote(room['slug'], safe='')}" + (f"?lang={forced}" if forced else ""),
        "live": now is not None,
        "now": _talk(now, langs, ui) if now else None,
        "next": _talk(nxt, langs, ui) if nxt else None,
    }


def _talk(talk: dict, langs: list[str], ui: Lang) -> dict:
    speakers = join_names(list(talk.get("speakers") or []), ui)
    langs_text = _describe_langs(talk.get("language", ""), langs, ui)
    return talk | {
        "speakers_text": speakers,
        "langs_text": langs_text,
        "meta": f"{speakers}. {langs_text}" if speakers else langs_text,
    }


def _describe_langs(talk_lang: str, langs: list[str], ui: Lang) -> str:
    """"En inglés, con subtítulos en español." / "In English, with Spanish captions." """
    text = t("in_lang", ui).format(lang=lang_name(talk_lang, ui))
    others = [lang_name(code, ui) for code in langs if code != talk_lang]
    if others:
        text += ", " + t("with_captions", ui).format(langs=join_names(others, ui))
    return text + "."


def _default_caption_lang(ui: Lang, langs: list[str], now: dict | None) -> str:
    if ui in langs:
        return ui
    if now and now.get("language") in langs:
        return now["language"]
    return langs[0] if langs else ui


def _direction(source: str | None, caption_lang: str, ui: Lang) -> dict:
    """Header badge: EN → ES, or just ES when reading the original."""
    if not source or source == caption_lang:
        code = source or caption_lang
        return {
            "codes": [code],
            "label": t("original_language", ui).format(lang=lang_name(code, ui)),
            "names": [lang_name(code, ui)],
        }
    return {
        "codes": [source, caption_lang],
        "label": t("direction", ui).format(
            src=lang_name(source, ui), dst=lang_name(caption_lang, ui)
        ),
        "names": [lang_name(source, ui), lang_name(caption_lang, ui)],
    }


def _lang_options(langs: list[str], source: str | None, ui: Lang) -> list[dict]:
    options = [
        {
            "value": code,
            "label": endonym(code) + (f" ({t('original', ui)})" if code == source else ""),
        }
        for code in langs
    ]
    if len(langs) >= 2:
        options.append({"value": "bilingual", "label": t("bilingual", ui)})
    return options


def _brand_css(branding: dict) -> list[tuple[str, str]]:
    """CSS custom properties for the event's colors. Only #hex values are accepted."""
    css: list[tuple[str, str]] = []
    for key in ("primary", "accent"):
        value = branding.get(key)
        if isinstance(value, str) and _HEX_COLOR.fullmatch(value.strip()):
            value = value.strip()
            css.append((f"--brand-{key}", value))
            css.append((f"--brand-on-{key}", _on_color(value)))
    return css


def _on_color(hex_color: str) -> str:
    """Black or white, whichever contrasts more with hex_color (WCAG luminance)."""
    digits = hex_color.lstrip("#")
    if len(digits) in (3, 4):
        digits = "".join(ch * 2 for ch in digits[:3])
    r, g, b = (int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
    on_white = 1.05 / (luminance + 0.05)
    on_black = (luminance + 0.05) / 0.05
    return "#FFFFFF" if on_white >= on_black else "#000000"
