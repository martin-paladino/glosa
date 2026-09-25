"""Tests for glosa.i18n: the ES/EN string table and Accept-Language detection."""

from __future__ import annotations

import pytest

from glosa import i18n
from glosa.i18n import detect_lang, endonym, join_names, lang_name, resolve_ui_lang, t


def test_every_key_exists_in_both_languages() -> None:
    assert set(i18n.STRINGS["es"]) == set(i18n.STRINGS["en"])


def test_t_returns_the_text_for_each_language() -> None:
    assert t("now", "es") == "Ahora"
    assert t("now", "en") == "Now"
    assert t("next", "es") == "Próxima"
    assert t("next", "en") == "Next"
    assert t("back_to_live", "es") == "Volver al vivo"
    assert t("back_to_live", "en") == "Back to live"


def test_t_raises_on_an_unknown_key() -> None:
    with pytest.raises(KeyError):
        t("no_such_key", "es")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "es"),
        ("", "es"),
        ("en-US,en;q=0.9", "en"),
        ("es-AR,es;q=0.9,en;q=0.8", "es"),
        ("fr-FR,fr;q=0.9,en;q=0.8", "en"),
        ("de", "es"),
        ("en;q=0.2, es;q=0.8", "es"),
        ("EN-gb", "en"),
        ("en;q=0", "es"),
        ("*", "es"),
        ("en;q=abc", "es"),
        ("en,es", "en"),
        ("pt-BR, en-GB;q=0.5, es;q=0.5", "en"),
    ],
)
def test_detect_lang(header: str | None, expected: str) -> None:
    assert detect_lang(header) == expected


def test_language_names_follow_the_interface_language() -> None:
    assert lang_name("en", "es") == "inglés"
    assert lang_name("es", "en") == "Spanish"
    assert lang_name("xx", "es") == "XX"


def test_endonyms_are_capitalised_in_their_own_language() -> None:
    assert endonym("es") == "Español"
    assert endonym("en") == "English"
    assert endonym("xx") == "XX"


@pytest.mark.parametrize(
    ("names", "lang", "expected"),
    [
        ([], "es", ""),
        (["Ana Sosa"], "es", "Ana Sosa"),
        (["Ana Sosa", "Pablo Díaz"], "es", "Ana Sosa y Pablo Díaz"),
        (["A", "B", "C"], "es", "A, B y C"),
        (["A", "B", "C"], "en", "A, B and C"),
    ],
)
def test_join_names(names: list[str], lang: str, expected: str) -> None:
    assert join_names(names, lang) == expected


def test_every_admin_key_exists_in_both_languages() -> None:  # Task 12
    assert set(i18n.ADMIN_STRINGS["es"]) == set(i18n.ADMIN_STRINGS["en"])


def test_admin_plurals() -> None:
    assert i18n.admin_plural("pending", 1, "es") == "1 pendiente"
    assert i18n.admin_plural("pending", 3, "es") == "3 pendientes"
    assert i18n.admin_plural("panels", 2, "en") == "2 panels open"
    assert i18n.admin_plural("tally_live", 2, "es") == "en vivo"


# ---- resolve_ui_lang (Ruling 63) ---------------------------------------------


def _resolve(query=None, cookie=None, configured="auto", accept=None):
    return resolve_ui_lang(
        query_lang=query, cookie_lang=cookie, configured=configured, accept_language=accept
    )


def test_query_lang_always_wins_and_is_returned_as_forced() -> None:
    assert _resolve(query="en", cookie="es", configured="es", accept="es") == ("en", "en")
    assert _resolve(query="es", cookie="en", configured="en", accept="en") == ("es", "es")


def test_an_unsupported_query_lang_is_ignored() -> None:
    assert _resolve(query="fr", configured="es") == ("es", None)


def test_cookie_wins_over_the_configured_default_when_no_query_lang() -> None:
    assert _resolve(cookie="en", configured="es", accept="es") == ("en", None)
    assert _resolve(cookie="es", configured="en", accept="en") == ("es", None)


def test_configured_es_or_en_ignores_accept_language() -> None:
    assert _resolve(configured="es", accept="en-US,en;q=0.9") == ("es", None)
    assert _resolve(configured="en", accept="es-AR,es;q=0.9") == ("en", None)


def test_configured_auto_or_missing_falls_back_to_accept_language() -> None:
    assert _resolve(configured="auto", accept="en-US,en;q=0.9") == ("en", None)
    assert _resolve(configured="auto", accept=None) == ("es", None)


def test_unsupported_cookie_value_is_ignored() -> None:
    assert _resolve(cookie="fr", configured="auto", accept="en") == ("en", None)
