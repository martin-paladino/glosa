"""parse_csv: load a Talk agenda from the CSV format documented in
agenda.example.csv (columns: sala, inicio, fin, titulo, speakers, idioma,
destinos, motor, abstract, tags, glosario).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glosa.agenda import AgendaError
from glosa.agenda.csv_import import parse_csv

HEADER = "sala,inicio,fin,titulo,speakers,idioma,destinos,motor,abstract,tags,glosario"
EXAMPLE_CSV_PATH = Path(__file__).resolve().parent.parent.parent / "agenda.example.csv"


def _csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def test_parses_basic_row_into_a_talk() -> None:
    text = _csv(
        "main,2026-09-24 09:55,2026-09-24 10:10,Bienvenida a Nerdearla!,"
        "Eduardo Casarero;Ariel Jolo,es,en,,Edu y Jolo abren el evento,"
        "nerd;apertura,"
    )
    talks = parse_csv(text, tz="America/Argentina/Buenos_Aires")

    assert len(talks) == 1
    talk = talks[0]
    assert talk.room_id == "main"
    assert talk.title == "Bienvenida a Nerdearla!"
    assert talk.speakers == ["Eduardo Casarero", "Ariel Jolo"]
    assert talk.language == "es"
    assert talk.targets == ["en"]
    assert talk.abstract == "Edu y Jolo abren el evento"
    assert talk.tags == ["nerd", "apertura"]
    assert talk.glossary == []
    assert talk.status == "scheduled"
    assert talk.actual_start is None
    assert talk.actual_end is None


def test_timezones_apply_to_naive_start_and_end() -> None:
    text = _csv(
        "main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,,,,"
    )

    ba_talks = parse_csv(text, tz="America/Argentina/Buenos_Aires")
    utc_talks = parse_csv(text, tz="UTC")

    ba_start = ba_talks[0].start
    utc_start = utc_talks[0].start
    assert ba_start.tzinfo is not None
    assert utc_start.tzinfo is not None
    # Same wall-clock digits, different absolute instant (BA is UTC-3).
    assert ba_start.hour == utc_start.hour == 9
    assert ba_start != utc_start
    assert (utc_start - ba_start).total_seconds() == -3 * 3600


def test_destinos_are_split_on_semicolon() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en;pt;fr,,,,")

    talks = parse_csv(text, tz="UTC")

    assert talks[0].targets == ["en", "pt", "fr"]


def test_invalid_language_raises_agenda_error_with_row_number() -> None:
    text = _csv(
        "main,2026-09-24 09:55,2026-09-24 10:10,Charla ok,Speaker,es,en,,,,",
        "main,2026-09-24 11:00,2026-09-24 11:30,Charla mala,Speaker,fr,en,,,,",
    )

    with pytest.raises(AgendaError) as exc_info:
        parse_csv(text, tz="UTC")

    assert exc_info.value.row == 3  # header is row 1, first data row is 2
    assert "idioma" in exc_info.value.reason
    assert "fr" in exc_info.value.reason


def test_default_engine_is_glossary_for_spanish() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,,,,")

    talks = parse_csv(text, tz="UTC")

    assert talks[0].engine == "glossary"


def test_default_engine_for_english_uses_default_engine_en_param() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Talk,Speaker,en,es,,,,")

    fast_talks = parse_csv(text, tz="UTC")
    glossary_talks = parse_csv(text, tz="UTC", default_engine_en="glossary")

    assert fast_talks[0].engine == "fast"
    assert glossary_talks[0].engine == "glossary"


def test_explicit_motor_overrides_the_default() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,fast,,,")

    talks = parse_csv(text, tz="UTC")

    assert talks[0].engine == "fast"


def test_invalid_motor_raises_agenda_error() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,turbo,,,")

    with pytest.raises(AgendaError) as exc_info:
        parse_csv(text, tz="UTC")

    assert exc_info.value.row == 2
    assert "motor" in exc_info.value.reason


def test_glosario_terms_without_translation_keep_in_english() -> None:
    text = _csv(
        'main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,,,,"Kubernetes;git"'
    )

    talks = parse_csv(text, tz="UTC")

    assert [t.term for t in talks[0].glossary] == ["Kubernetes", "git"]
    assert all(t.keep_in_english for t in talks[0].glossary)
    assert all(t.translation is None for t in talks[0].glossary)


def test_glosario_terms_with_translation() -> None:
    text = _csv(
        'main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,,,,'
        '"control plane=plano de control;pod"'
    )

    talks = parse_csv(text, tz="UTC")

    by_term = {t.term: t for t in talks[0].glossary}
    assert by_term["control plane"].translation == "plano de control"
    assert by_term["control plane"].keep_in_english is False
    assert by_term["pod"].translation is None
    assert by_term["pod"].keep_in_english is True


def test_talk_id_is_stable_across_reparsing() -> None:
    text = _csv("main,2026-09-24 09:55,2026-09-24 10:10,Charla,Speaker,es,en,,,,")

    first = parse_csv(text, tz="UTC")[0].id
    second = parse_csv(text, tz="UTC")[0].id

    assert first == second


def test_talk_id_differs_for_different_rows() -> None:
    text = _csv(
        "main,2026-09-24 09:55,2026-09-24 10:10,Charla A,Speaker,es,en,,,,",
        "main,2026-09-24 11:00,2026-09-24 11:30,Charla B,Speaker,es,en,,,,",
    )

    talks = parse_csv(text, tz="UTC")

    assert talks[0].id != talks[1].id


def test_missing_required_column_raises_agenda_error() -> None:
    text = "sala,inicio,fin,titulo\nmain,2026-09-24 09:55,2026-09-24 10:10,Charla\n"

    with pytest.raises(AgendaError):
        parse_csv(text, tz="UTC")


def test_multiple_rows_parsed_in_order() -> None:
    text = _csv(
        "main,2026-09-24 09:55,2026-09-24 10:10,Charla A,Speaker,es,en,,,,",
        "track-2,2026-09-24 11:00,2026-09-24 11:30,Talk B,Speaker,en,es,,,,",
    )

    talks = parse_csv(text, tz="UTC")

    assert [t.title for t in talks] == ["Charla A", "Talk B"]
    assert [t.room_id for t in talks] == ["main", "track-2"]


def test_agenda_example_csv_parses_with_5_realistic_talks_in_two_rooms() -> None:
    text = EXAMPLE_CSV_PATH.read_text(encoding="utf-8")

    talks = parse_csv(text, tz="America/Argentina/Buenos_Aires")

    assert 4 <= len(talks) <= 6
    assert {t.room_id for t in talks} == {"main", "track-2"}
    assert {t.language for t in talks} == {"es", "en"}
    assert all(t.speakers for t in talks)
    assert all(t.title for t in talks)
