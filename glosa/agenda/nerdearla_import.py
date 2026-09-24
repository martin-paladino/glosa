"""Nerdearla agenda importer: parse_nerdearla(json, room_map, tz) -> list[Talk].

``json`` is the object returned by
GET https://backstage.nerdearla.com/api/sessions/?event_id=<uuid>
(a dict with a top-level "sessions" list); a bare list of sessions is also
accepted, so callers that already unwrapped the response work too.

``room_map`` maps Nerdearla's own room slug (the session's ``room``, or
``location_name`` when ``room`` is absent, e.g. "gran-sala") to one of
Glosa's own room ids (glosa/config.py RoomCfg.id, e.g. "main"). Nerdearla's
public feed lists every room and workshop track at the conference, most of
which we are not captioning, so sessions whose room isn't in room_map are
silently skipped rather than erroring out the whole import. The same applies
to sessions whose ``language`` isn't "English" or "Spanish" (lunch breaks and
other non-talk entries use language=null).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glosa.agenda import AgendaError, stable_talk_id
from glosa.models import Talk

_LANGUAGE_MAP = {"english": "en", "spanish": "es"}
_SUPPORTED_LANGUAGES = ("es", "en")


def _speaker_name(speaker: dict[str, Any]) -> str:
    name = (speaker.get("name") or "").strip()
    if name:
        return name
    parts = [(speaker.get("first_name") or "").strip(), (speaker.get("last_name") or "").strip()]
    return " ".join(p for p in parts if p)


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return [str(t).strip() for t in raw if str(t).strip()]


def _parse_dt(value: Any, zone: ZoneInfo, row: int, field: str) -> datetime:
    raw = str(value or "").strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AgendaError(row, f"invalid {field}: {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt


def parse_nerdearla(
    json: dict[str, Any] | list[dict[str, Any]],
    room_map: dict[str, str],
    tz: str,
    default_engine_en: Literal["fast", "glossary"] = "fast",
) -> list[Talk]:
    """Parse Nerdearla sessions JSON into a list of Talk.

    Raises AgendaError(row, reason) for the first session that has a mapped
    room and a supported language but is otherwise malformed (e.g. no
    title). ``row`` is the session's 1-based position in the sessions list.
    """
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise AgendaError(0, f"unknown timezone: {tz!r}") from exc

    sessions = json.get("sessions", []) if isinstance(json, dict) else json

    talks: list[Talk] = []
    for row_num, session in enumerate(sessions, start=1):
        room_slug = session.get("room") or session.get("location_name") or ""
        room_id = room_map.get(room_slug)
        if room_id is None:
            continue

        language = _LANGUAGE_MAP.get((session.get("language") or "").strip().lower())
        if language is None:
            continue

        title = (session.get("title") or "").strip()
        if not title:
            raise AgendaError(row_num, "missing title")

        start = _parse_dt(session.get("start"), zone, row_num, "start")
        end = _parse_dt(session.get("end"), zone, row_num, "end")

        source_id = session.get("id")
        talk_id = str(source_id) if source_id else stable_talk_id(room_id, start, title)

        speakers = [_speaker_name(sp) for sp in session.get("speakers") or []]
        speakers = [s for s in speakers if s]

        targets = [lang for lang in _SUPPORTED_LANGUAGES if lang != language]
        engine: Literal["fast", "glossary"] = "glossary" if language == "es" else default_engine_en

        talks.append(
            Talk(
                id=talk_id,
                room_id=room_id,
                title=title,
                speakers=speakers,
                language=language,
                targets=targets,
                engine=engine,
                start=start,
                end=end,
                abstract=(session.get("description") or "").strip(),
                tags=_parse_tags(session.get("tags")),
                glossary=[],
                status="scheduled",
                actual_start=None,
                actual_end=None,
            )
        )

    return talks
