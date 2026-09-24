"""Tests for glosa.i18n: the ES/EN string table and Accept-Language detection."""

from __future__ import annotations

import pytest

from glosa import i18n
from glosa.i18n import detect_lang, endonym, join_names, lang_name, t


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
