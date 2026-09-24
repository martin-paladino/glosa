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
skipped rather than erroring out the whole import. The same applies to
sessions whose ``language`` isn't "English" or "Spanish" (lunch breaks and
other non-talk entries use language=null).

Skips are not silent: use ``parse_nerdearla_report`` to get the list of
SkippedSession (source id, title, reason) alongside the talks -- e.g. to show
an operator "N imported, M skipped" after an admin import. ``parse_nerdearla``
itself is a thin wrapper that logs one summary warning when anything was
skipped and returns just the talks, for callers that don't need the detail.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glosa.agenda import AgendaError, stable_talk_id
from glosa.models import Talk

logger = logging.getLogger(__name__)

_LANGUAGE_MAP = {"english": "en", "spanish": "es"}
_SUPPORTED_LANGUAGES = ("es", "en")


@dataclass
class SkippedSession:
    """A Nerdearla session parse_nerdearla_report chose not to import.

    source_id: the session's own ``id``, when it had one.
    title: the session's title, for a human-readable summary.
    reason: e.g. "unmapped room: 'workshops-in-person-1'" or
        "unsupported language: 'Portuguese'" / "unsupported language: None".
    """

    source_id: str | None
    title: str
    reason: str


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


def parse_nerdearla_report(
    json: dict[str, Any] | list[dict[str, Any]],
    room_map: dict[str, str],
    tz: str,
    default_engine_en: Literal["fast", "glossary"] = "fast",
) -> tuple[list[Talk], list[SkippedSession]]:
    """Parse Nerdearla sessions JSON into Talks, and report what was skipped.

    A session whose room isn't in room_map, or whose language isn't
    "English"/"Spanish", is not an error: Nerdearla's public feed lists every
    room/workshop at the whole conference (most of which a given Glosa
    deployment isn't captioning) and includes non-talk entries such as lunch
    breaks (language: null). Such sessions are reported in the second return
    value instead of being dropped silently, so a caller (the admin import
    endpoint) can show the operator what didn't come in and why -- a stale
    room_map silently dropping talks is otherwise easy to miss.

    Raises AgendaError(row, reason) for a session that has a mapped room and
    a supported language but is otherwise malformed (e.g. no title). row is
    the session's 1-based position in the sessions list.
    """
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise AgendaError(0, f"unknown timezone: {tz!r}") from exc

    sessions = json.get("sessions", []) if isinstance(json, dict) else json

    talks: list[Talk] = []
    skipped: list[SkippedSession] = []
    for row_num, session in enumerate(sessions, start=1):
        source_id = session.get("id")
        title = (session.get("title") or "").strip()

        room_slug = session.get("room") or session.get("location_name") or ""
        room_id = room_map.get(room_slug)
        if room_id is None:
            skipped.append(
                SkippedSession(source_id=source_id, title=title, reason=f"unmapped room: {room_slug!r}")
            )
            continue

        language_raw = session.get("language")
        language = _LANGUAGE_MAP.get((language_raw or "").strip().lower())
        if language is None:
            skipped.append(
                SkippedSession(
                    source_id=source_id,
                    title=title,
                    reason=f"unsupported language: {language_raw!r}",
                )
            )
            continue

        if not title:
            raise AgendaError(row_num, "missing title")

        start = _parse_dt(session.get("start"), zone, row_num, "start")
        end = _parse_dt(session.get("end"), zone, row_num, "end")

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

    return talks, skipped


def parse_nerdearla(
    json: dict[str, Any] | list[dict[str, Any]],
    room_map: dict[str, str],
    tz: str,
    default_engine_en: Literal["fast", "glossary"] = "fast",
) -> list[Talk]:
    """Parse Nerdearla sessions JSON into a list of Talk.

    A thin wrapper around parse_nerdearla_report() for callers that just want
    the talks: if any sessions were skipped (unmapped room, unsupported
    language), logs one warning summarizing how many and why, then discards
    the report. Use parse_nerdearla_report() directly to see exactly what was
    skipped and why (the admin import endpoint does, to show the operator).
    """
    talks, skipped = parse_nerdearla_report(json, room_map, tz, default_engine_en)
    if skipped:
        reasons = Counter(s.reason.split(":", 1)[0] for s in skipped)
        detail = ", ".join(f"{count} {reason}" for reason, count in reasons.most_common())
        logger.warning(
            "parse_nerdearla: skipped %d of %d session(s): %s",
            len(skipped),
            len(talks) + len(skipped),
            detail,
        )
    return talks
