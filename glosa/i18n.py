"""User-facing strings in Spanish and English, plus Accept-Language detection.

Technical names (routes, config keys, query params) stay in English; only
what a person reads goes through here. Spanish is rioplatense (voseo) and the
default. Some strings are templates for str.format (e.g. "{time}").
"""

from __future__ import annotations

from typing import Literal, cast

Lang = Literal["es", "en"]

DEFAULT_LANG: Lang = "es"
SUPPORTED: tuple[Lang, ...] = ("es", "en")

STRINGS: dict[str, dict[str, str]] = {
    "es": {
        # Navigation and brand
        "all_rooms": "Glosa, todas las salas",
        "change_room": "Cambiar de sala",
        "rooms": "Salas",
        "index_title": "{event}, subtítulos en vivo",
        "index_lede": "Subtítulos en vivo en cada sala, traducidos a tu idioma. Elegí una sala y leé desde tu celular.",
        "rooms_empty": "No hay salas para mostrar. Si estás en una sala, escaneá el código QR que está en la pantalla.",
        "room_title": "{room}, en vivo",
        "room_not_found": "No encontramos esa sala.",
        "see_all_rooms": "Ver todas las salas",
        # Agenda
        "now": "Ahora",
        "next": "Próxima",
        "no_talk_now": "No hay charla en esta sala.",
        "no_more_talks": "No hay más charlas en esta sala.",
        "starts_at": "Empieza a las {time}.",
        "next_talk_at": "La próxima charla empieza a las {time}.",
        "in_lang": "En {lang}",
        "with_captions": "con subtítulos en {langs}",
        "and": "y",
        # Status (same words as the status lamp in the design system)
        "live": "En vivo",
        "reconnecting": "Reconectando",
        "between_talks": "Entre charlas",
        # Transcript
        "captions_in": "Subtítulos en {lang}",
        "captions_in_two": "Subtítulos en {a} y en {b}",
        "said_so_far": "Lo que ya se dijo",
        "waiting_captions": "Los subtítulos aparecen acá en cuanto alguien hable.",
        "direction": "Del {src} al {dst}",
        "original_language": "En {lang}, idioma original",
        "original": "original",
        "translation": "traducción",
        "back_to_live": "Volver al vivo",
        "no_js": "Para ver los subtítulos en vivo, activá JavaScript.",
        # Controls
        "reading_controls": "Lectura",
        "caption_language": "Idioma de los subtítulos",
        "bilingual": "Bilingüe",
        "font_size": "Tamaño de letra",
        "smaller": "Achicar letra",
        "larger": "Agrandar letra",
        "theme": "Tema: {name}. Cambiar tema",
        "theme_system": "del sistema",
        "theme_light": "claro",
        "theme_dark": "oscuro",
        "theme_contrast": "alto contraste",
        "fullscreen": "Pantalla completa",
        "shortcut_fullscreen": "pantalla completa",
        "shortcut_size": "tamaño de letra",
        # Footer
        "colophon_word": "glosa",
        "colophon_def": "(f.): nota que explica o traduce un texto.",
        "free_software": "Glosa es software libre.",
        "view_source": "Ver el código",
    },
    "en": {
        # Navigation and brand
        "all_rooms": "Glosa, all rooms",
        "change_room": "Change room",
        "rooms": "Rooms",
        "index_title": "{event}, live captions",
        "index_lede": "Live captions in every room, translated into your language. Pick a room and read along on your phone.",
        "rooms_empty": "There are no rooms to show. If you are in a room, scan the QR code on the screen.",
        "room_title": "{room}, live",
        "room_not_found": "We couldn't find that room.",
        "see_all_rooms": "See all rooms",
        # Agenda
        "now": "Now",
        "next": "Next",
        "no_talk_now": "No talk in this room right now.",
        "no_more_talks": "No more talks in this room.",
        "starts_at": "Starts at {time}.",
        "next_talk_at": "The next talk starts at {time}.",
        "in_lang": "In {lang}",
        "with_captions": "with {langs} captions",
        "and": "and",
        # Status
        "live": "Live",
        "reconnecting": "Reconnecting",
        "between_talks": "Between talks",
        # Transcript
        "captions_in": "Captions in {lang}",
        "captions_in_two": "Captions in {a} and {b}",
        "said_so_far": "What was said so far",
        "waiting_captions": "Captions show up here as soon as someone speaks.",
        "direction": "From {src} to {dst}",
        "original_language": "In {lang}, the original language",
        "original": "original",
        "translation": "translation",
        "back_to_live": "Back to live",
        "no_js": "Turn on JavaScript to see live captions.",
        # Controls
        "reading_controls": "Reading",
        "caption_language": "Caption language",
        "bilingual": "Bilingual",
        "font_size": "Text size",
        "smaller": "Smaller text",
        "larger": "Larger text",
        "theme": "Theme: {name}. Change theme",
        "theme_system": "system",
        "theme_light": "light",
        "theme_dark": "dark",
        "theme_contrast": "high contrast",
        "fullscreen": "Full screen",
        "shortcut_fullscreen": "full screen",
        "shortcut_size": "text size",
        # Footer
        "colophon_word": "glosa",
        "colophon_def": "(Spanish, f.): a note that explains or translates a text.",
        "free_software": "Glosa is free software.",
        "view_source": "View the source",
    },
}

# Language names as they appear inside a sentence of the interface language
# ("con subtítulos en español", "with Spanish captions").
LANG_NAMES: dict[str, dict[str, str]] = {
    "es": {
        "es": "español",
        "en": "inglés",
        "pt": "portugués",
        "fr": "francés",
        "de": "alemán",
        "it": "italiano",
    },
    "en": {
        "es": "Spanish",
        "en": "English",
        "pt": "Portuguese",
        "fr": "French",
        "de": "German",
        "it": "Italian",
    },
}

# Each language's name in itself, for the language selector.
ENDONYMS: dict[str, str] = {
    "es": "Español",
    "en": "English",
    "pt": "Português",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
}


def t(key: str, lang: Lang) -> str:
    """The text for `key` in `lang`. Unknown keys raise KeyError on purpose."""
    return STRINGS[lang if lang in STRINGS else DEFAULT_LANG][key]


def detect_lang(accept_language: str | None) -> Lang:
    """Pick "es" or "en" from an Accept-Language header, by q-value.

    Ties keep header order; q=0 and malformed q-values don't count.
    Anything else (no header, only other languages, "*") gives "es".
    """
    if not accept_language:
        return DEFAULT_LANG
    best: str | None = None
    best_q = 0.0
    for part in accept_language.split(","):
        tag, _, params = part.strip().partition(";")
        primary = tag.strip().lower().split("-", 1)[0]
        if primary not in SUPPORTED:
            continue
        q = _quality(params)
        if q > best_q:
            best, best_q = primary, q
    return cast(Lang, best) if best else DEFAULT_LANG


def _quality(params: str) -> float:
    for param in params.split(";"):
        name, _, value = param.strip().partition("=")
        if name.strip().lower() == "q":
            try:
                q = float(value)
            except ValueError:
                return 0.0
            return q if 0.0 <= q <= 1.0 else 0.0
    return 1.0


def lang_name(code: str, lang: Lang) -> str:
    """Name of language `code` as written in a `lang` sentence ("inglés")."""
    return LANG_NAMES.get(lang, LANG_NAMES[DEFAULT_LANG]).get(code, code.upper())


def endonym(code: str) -> str:
    """Name of language `code` in that language ("Español", "English")."""
    return ENDONYMS.get(code, code.upper())


def join_names(names: list[str], lang: Lang) -> str:
    """"A, B y C" / "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} {t('and', lang)} {names[-1]}"
