"""The production panel's live feed (Task 12): ``GET /api/admin/stream``, SSE.

Frames, by ``event:`` name (``data`` is one line of JSON):

  - ``state``, every ``TICK_S`` (1 s): every room with its raw
    ``RoomWorker.status()`` (state, detail, level_db, latency_p50_s,
    quality, cost_usd, talk_id), its mode, current talk and next agenda
    talk, what is wrong with it (``issue``, ``classify``) and since when;
    the spend against the budget, the next automatic change and how many
    panels are open. The texts come localized to the panel's language
    (``?lang=es|en``, else Accept-Language): the same ``localize()`` renders
    the first paint of /admin (glosa/web/admin_api.py).
  - ``log``, with ``id:`` = the event's id in the ``events`` table: the log,
    oldest first. On connect, the last ``BACKLOG_EVENTS``, or, with
    ``Last-Event-ID`` (what EventSource sends when it reconnects), the ones
    after it; then each new one within a tick.
  - ``notice``: each ``app.state.admin_events`` event (talk edited, agenda
    imported, mode changed, talk started...) as it is published, so the
    panel refetches the agenda at once.
  - ``cc``: the last lines of a room's captions in its talk's language (the
    original), when they change, at most every ``CC_INTERVAL_S``. This
    stream holds one bus subscription per room, so the browser opens a
    single connection instead of one per room (HTTP/1.1 browsers allow six
    per host, and the panel's own requests need some).
  - ``bye``: the session ended (a logout anywhere, or it expired); the stream
    stops and the panel asks to log in again.

Auth: ``stream_router`` carries only ``require_admin``. An EventSource cannot
send the X-Glosa-Admin header every other /api/admin route requires, and this
GET changes nothing. The session is checked again every tick, so a logout
(a new session epoch) also closes the streams that were open.

``AdminMonitor`` (one per app, ``monitor_for(app)``) computes the snapshot at
most every ``SNAPSHOT_TTL_S`` however many panels are open, and keeps what
needs memory: since when each room is in its state and issue, and the silence
alarm (the level issue for more than ``SILENCE_ALARM_S`` with a talk on
becomes an issue of its own, and one ``silence`` warning in the log per
episode). It only runs while a panel is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from glosa.i18n import SUPPORTED, Lang, admin_plural, admin_t, detect_lang
from glosa.metrics import CostTracker
from glosa.models import CaptionMsg, RoomStatus, Talk
from glosa.room import is_free_talk, target_lang
from glosa.talk_check import TalkMismatchSuggestion
from glosa.web.auth import is_authenticated, require_admin

log = logging.getLogger(__name__)

TICK_S = 1.0  # state frames and new log events
CC_INTERVAL_S = 0.3  # caption frames, at most this often per stream
SNAPSHOT_TTL_S = 0.5  # one snapshot serves every open panel
BACKLOG_EVENTS = 100
EVENTS_PER_TICK = 200
RETRY_MS = 3000  # EventSource's reconnection delay

# RoomHealth's limits (glosa/metrics.py), for the panel's notes ("el tope es
# 5 s"); tests/web/test_admin_stream.py pins them to RoomHealth.evaluate.
LATENCY_LIMIT_S = 5.0
QUALITY_MIN = 0.5
LEVEL_MIN_DB = -50.0
# Spec §12: more than 30 s below the level threshold with a talk on.
SILENCE_ALARM_S = 30.0

# RoomStatus.state -> the four words of glosa.css (.led--*, .monitor--*...).
STATE_CSS = {"green": "live", "yellow": "degraded", "red": "down", "idle": "idle"}
STATE_ORDER = ("live", "degraded", "down", "idle")
# "info" (Task 18: Jev's talk_mismatch suggestion) is deliberately last: a
# real health problem always outranks a soft suggestion in the Atención list.
SEVERITY_ORDER = {"down": 0, "degraded": 1, "info": 2}

stream_router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


# ---- what is wrong with a room --------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """Why a room is not green, and the action the panel suggests:
    ``reconnect`` (a new engine session), ``restart`` (open the source
    again for the same talk: POST .../restart), ``open`` (look at it: the
    drawer) or None."""

    kind: str
    severity: str  # "down" | "degraded"
    action: str | None
    values: dict[str, Any] = field(default_factory=dict)


STATION_SUFFIX = " | station:"
_NUMBER = r"(-?\d+(?:\.\d+)?)"
_LEVEL_RE = re.compile(rf"level {_NUMBER}\s*dB")
_LATENCY_RE = re.compile(rf"latency {_NUMBER}")
_QUALITY_RE = re.compile(rf"quality {_NUMBER}")


def classify(status: RoomStatus) -> Issue | None:
    """The issue behind a red or yellow RoomStatus, from its detail (the
    texts of RoomHealth.evaluate and RoomWorker.status)."""
    severity = STATE_CSS.get(status.state)
    if severity not in ("down", "degraded"):
        return None
    # An emitter room's detail ends with its station (RoomWorker._station_summary,
    # Task 14a); the panel reads the station from its own field instead.
    detail = (status.detail or "").split(STATION_SUFFIX, 1)[0]
    if detail.startswith("source is down: station"):
        return Issue("station", severity, "open")
    if detail.startswith("source is down"):
        _, _, error = detail.partition(": ")
        return Issue("source_down", severity, "restart", {"error": _first_line(error) or "ffmpeg"})
    if detail.startswith("engine halted"):
        return Issue("halted", severity, "reconnect")
    if detail.startswith("stalled"):
        return Issue("stalled", severity, "reconnect")
    if detail.startswith("payment blocked"):
        return Issue("payment", severity, None)
    if detail.startswith("latency"):
        return Issue("latency", severity, "reconnect", {"latency": _value(status.latency_p50_s, _LATENCY_RE, detail)})
    if detail.startswith("average quality"):
        return Issue("quality", severity, "open", {"quality": _value(status.quality, _QUALITY_RE, detail)})
    if detail.startswith("level"):
        return Issue("level", severity, "open", {"level": _value(None, _LEVEL_RE, detail)})
    if "recent reconnect" in detail:
        return Issue("reconnected", severity, "open")
    return Issue("other", severity, "reconnect" if severity == "down" else "open", {"detail": detail})


def _talk_mismatch_issue(state: Any, room_id: str, talk: Talk | None) -> Issue | None:
    """Task 18: Jev's "switch to manual?" suggestion (TalkMismatchStore),
    read only when the room has no real health issue -- a genuine problem
    always wins the room's one Atención slot. ``info`` severity: yellow,
    but never the room's LED (STATE_CSS/RoomStatus.state is untouched).
    Also guards against reading a suggestion that outlived its talk by one
    beat (TalkCheckScheduler clears it on the next tick, up to
    TALK_CHECK_EVERY_S later; this is instant)."""
    store = getattr(state, "talk_mismatches", None)
    if store is None or talk is None:
        return None
    suggestion: TalkMismatchSuggestion | None = store.get(room_id)
    if suggestion is None or suggestion.talk_id != talk.id:
        return None
    return Issue("talk_mismatch", "info", None, {"guess": suggestion.guess, "next_title": suggestion.next_title or ""})


_FFMPEG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")
_ERROR_CHARS = 120


def _first_line(error: str) -> str:
    """ffmpeg's error, as the Atención row and the monitor show it: its first
    line without the "[in#0 @ 0x...]" tags, at most _ERROR_CHARS long. The
    drawer shows the whole detail."""
    line = next((line.strip() for line in error.splitlines() if line.strip()), "")
    line = _FFMPEG_PREFIX.sub("", line)
    return line if len(line) <= _ERROR_CHARS else line[: _ERROR_CHARS - 1].rstrip() + "…"


def _value(known: float | None, pattern: re.Pattern[str], detail: str) -> float | None:
    if known is not None:
        return known
    match = pattern.search(detail)
    return float(match.group(1)) if match else None


# ---- captions --------------------------------------------------------------------


class CaptionTail:
    """The end of one caption track, as the monitors show it: the closed
    text (at most ``MAX_CHARS``) and the open phrase. Fed CaptionMsg in bus
    order: ``append``, ``set`` (the whole open segment so far; "" removes
    it), ``close``, and ``talk`` (a new talk starts over)."""

    MAX_SEGS = 16
    MAX_CHARS = 360

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.talk_id: str | None = None
        self.ts: float | None = None
        self._segs: dict[int, list[Any]] = {}  # seg -> [text, closed]

    def feed(self, msg: CaptionMsg) -> bool:
        """Apply one message; whether what the monitor shows changed."""
        kind = msg.type
        if kind == "talk":
            talk_id = (msg.data or {}).get("talk_id")
            if talk_id == self.talk_id:
                return False
            self.talk_id, self.ts = talk_id, None
            self._segs.clear()
            return True
        if msg.seg is None:
            return False
        if kind == "append":
            self._segs.setdefault(msg.seg, ["", False])[0] += msg.text or ""
        elif kind == "set":
            if msg.text:
                self._segs.setdefault(msg.seg, ["", False])[0] = msg.text
            else:
                self._segs.pop(msg.seg, None)
        elif kind == "close":
            entry = self._segs.get(msg.seg)
            if entry is None or entry[1]:
                return False
            entry[1] = True
        else:
            return False
        if kind != "close" and getattr(msg, "ts", None) is not None:
            self.ts = msg.ts
        while len(self._segs) > self.MAX_SEGS:
            self._segs.pop(next(iter(self._segs)))
        return True

    def state(self) -> dict[str, Any]:
        closed = " ".join(text.strip() for text, done in self._segs.values() if done and text.strip())
        open_ = " ".join(text.strip() for text, done in self._segs.values() if not done and text.strip())
        if len(closed) > self.MAX_CHARS:
            cut = closed[-self.MAX_CHARS:]
            space = cut.find(" ")
            closed = "…" + (cut[space + 1:] if 0 <= space < len(cut) - 1 else cut)
        return {"lang": self.lang, "talk_id": self.talk_id, "closed": closed, "open": open_, "ts": self.ts}


# ---- the snapshot ---------------------------------------------------------------------


def event_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _iso(value: datetime | None, zone: tzinfo) -> str | None:
    return value.astimezone(zone).isoformat(timespec="seconds") if value is not None else None


def talk_brief(talk: Talk, zone: tzinfo) -> dict[str, Any]:
    """A talk as the panel shows it (the full one is GET /api/admin/talks/{id})."""
    return {
        "id": talk.id,
        "title": talk.title,
        "speakers": list(talk.speakers),
        "language": talk.language,
        "target": target_lang(talk.language, talk.targets),
        "targets": list(talk.targets),
        "engine": talk.engine,
        "glossary": len(talk.glossary),
        "start": _iso(talk.start, zone),
        "end": _iso(talk.end, zone),
        "actual_start": _iso(talk.actual_start, zone),
        "status": talk.status,
        "free": is_free_talk(talk.id),
    }


class AdminMonitor:
    def __init__(self, app: Any, *, silence_alarm_s: float = SILENCE_ALARM_S) -> None:
        self._state = app.state
        self.silence_alarm_s = silence_alarm_s
        self.panels = 0  # admin streams open now
        self.titles: dict[str, str] = {}  # talk id -> title, for the log
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._since: dict[str, tuple[Any, str]] = {}
        self._quiet: dict[str, float] = {}
        self._alarmed: set[str] = set()

    async def snapshot(self) -> dict[str, Any]:
        """The rooms, the budget and the next automatic change, language
        neutral (``localize`` adds the texts)."""
        async with self._lock:
            now = self._state.clock.now()
            if self._cache is None or now - self._cache_at >= SNAPSHOT_TTL_S:
                self._cache = await self._compute(now)
                self._cache_at = now
            return self._cache

    async def _compute(self, now: float) -> dict[str, Any]:
        state = self._state
        settings = state.settings
        zone = event_zone(settings.timezone)
        wall = state.clock.wall().astimezone(zone)
        try:
            costs = await state.db.cost_by_room()
        except Exception:
            log.exception("admin: could not read the costs")
            costs = {}
        rooms: list[dict[str, Any]] = []
        payment = False
        changes: list[tuple[datetime, int, str, dict[str, Any], Talk]] = []
        for index, worker in enumerate(list(state.workers.values()), start=1):
            room_id = worker.room.id
            status: RoomStatus = worker.status()
            issue = classify(status)
            payment |= issue is not None and issue.kind == "payment"
            issue = await self._silence(room_id, issue, now)
            css = STATE_CSS.get(status.state, "idle")
            talk: Talk | None = worker.talk
            if issue is None:
                issue = _talk_mismatch_issue(state, room_id, talk)
            try:
                nxt = await state.autopilot.next_talk(room_id)
            except Exception:
                log.exception("admin: room %s: could not read its next talk", room_id)
                nxt = None
            mode = state.autopilot.mode(room_id)
            for known in (talk, nxt):
                if known is not None:
                    self.titles[known.id] = known.title
            room = {
                "id": room_id,
                "name": worker.room.name,
                "slug": worker.room.slug,
                "key": index if index <= 9 else None,
                "mode": mode,
                "has_source": bool(worker.has_source),
                "state": css,
                "status": asdict(status),
                "issue": asdict(issue) if issue is not None else None,
                "since": self._track(room_id, (css, _age_key(issue)), wall),
                "station": _station(state, worker),
                "talk": talk_brief(talk, zone) if talk is not None else None,
                "next": talk_brief(nxt, zone) if nxt is not None else None,
                "lang": talk.language if talk is not None else worker.language,
                "cost_usd": costs.get(room_id, 0.0),
            }
            rooms.append(room)
            if mode == "auto":
                if nxt is not None and nxt.start > wall and nxt.start.astimezone(zone).date() == wall.date():
                    changes.append((nxt.start, 0, "open", room, nxt))
                if (
                    talk is not None
                    and not is_free_talk(talk.id)
                    and wall < talk.end
                    and talk.end.astimezone(zone).date() == wall.date()
                ):
                    changes.append((talk.end, 1, "close", room, talk))
        spent = sum(costs.values())
        tracker = CostTracker(settings.prices, settings.budget_usd)
        tracker.add("all", "all", spent)
        alert = "exhausted" if payment else tracker.alert()
        budget = {
            "spent": spent,
            "budget": settings.budget_usd,
            "ratio": min(tracker.ratio(), 99.0),
            "alert": alert,
            "since": self._track("#budget", alert, wall) if alert else None,
        }
        next_change = None
        if changes:
            at, _, kind, room, talk = min(changes, key=lambda c: (c[0], c[1]))
            next_change = {"at": _iso(at, zone), "kind": kind, "room_id": room["id"], "room": room["name"],
                           "talk_id": talk.id, "title": talk.title}
        return {
            "now": wall.isoformat(timespec="seconds"),
            "tz": settings.timezone,
            "rooms": rooms,
            "budget": budget,
            "next_change": next_change,
            "panels": self.panels,
        }

    def _track(self, key: str, value: Any, wall: datetime) -> str:
        """Since when ``key`` has had ``value`` (as first seen by the panel)."""
        known = self._since.get(key)
        if known is None or known[0] != value:
            known = self._since[key] = (value, wall.isoformat(timespec="seconds"))
        return known[1]

    async def _silence(self, room_id: str, issue: Issue | None, now: float) -> Issue | None:
        if issue is None or issue.kind != "level":
            self._quiet.pop(room_id, None)
            self._alarmed.discard(room_id)
            return issue
        started = self._quiet.setdefault(room_id, now)
        if now - started < self.silence_alarm_s:
            return issue
        seconds = int(self.silence_alarm_s)
        if room_id not in self._alarmed:
            self._alarmed.add(room_id)
            try:
                await self._state.db.log_event(room_id, "warning", "silence", f"no audio for {seconds} s")
            except Exception:
                log.exception("admin: room %s: could not log the silence alarm", room_id)
        return Issue("silence", issue.severity, "open", {"seconds": seconds, "level": issue.values.get("level")})


def _age_key(issue: Issue | None) -> str | None:
    """What a room's Atención age counts from: the silence alarm is the same
    episode as the low level that became it (its row keeps its age)."""
    if issue is None:
        return None
    return "level" if issue.kind == "silence" else issue.kind


def _station(state: Any, worker: Any) -> dict[str, Any] | None:
    """An emitter room's station (Task 14a, glosa/web/station.py), admin-only
    like the rest of this stream: connected or not, device, last level and
    seconds since its last audio. None for every other source type."""
    hub = getattr(state, "station_hub", None)
    if hub is None or worker.room.source_type != "emitter":
        return None
    info = hub.info(worker.room.id)
    return {
        "connected": info.connected,
        "device": info.device,
        "level_db": info.level_db,
        "last_audio_age_s": info.last_audio_age_s,
    }


def monitor_for(app: Any) -> AdminMonitor:
    monitor = getattr(app.state, "admin_monitor", None)
    if monitor is None:
        monitor = app.state.admin_monitor = AdminMonitor(app)
    return cast(AdminMonitor, monitor)


# ---- texts ------------------------------------------------------------------------------


def fmt_num(value: float, digits: int, lang: Lang) -> str:
    text = f"{value:,.{digits}f}"
    if lang == "es":
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def fmt_limit(value: float, lang: Lang) -> str:
    """5 -> "5", 0.5 -> "0,5", -50 -> "−50"."""
    text = fmt_num(abs(value), 0 if float(value).is_integer() else 1, lang)
    return ("−" if value < 0 else "") + text


def fmt_db(value: float, lang: Lang) -> str:
    return ("−" if value < -0.5 else "") + fmt_num(abs(value), 0, lang)


def _hhmm(iso: str | None, zone: tzinfo) -> str:
    return datetime.fromisoformat(iso).astimezone(zone).strftime("%H:%M") if iso else ""


def issue_texts(issue: dict[str, Any], lang: Lang) -> dict[str, str]:
    values = issue.get("values") or {}
    kind = issue["kind"]
    # Task 18: talk_mismatch has two message variants (which agenda talk
    # Jev's last check guessed), not one -- issue_{key}_{part} picks the
    # right one; every other kind's key is just its own kind.
    key = f"talk_mismatch_{values.get('guess', 'break')}" if kind == "talk_mismatch" else kind
    limit = {"latency": LATENCY_LIMIT_S, "quality": QUALITY_MIN, "level": LEVEL_MIN_DB}.get(kind)
    fill = {
        "latency": fmt_num(values["latency"], 1, lang) if values.get("latency") is not None else "?",
        "quality": fmt_num(values["quality"], 2, lang) if values.get("quality") is not None else "?",
        "level": fmt_db(values["level"], lang) if values.get("level") is not None else "?",
        "limit": fmt_limit(limit, lang) if limit is not None else "",
        "seconds": values.get("seconds", int(SILENCE_ALARM_S)),
        "error": str(values.get("error", "")).rstrip("."),
        "detail": values.get("detail", ""),
        "next_title": values.get("next_title", ""),
    }
    texts = {part: admin_t(f"issue_{key}_{part}", lang).format(**fill) for part in ("what", "em", "title", "note", "hint")}
    action = issue.get("action")
    texts["action_label"] = admin_t(f"action_{action}", lang) if action else ""
    return texts


def localize(snap: dict[str, Any], lang: Lang) -> dict[str, Any]:
    """``snap`` (AdminMonitor.snapshot) with the texts of the panel."""
    zone = event_zone(snap.get("tz") or "UTC")
    today = datetime.fromisoformat(snap["now"]).date()
    rooms = [_room_texts(room, lang, zone, today) for room in snap["rooms"]]
    attention = [
        {
            "key": room["id"], "room_id": room["id"], "room": room["name"], "severity": room["issue"]["severity"],
            "what": room["text"]["what"], "em": room["text"]["em"], "since": room["since"],
            "action": room["issue"]["action"], "action_label": room["text"]["action_label"],
        }
        for room in rooms
        if room["issue"] is not None
    ]
    budget = dict(snap["budget"])
    money = {
        "spent": fmt_num(budget["spent"], 2, lang),
        "budget": fmt_num(budget["budget"], 2, lang),
        "warn": fmt_num(budget["budget"] * 0.8, 2, lang),
        "pct": fmt_num(budget["ratio"] * 100, 0, lang),
    }
    budget["text"] = {
        "value": f"US$ {money['spent']}",
        "of": admin_t("budget_of", lang).format(**money),
        "valuetext": admin_t("budget_valuetext", lang).format(**money),
        "warns_at": admin_t("budget_warns_at", lang).format(**money),
    }
    if budget["alert"]:
        over = budget["alert"] == "exhausted"
        attention.append({
            "key": "budget", "room_id": None, "room": admin_t("budget_room", lang),
            "severity": "down" if over else "degraded",
            "what": admin_t("budget_over" if over else "budget_80", lang).format(**money),
            "em": admin_t("budget_over_em" if over else "budget_80_em", lang),
            "since": budget["since"], "action": None, "action_label": "",
        })
    order = {room["id"]: n for n, room in enumerate(rooms)}
    attention.sort(key=lambda row: (SEVERITY_ORDER[row["severity"]], order.get(row["room_id"], len(order))))
    counts = {state: sum(room["state"] == state for room in rooms) for state in STATE_ORDER}
    # "N en vivo" counts every room with a talk running, degraded or down ones
    # included; those also get their own item ("1 degradada") on top.
    counts["live"] = sum(room["state"] != "idle" for room in rooms)
    change = snap.get("next_change")
    if change is not None:
        key = "change_open" if change["kind"] == "open" else "change_close"
        change = change | {"text": admin_t(key, lang).format(time=_hhmm(change["at"], zone), room=change["room"])}
    return snap | {
        "rooms": rooms,
        "attention": attention,
        "pending": admin_plural("pending", len(attention), lang) if attention else admin_t("nothing_pending", lang),
        "all_clear": admin_plural("all_clear", len(rooms), lang) if rooms else admin_t("no_rooms", lang),
        "tally": [
            {"state": state, "count": counts[state], "word": admin_plural(f"tally_{state}", counts[state], lang)}
            for state in STATE_ORDER
            if counts[state]
        ],
        "budget": budget,
        "next_change": change,
        "next_change_text": change["text"] if change else admin_t("no_change", lang),
        "panels_text": admin_plural("panels", snap["panels"], lang),
    }


def _room_texts(room: dict[str, Any], lang: Lang, zone: tzinfo, today: Any) -> dict[str, Any]:
    issue = room["issue"]
    nxt = room["next"]
    texts: dict[str, str] = {
        "what": "", "em": "", "title": "", "note": "", "hint": "", "action_label": "", "state_word": "", "wait": "",
    }
    if issue is not None:
        texts |= issue_texts(issue, lang)
        texts["state_word"] = admin_t(f"state_{room['state']}", lang)
        said = texts["state_word"].lower() in texts["what"].lower()  # "Fuente caída: ..." already says it
        texts["state_line"] = texts["what"] if said else f"{texts['state_word']}. {texts['what']}"
    elif room["state"] == "idle":
        next_today = nxt is not None and datetime.fromisoformat(nxt["start"]).astimezone(zone).date() == today
        if next_today:
            key = "opens_at" if room["mode"] == "auto" else "next_at"
            texts["state_word"] = admin_t(key, lang).format(time=_hhmm(nxt["start"], zone))
            texts["wait"] = admin_t("pilot_opens" if room["mode"] == "auto" else "manual_opens", lang)
        else:
            texts["state_word"] = admin_t("no_talk", lang)
            texts["wait"] = admin_t("no_more_today", lang)
        texts["state_line"] = admin_t("idle_ok", lang)
    else:
        texts["state_line"] = admin_t("live_ok", lang)
    talk = room["talk"]
    if talk is not None and talk["free"]:
        talk = talk | {"title": admin_t("free_session", lang)}
    return room | {"text": texts, "talk": talk}


# ---- the event log ------------------------------------------------------------------------

_TALK_START = re.compile(r"^(?P<id>[^:]+): (?P<title>.*) \((?P<src>[a-z]{2,3}) -> (?P<dst>[a-z]{2,3})\)$", re.S)
_OPENED = re.compile(r"^opened (?P<id>\S+) \((?P<by>\w+)\): (?P<title>.*)$", re.S)
_NUMBERED = re.compile(r"#(?P<n>\d+)")
_SOURCE_RESTART = re.compile(r"^ffmpeg restart #(?P<n>\d+): (?P<error>.*)$", re.S)
_GO_AWAY = re.compile(r"^session \d+: (?P<s>\d+) s left$")
_ENGINE_ERROR = re.compile(r"^session \d+: error (?P<code>-?\d+): (?P<text>.*)$", re.S)
_IMPORT = re.compile(r"^(?P<format>\w+): (?P<imported>\d+) imported, (?P<skipped>\d+) skipped, (?P<removed>\d+) removed$")
_ID_PREFIX = re.compile(r"^(?P<id>[^:\s]+): (?P<rest>.*)$", re.S)
_SECONDS = re.compile(r"(\d+) s")
_TITLED = ("talk_end", "talk_updated", "resumed", "stale_live", "restart")


def referenced_talk(ev: Any) -> str | None:
    """The talk id an event names, when its message has only the id."""
    if ev.type == "talk_end":
        return ev.message.strip() or None
    if ev.type in _TITLED:
        match = _ID_PREFIX.match(ev.message)
        return match["id"] if match else None
    return None


def describe_event(ev: Any, lang: Lang, titles: dict[str, str]) -> str:
    """One log line in the panel's language; the raw message when its shape
    is unknown (so nothing is ever hidden)."""
    msg = ev.message or ""

    def title(talk_id: str) -> str:
        if is_free_talk(talk_id):
            return admin_t("free_session", lang)
        return titles.get(talk_id, talk_id)

    def say(key: str, **values: Any) -> str:
        return admin_t(key, lang).format(**values)

    kind = ev.type
    if kind == "talk_start" and (m := _TALK_START.match(msg)):
        return say("ev_talk_start", title=m["title"], src=m["src"].upper(), dst=m["dst"].upper())
    if kind == "talk_end" and msg.strip():
        return say("ev_talk_end", title=title(msg.strip()))
    if kind == "autopilot":
        if m := _OPENED.match(msg):
            return say("ev_opened_operator" if m["by"] == "operator" else "ev_opened_autopilot", title=m["title"])
        if msg.startswith("idle:"):
            return say("ev_autopilot_idle")
    if kind == "autopilot_error":
        return say("ev_autopilot_error", error=_first_line(msg.removeprefix("could not open ")))
    if kind == "mode" and msg.endswith(("auto", "manual")):
        return say("ev_mode_auto" if msg.endswith("auto") else "ev_mode_manual")
    if kind == "source_down" and msg.startswith("station"):
        return say("ev_station_down")
    if kind == "source_down":
        return say("ev_source_down", error=_first_line(msg))
    if kind == "source_recovered" and msg.startswith("station"):
        return say("ev_station_back")
    if kind == "source_restart" and (m := _SOURCE_RESTART.match(msg)):
        return say("ev_source_restart", n=m["n"], error=_first_line(m["error"]))
    if kind in ("reconnect", "rotation") and (m := _NUMBERED.search(msg)):
        return say(f"ev_{kind}", n=m["n"])
    if kind == "go_away" and (m := _GO_AWAY.match(msg)):
        return say("ev_go_away", s=m["s"])
    if kind == "engine_error" and (m := _ENGINE_ERROR.match(msg)):
        return say("ev_engine_error", code=m["code"], text=_first_line(m["text"]))
    if kind == "agenda_import" and (m := _IMPORT.match(msg)):
        name = "CSV" if m["format"] == "csv" else m["format"].capitalize()
        return say("ev_agenda_import", format=name, imported=m["imported"], skipped=m["skipped"], removed=m["removed"])
    if kind in (*_TITLED, "talk_deleted") and (m := _ID_PREFIX.match(msg)):
        if kind == "talk_deleted":
            return say("ev_talk_deleted", title=m["rest"])
        if kind == "talk_updated":
            fields = ", ".join(_field(name.strip(), lang) for name in m["rest"].split(","))
            return say("ev_talk_updated", title=title(m["id"]), fields=fields)
        return say(f"ev_{kind}", title=title(m["id"]))
    if kind in ("start_failed", "resume_failed"):
        return say("ev_start_failed", error=_first_line(msg))
    if kind == "silence" and (m := _SECONDS.search(msg)):
        return say("ev_silence", seconds=m.group(1))
    if kind == "source_change":
        return say("ev_source_change", path=msg.removeprefix("playing file "))
    return msg


def _field(name: str, lang: Lang) -> str:
    try:
        return admin_t(f"field_{name}", lang)
    except KeyError:
        return name


# ---- the stream ------------------------------------------------------------------------------


def sse_frame(event: str, data: Any, id: int | None = None) -> str:
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def panel_lang(request: Request) -> Lang:
    requested = request.query_params.get("lang")
    if requested in SUPPORTED:
        return cast(Lang, requested)
    return detect_lang(request.headers.get("accept-language"))


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


@stream_router.get("/stream")
async def admin_stream(request: Request) -> StreamingResponse:
    """The panel's live feed (see the module docstring)."""
    return StreamingResponse(
        _stream(request, panel_lang(request), _last_event_id(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class _Feed:
    """One room's captions inside one stream: a bus subscription feeding a
    CaptionTail."""

    def __init__(self, bus: Any, room_id: str, lang: str, changed: Any) -> None:
        self.tail = CaptionTail(lang)
        self.task = asyncio.create_task(self._run(bus, room_id, lang, changed), name=f"admin-cc-{room_id}")

    async def _run(self, bus: Any, room_id: str, lang: str, changed: Any) -> None:
        async with contextlib.aclosing(bus.subscribe(room_id, lang, None)) as messages:
            async for msg in messages:
                if self.tail.feed(msg):
                    changed(room_id)


async def _stream(request: Request, lang: Lang, last_id: int | None) -> AsyncIterator[str]:
    state = request.app.state
    db = state.db
    monitor = monitor_for(request.app)
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    notices: list[Any] = []
    feeds: dict[str, _Feed] = {}
    dirty: set[str] = set()
    subscription = state.admin_events.subscribe()

    def changed(room_id: str) -> None:
        dirty.add(room_id)
        wake.set()

    async def forward() -> None:
        async for event in subscription:
            notices.append(event)
            wake.set()

    forwarder = asyncio.create_task(forward(), name="admin-notices")
    monitor.panels += 1
    try:
        yield f"retry: {RETRY_MS}\n\n"
        snap = await monitor.snapshot()
        yield sse_frame("state", localize(snap, lang))
        _sync_feeds(feeds, snap, state.bus, changed)
        cursor, backlog = await _backlog(db, last_id)
        frames, cursor = await _log_frames(backlog, lang, monitor, state, cursor)
        for frame in frames:
            yield frame
        next_tick = loop.time() + TICK_S
        next_cc = 0.0
        while True:
            wake.clear()
            while notices:
                yield sse_frame("notice", notices.pop(0).as_dict())
            now = loop.time()
            if now >= next_tick:
                if not is_authenticated(request):
                    yield sse_frame("bye", {"reason": "session"})
                    return
                snap = await monitor.snapshot()
                yield sse_frame("state", localize(snap, lang))
                _sync_feeds(feeds, snap, state.bus, changed)
                frames, cursor = await _log_frames(
                    await db.events_after(cursor, EVENTS_PER_TICK), lang, monitor, state, cursor
                )
                for frame in frames:
                    yield frame
                next_tick = max(next_tick + TICK_S, now + TICK_S / 2)
            if dirty and now >= next_cc:
                for room_id in sorted(dirty):
                    if room_id in feeds:
                        yield sse_frame("cc", {"room_id": room_id} | feeds[room_id].tail.state())
                dirty.clear()
                next_cc = now + CC_INTERVAL_S
            timeout = next_tick - loop.time()
            if dirty:
                timeout = min(timeout, next_cc - loop.time())
            if timeout > 0 and not notices:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout)
    finally:
        monitor.panels -= 1
        subscription.close()
        tasks = [forwarder, *(feed.task for feed in feeds.values())]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync_feeds(feeds: dict[str, _Feed], snap: dict[str, Any], bus: Any, changed: Any) -> None:
    """One caption feed per room, in the language of its talk (the original)."""
    wanted = {room["id"]: room["lang"] for room in snap["rooms"]}
    for room_id in list(feeds):
        feed = feeds[room_id]
        if wanted.get(room_id) != feed.tail.lang:
            feed.task.cancel()
            del feeds[room_id]
    for room_id, lang in wanted.items():
        if room_id not in feeds:
            feeds[room_id] = _Feed(bus, room_id, lang, changed)


async def _backlog(db: Any, last_id: int | None) -> tuple[int, list[Any]]:
    """The events to send on connect, and the id to go on from."""
    newest = await db.recent_events(BACKLOG_EVENTS)
    top = newest[0].id if newest else 0
    if last_id is None or last_id > top:  # a fresh panel, or an id from another database
        return 0, list(reversed(newest))
    return last_id, await db.events_after(last_id, BACKLOG_EVENTS)


async def _log_frames(
    events: Iterable[Any], lang: Lang, monitor: AdminMonitor, state: Any, cursor: int
) -> tuple[list[str], int]:
    """The ``log`` frames of ``events``, and the id to go on from."""
    events = list(events)
    for ev in events:
        talk_id = referenced_talk(ev)
        if talk_id is not None and talk_id not in monitor.titles and not is_free_talk(talk_id):
            try:
                talk = await state.db.get_talk(talk_id)
            except Exception:
                talk = None
            monitor.titles[talk_id] = talk.title if talk is not None else talk_id
    names = {room_id: worker.room.name for room_id, worker in state.workers.items()}
    frames = []
    for ev in events:
        data = {
            "id": ev.id, "ts": ev.ts, "room_id": ev.room_id, "room": names.get(ev.room_id), "level": ev.level,
            "type": ev.type, "alert": ev.level in ("warning", "error"),
            "text": describe_event(ev, lang, monitor.titles),
        }
        cursor = max(cursor, ev.id)
        frames.append(sse_frame("log", data, id=ev.id))
    return frames, cursor
