"""glosa/room_text.py: the languages of a talk, the default engine, and the
rule that switches a fast talk to the glossary engine (case 10.5)."""

from __future__ import annotations

from glosa.config import Settings
from glosa.room_text import FlapDetector, default_engine, translation_langs


def _stats(reconnects: int = 0, **errors: int) -> dict:
    return {"rotations": 0, "reconnects": reconnects, "errors": {int(code[1:]): n for code, n in errors.items()}}


def test_translation_langs_put_live_translates_target_first() -> None:
    assert translation_langs("en", ["es"]) == ["es"]
    assert translation_langs("en", ["en", "es", "pt", "es"]) == ["es", "pt"]
    assert translation_langs("es", []) == ["en"]
    assert translation_langs("en", ["en"]) == ["es"]


def test_default_engine_by_language() -> None:
    settings = Settings(gemini_api_key="k", admin_password="test-password")
    assert default_engine("es", settings) == "glossary"
    assert default_engine("en", settings) == "fast"
    assert default_engine("en", settings.model_copy(update={"default_engine_en": "glossary"})) == "glossary"


def test_three_incidents_within_the_window_flap() -> None:
    flaps = FlapDetector(limit=3, window_s=120.0)
    assert not flaps.update(_stats(1), manual=0, now=10.0)  # a stall
    assert not flaps.update(_stats(1, e0=1), manual=0, now=20.0)  # a failed connect (errors only)
    assert flaps.update(_stats(2, e0=1), manual=0, now=129.0)  # a session that closed on its own


def test_incidents_older_than_the_window_do_not_count() -> None:
    flaps = FlapDetector(limit=3, window_s=120.0)
    assert not flaps.update(_stats(1), manual=0, now=0.0)
    assert not flaps.update(_stats(2), manual=0, now=60.0)
    assert not flaps.update(_stats(3), manual=0, now=121.0)  # the first one is 121 s old
    assert flaps.update(_stats(4), manual=0, now=150.0)


def test_an_active_session_dying_with_an_error_is_one_incident_not_two() -> None:
    """Its error is counted in stats["errors"] and its death in
    stats["reconnects"], at the same moment."""
    flaps = FlapDetector(limit=3, window_s=120.0)
    assert not flaps.update(_stats(1, e1011=1), manual=0, now=1.0)
    assert not flaps.update(_stats(2, e1011=2), manual=0, now=5.0)
    assert flaps.update(_stats(3, e1011=3), manual=0, now=9.0)


def test_manual_reconnects_and_payment_errors_do_not_count() -> None:
    flaps = FlapDetector(limit=3, window_s=120.0)
    assert not flaps.update(_stats(1), manual=1, now=1.0)  # the admin's "Reconectar"
    assert not flaps.update(_stats(2, e402=1), manual=1, now=2.0)  # credit exhausted: a fallback would not help
    assert not flaps.update(_stats(2, e402=3), manual=1, now=60.0)
    assert not flaps.update(_stats(4, e402=3), manual=3, now=70.0)  # stats are cumulative
    assert not flaps.update(_stats(5, e402=3), manual=3, now=80.0)  # one real incident so far
    assert not flaps.update(_stats(5, e402=3, e0=1), manual=3, now=81.0)
    assert flaps.update(_stats(6, e402=3, e0=1), manual=3, now=82.0)
