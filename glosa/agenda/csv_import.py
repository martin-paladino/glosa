"""CSV agenda importer: parse_csv(text, tz) -> list[Talk].

Expected columns (see agenda.example.csv), in any order:
    sala, inicio, fin, titulo, speakers, idioma, destinos, motor, abstract,
    tags, glosario

- speakers, destinos and tags are ``;``-separated lists.
- idioma must be "es" or "en" (the only source languages Glosa's engines
  support); anything else is a row error.
- motor is "fast" or "glossary". When blank, it defaults from idioma:
  es -> "glossary" (verbatim transcription, for the glossary to apply), en ->
  default_engine_en (translate "fast" by default).
- glosario is a ``;``-separated list of terms. A bare term
  ("Kubernetes") is kept untranslated (keep_in_english=True); a term with a
  translation ("control plane=plano de control") is not
  (keep_in_english=False, translation set).
- inicio/fin are ISO-ish local timestamps ("YYYY-MM-DD HH:MM"); when they
  carry no offset they're localized to tz (an IANA zone name, e.g.
  "America/Argentina/Buenos_Aires").
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glosa.agenda import AgendaError, stable_talk_id
from glosa.models import GlossaryTerm, Talk

REQUIRED_COLUMNS = (
    "sala",
    "inicio",
    "fin",
    "titulo",
    "speakers",
    "idioma",
    "destinos",
)
VALID_LANGUAGES = {"es", "en"}
VALID_ENGINES = {"fast", "glossary"}


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(";") if v.strip()]


def _parse_glossary(value: str | None) -> list[GlossaryTerm]:
    terms: list[GlossaryTerm] = []
    for raw in _split(value):
        if "=" in raw:
            term, translation = raw.split("=", 1)
            terms.append(
                GlossaryTerm(term=term.strip(), keep_in_english=False, translation=translation.strip())
            )
        else:
            terms.append(GlossaryTerm(term=raw.strip(), keep_in_english=True))
    return terms


def _parse_dt(value: str | None, zone: ZoneInfo, row: int, field: str) -> datetime:
    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AgendaError(row, f"invalid {field}: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt


def parse_csv(
    text: str,
    tz: str,
    default_engine_en: Literal["fast", "glossary"] = "fast",
) -> list[Talk]:
    """Parse a CSV agenda export into a list of Talk, in row order.

    Raises AgendaError(row, reason) for the first row that can't be parsed.
    row is 1-based counting the header, so the first data row is row 2
    (matching what a spreadsheet editor shows).
    """
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise AgendaError(0, f"unknown timezone: {tz!r}") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise AgendaError(1, f"missing required columns: {', '.join(missing)}")

    talks: list[Talk] = []
    for row_num, row in enumerate(reader, start=2):
        room_id = (row.get("sala") or "").strip()
        title = (row.get("titulo") or "").strip()
        if not room_id:
            raise AgendaError(row_num, "missing sala")
        if not title:
            raise AgendaError(row_num, "missing titulo")

        start = _parse_dt(row.get("inicio"), zone, row_num, "inicio")
        end = _parse_dt(row.get("fin"), zone, row_num, "fin")

        language = (row.get("idioma") or "").strip().lower()
        if language not in VALID_LANGUAGES:
            raise AgendaError(row_num, f"invalid idioma: {language!r} (expected es or en)")

        motor_raw = (row.get("motor") or "").strip().lower()
        if motor_raw:
            if motor_raw not in VALID_ENGINES:
                raise AgendaError(row_num, f"invalid motor: {motor_raw!r} (expected fast or glossary)")
            engine: Literal["fast", "glossary"] = motor_raw  # type: ignore[assignment]
        else:
            engine = "glossary" if language == "es" else default_engine_en

        talks.append(
            Talk(
                id=stable_talk_id(room_id, start, title),
                room_id=room_id,
                title=title,
                speakers=_split(row.get("speakers")),
                language=language,
                targets=_split(row.get("destinos")),
                engine=engine,
                start=start,
                end=end,
                abstract=(row.get("abstract") or "").strip(),
                tags=_split(row.get("tags")),
                glossary=_parse_glossary(row.get("glosario")),
                status="scheduled",
                actual_start=None,
                actual_end=None,
            )
        )

    return talks
