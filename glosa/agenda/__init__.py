"""Agenda importers: turn a CSV export or Nerdearla's public sessions JSON
into glosa.models.Talk objects that drive the autopilot (glosa/scheduler.py).

This package holds only the pure parsing logic (glosa/agenda/csv_import.py,
glosa/agenda/nerdearla_import.py). The admin endpoints and edit UI that
consume these parsers are built later, once the DB and web app exist.
"""

from __future__ import annotations

import hashlib
from datetime import datetime


class AgendaError(Exception):
    """Raised when a single agenda row/session can't be parsed.

    row: 1-based position of the offending entry as a human would see it
         (for CSV: counting the header row, so the first data row is 2; for
         Nerdearla JSON: the session's position in the sessions list).
    reason: short, human-readable explanation of what was wrong.
    """

    def __init__(self, row: int, reason: str) -> None:
        self.row = row
        self.reason = reason
        super().__init__(f"row {row}: {reason}")


def stable_talk_id(room_id: str, start: datetime, title: str) -> str:
    """A stable id for a talk that has no source id: a hash of room + start +
    title, so re-importing the same agenda (CSV row order can change) keeps
    the same talk ids.
    """
    digest = hashlib.sha1(f"{room_id}|{start.isoformat()}|{title}".encode("utf-8"))
    return digest.hexdigest()[:16]
