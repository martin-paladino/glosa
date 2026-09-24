"""parse_nerdearla: load a Talk agenda from Nerdearla's public sessions API
(https://backstage.nerdearla.com/api/sessions/?event_id=...). The fixture
(tests/fixtures/nerdearla_sessions.json) holds 3 real sessions from that
endpoint, trimmed to the fields the importer uses: 2 Spanish (gran-sala,
auditorio) and 1 English (gran-sala).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from glosa.agenda import AgendaError
from glosa.agenda.nerdearla_import import parse_nerdearla, parse_nerdearla_report

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "nerdearla_sessions.json"
ROOM_MAP = {"gran-sala": "main", "auditorio": "track-2"}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_maps_all_three_fixture_sessions() -> None:
    talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert len(talks) == 3
    by_id = {t.id: t for t in talks}
    assert set(by_id) == {"1341066", "1250744", "1286278"}


def test_fields_are_mapped_for_a_spanish_session() -> None:
    talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")
    talk = next(t for t in talks if t.id == "1341066")

    assert talk.title == "Bienvenida a Nerdearla!"
    assert talk.room_id == "main"
    assert talk.language == "es"
    assert talk.speakers == ["Eduardo Casarero", "Ariel Jolo"]
    assert talk.tags == ["data-science-ai"]
    assert talk.abstract.startswith("Edu y Jolo dan inicio")
    assert talk.start.tzinfo is not None
    assert talk.start.hour == 9
    assert talk.start.minute == 55
    assert talk.end.hour == 10
    assert talk.end.minute == 10
    assert talk.status == "scheduled"
    assert talk.actual_start is None
    assert talk.actual_end is None


def test_fields_are_mapped_for_an_english_session() -> None:
    talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")
    talk = next(t for t in talks if t.id == "1286278")

    assert talk.title == "Local AI Ecosystem"
    assert talk.room_id == "main"
    assert talk.language == "en"
    assert talk.speakers == ["Merve Noyan"]
    assert talk.tags == ["data-science-ai"]


def test_default_engine_from_language() -> None:
    talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")
    by_id = {t.id: t for t in talks}

    # es -> glossary, regardless of default_engine_en.
    assert by_id["1341066"].engine == "glossary"
    assert by_id["1250744"].engine == "glossary"
    # en -> default_engine_en (default "fast").
    assert by_id["1286278"].engine == "fast"


def test_default_engine_en_param_is_honored() -> None:
    talks = parse_nerdearla(
        _fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires", default_engine_en="glossary"
    )
    talk = next(t for t in talks if t.id == "1286278")

    assert talk.engine == "glossary"


def test_room_not_in_room_map_is_skipped() -> None:
    data = _fixture()
    talks = parse_nerdearla(data, {"gran-sala": "main"}, tz="America/Argentina/Buenos_Aires")

    # The auditorio session (1250744) has no entry in room_map and is skipped.
    assert {t.id for t in talks} == {"1341066", "1286278"}


def test_sessions_with_unsupported_language_are_skipped() -> None:
    data = _fixture()
    data["sessions"].append(
        {
            "id": "9999999",
            "title": "Almuerzo / Lunch break",
            "description": "",
            "tags": "",
            "room": "gran-sala",
            "location_name": "Gran sala",
            "start": "2026-09-24 12:50",
            "end": "2026-09-24 13:30",
            "language": None,
            "speakers": [],
        }
    )

    talks = parse_nerdearla(data, ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert "9999999" not in {t.id for t in talks}
    assert len(talks) == 3


def test_targets_default_to_the_other_supported_language() -> None:
    talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")
    by_id = {t.id: t for t in talks}

    assert by_id["1341066"].targets == ["en"]  # es talk -> translate to en
    assert by_id["1286278"].targets == ["es"]  # en talk -> translate to es


def test_accepts_a_bare_list_of_sessions_too() -> None:
    sessions = _fixture()["sessions"]

    talks = parse_nerdearla(sessions, ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert len(talks) == 3


def test_missing_title_raises_agenda_error() -> None:
    sessions = [
        {
            "id": "1",
            "title": "",
            "description": "",
            "tags": "",
            "room": "gran-sala",
            "location_name": "Gran sala",
            "start": "2026-09-24 09:00",
            "end": "2026-09-24 09:30",
            "language": "Spanish",
            "speakers": [],
        }
    ]

    with pytest.raises(AgendaError) as exc_info:
        parse_nerdearla(sessions, ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert exc_info.value.row == 1
    assert "title" in exc_info.value.reason


def test_report_lists_a_skipped_session_for_an_unmapped_room() -> None:
    data = _fixture()  # auditorio session (1250744) has no entry below

    talks, skipped = parse_nerdearla_report(data, {"gran-sala": "main"}, tz="America/Argentina/Buenos_Aires")

    assert {t.id for t in talks} == {"1341066", "1286278"}
    assert len(skipped) == 1
    entry = skipped[0]
    assert entry.source_id == "1250744"
    assert entry.title == "Cuando mi Agente Perdió la Paciencia: Seguridad en IA"
    assert "unmapped room" in entry.reason
    assert "auditorio" in entry.reason


def test_report_lists_a_skipped_session_for_an_unsupported_language() -> None:
    data = _fixture()
    data["sessions"].append(
        {
            "id": "9999999",
            "title": "Almuerzo / Lunch break",
            "description": "",
            "tags": "",
            "room": "gran-sala",
            "location_name": "Gran sala",
            "start": "2026-09-24 12:50",
            "end": "2026-09-24 13:30",
            "language": None,
            "speakers": [],
        }
    )

    talks, skipped = parse_nerdearla_report(data, ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert len(talks) == 3
    assert len(skipped) == 1
    entry = skipped[0]
    assert entry.source_id == "9999999"
    assert entry.title == "Almuerzo / Lunch break"
    assert "unsupported language" in entry.reason
    assert "None" in entry.reason


def test_parse_nerdearla_logs_one_warning_when_sessions_are_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = _fixture()  # auditorio session is unmapped below -> 1 skip

    with caplog.at_level(logging.WARNING, logger="glosa.agenda.nerdearla_import"):
        talks = parse_nerdearla(data, {"gran-sala": "main"}, tz="America/Argentina/Buenos_Aires")

    assert {t.id for t in talks} == {"1341066", "1286278"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "skipped 1 of 3" in warnings[0].getMessage()
    assert "unmapped room" in warnings[0].getMessage()


def test_parse_nerdearla_logs_nothing_when_nothing_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="glosa.agenda.nerdearla_import"):
        talks = parse_nerdearla(_fixture(), ROOM_MAP, tz="America/Argentina/Buenos_Aires")

    assert len(talks) == 3
    assert caplog.records == []
