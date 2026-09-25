"""Autopilot: the agenda drives the rooms, with a manual override per room.

``Autopilot(db, workers, clock, lead_s=60, tz=..., events=...)``; the app
calls ``tick()`` every 5 s (``run()``). Each room has a mode, stored in the
``rooms`` table (``Room.mode``) and read from there once, so it survives a
restart:

``auto``
    If the room has at least one agenda talk today (the event's timezone,
    free sessions aside), the autopilot is in charge of it (Ruling 33):

      - a talk is *due* from ``lead_s`` before its start until its end; the
        room runs the due talk, opening it with the talk itself (engine,
        targets and glossary are the talk's);
      - when two overlap (the next one's lead against the current one's
        end), the later-starting one wins: the worker ends the current talk
        (its ``actual_end`` is now) before it opens the next;
      - with nothing due the room is idle: the free session the room
        started with, a talk past its end, or any other agenda talk is
        stopped, except (Ruling 44) the room's next agenda talk of today
        that the operator opened ahead of its slot: that one goes on, and
        when its slot comes the tick finds it running and leaves it be (no
        restart, same ``actual_start``).

    A room without agenda talks today is left alone: it keeps the free
    session of Task 5 (rooms with a source start one at boot).
``manual``
    ``tick()`` never touches the room.

Operator actions (Ruling 34): ``start_talk`` (ends the current talk, with
its real ``actual_end``, and opens the chosen one; on the talk already
running it only switches the mode) and ``end_talk`` (the room goes idle)
switch the room to ``manual`` and persist it, and so do the
Task 7 start/stop endpoints (they call ``set_mode``), or the next tick would
undo them. ``reconnect`` keeps the mode. Back to ``auto`` is explicit
(``set_mode``), and from then on the clock rules again.

Which talks can be due
    Any agenda talk whose slot contains now, whatever its status, except one
    that this Autopilot already opened and that has since ended inside its
    slot (its source finished, the operator ended it, the next talk took
    over): that one stays over. So after a restart (a new Autopilot on the
    same database, spec §6) the talk of the moment reopens, whether the old
    process crashed (``live``) or shut down cleanly (its stop left it
    ``done``); a talk the operator closed before its slot opens in it.
    Known limitation: the "already opened" set is in memory, so after a
    restart a talk that was ended early inside its slot reopens (the
    operator can switch the room to manual).

Each room has a lock: a tick's decision and its start/stop, and every
operator action, run one at a time per room, so a tick can never undo an
action that raced it. Rooms tick concurrently; one failing room (say, no
audio source: logged once per talk) does not keep the others from ticking.

"Probar con audio" (C1, Ruling 60) goes through ``play_test_audio`` under the
room's lock: refused while an agenda talk is open or (auto) due within
``lead_s``; otherwise the clip's test session is left alone by the tick
until it ends (a talk that comes due still replaces it).
Admin events (``events``, glosa/web/admin_events.py): ``room_mode``,
``talk_started`` (``by``: ``autopilot`` | ``operator``), ``room_reconnect``
and ``room_restart`` (Task 12: ``restart`` reopens a dead source for the
same talk). A talk's end is published by the app from
``RoomWorker.on_talk_end``, which sees every way a talk ends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from glosa.clock import Clock
from glosa.db import Database
from glosa.models import Room, Talk
from glosa.room import SoundCheckRefused, is_free_talk

log = logging.getLogger(__name__)

TICK_S = 5.0
LEAD_S = 60.0
Mode = Literal["auto", "manual"]
MODES: tuple[Mode, ...] = ("auto", "manual")


class NoTalkToRestart(LookupError):
    """``Autopilot.restart`` on a room that runs no talk."""


class Worker(Protocol):
    """The part of glosa.room.RoomWorker the autopilot drives."""

    room: Room
    talk: Talk | None

    def start(self, talk: Talk | None = None) -> Awaitable[None]: ...
    def stop(self) -> Awaitable[None]: ...
    def reconnect(self, reason: str) -> Awaitable[None]: ...


class EventSink(Protocol):
    """Where admin events go (glosa.web.admin_events.AdminEvents)."""

    def publish(self, kind: str, data: dict[str, Any]) -> Any: ...


class Autopilot:
    def __init__(
        self,
        db: Database,
        workers: dict[str, Any],
        clock: Clock,
        lead_s: float = LEAD_S,
        *,
        tz: str | tzinfo = "UTC",
        events: EventSink | None = None,
    ) -> None:
        self._db = db
        self._workers: dict[str, Worker] = workers
        self._clock = clock
        self._lead = timedelta(seconds=lead_s)
        self._tz = _zone(tz)
        self._events = events
        self._modes: dict[str, str] = {}
        self._modes_loaded = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._opened: set[str] = set()  # talk ids this Autopilot has opened
        self._failed: set[tuple[str, str]] = set()  # (room, talk) whose start failed, logged once

    # ------------------------------------------------------------ modes

    def mode(self, room_id: str) -> str:
        """The room's mode (KeyError for an unknown room)."""
        worker = self._worker(room_id)
        return self._modes.get(room_id, worker.room.mode)

    async def set_mode(self, room_id: str, mode: str) -> None:
        """Switch the room to ``auto`` or ``manual`` and persist it. Waits for
        a tick of that room in progress."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r} (expected auto or manual)")
        worker = self._worker(room_id)
        await self._load_modes()
        async with self._lock(room_id):
            await self._set_mode(room_id, worker, mode)

    # ------------------------------------------------------------ the clock

    async def tick(self, room_id: str | None = None) -> None:
        """Bring every room in ``auto`` (or just ``room_id``) in line with the
        agenda. Never raises for one room's failure."""
        await self._load_modes()
        now = self._clock.wall()
        rooms = [room_id] if room_id is not None else list(self._workers)
        results = await asyncio.gather(*(self._tick_room(r, now) for r in rooms), return_exceptions=True)
        for rid, result in zip(rooms, results, strict=True):
            if isinstance(result, Exception):
                log.error("autopilot: tick of room %s failed", rid, exc_info=result)

    async def run(self, interval_s: float = TICK_S) -> None:
        """``tick()`` every ``interval_s`` until cancelled. A failing tick is
        logged; a cancel waits for the tick in progress (starts and stops are
        never cut halfway)."""
        while True:
            await self._clock.sleep(interval_s)
            tick = asyncio.ensure_future(self._safe_tick())
            try:
                await asyncio.shield(tick)
            except asyncio.CancelledError:
                await asyncio.gather(tick, return_exceptions=True)
                raise

    async def in_charge(self, room_id: str) -> bool:
        """Whether the next tick decides what this room runs: ``auto`` and
        agenda talks today (or one due now, or one running)."""
        worker = self._worker(room_id)
        await self._load_modes()
        if self.mode(room_id) != "auto":
            return False
        now = self._clock.wall()
        agenda = await self._agenda(room_id, now)
        return self._owns(worker, agenda, now, self._due(agenda, now))

    async def next_talk(self, room_id: str) -> Talk | None:
        """The room's next agenda talk (the one to preselect for "start
        talk"): the first, by start, that is not done, not running and not
        over, up to tomorrow in the event's timezone."""
        worker = self._worker(room_id)
        now = self._clock.wall()
        current = worker.talk.id if worker.talk is not None else None
        for talk in await self._agenda(room_id, now):
            if talk.status != "done" and talk.id != current and talk.end > now:
                return talk
        return None

    # ------------------------------------------------------------ operator

    async def start_talk(self, room_id: str, talk_id: str) -> None:
        """Open ``talk_id`` in its room now (ending the current talk) and
        switch the room to manual. LookupError: no such talk; ValueError:
        another room's talk, a free session, or a room with no source."""
        worker = self._worker(room_id)
        await self._load_modes()
        talk = await self._db.get_talk(talk_id)
        if talk is None:
            raise LookupError(f"no talk {talk_id!r}")
        if is_free_talk(talk.id):
            raise ValueError("a free session is not an agenda talk")
        if talk.room_id != room_id:
            raise ValueError(f"talk {talk_id!r} belongs to room {talk.room_id!r}")
        async with self._lock(room_id):
            await self._set_mode(room_id, worker, "manual")
            if worker.talk is not None and worker.talk.id == talk.id:
                return  # already on: no engine restart, no second talk_started
            await self._open(worker, talk, by="operator")

    async def end_talk(self, room_id: str) -> None:
        """End the room's talk now (the room goes idle) and switch it to
        manual."""
        worker = self._worker(room_id)
        await self._load_modes()
        async with self._lock(room_id):
            await self._set_mode(room_id, worker, "manual")
            await worker.stop()

    async def reconnect(self, room_id: str) -> None:
        """The operator's "Reconectar": a new engine session for the running
        talk. The mode stays as it is."""
        worker = self._worker(room_id)
        await worker.reconnect("manual")
        self._publish("room_reconnect", {"room_id": room_id})

    async def restart(self, room_id: str) -> Talk:
        """The panel's "Reconectar" for a room whose source is down: open the
        source again for the talk the room runs -- the same talk (no talk
        end, no free session, same actual_start). The talk is read under the
        room's lock, so a tick or an operator action in progress is never
        undone by a stale one. The mode stays. NoTalkToRestart: no talk
        running; ValueError: the room has no audio source."""
        worker = self._worker(room_id)
        async with self._lock(room_id):
            talk = worker.talk
            if talk is None:
                raise NoTalkToRestart(f"room {room_id!r} has no talk to restart")
            await worker.start(talk)
        await self._log(room_id, "restart", f"{talk.id}: source reopened by the operator")
        self._publish("room_restart", {"room_id": room_id, "talk_id": talk.id})
        return talk

    async def play_test_audio(self, room_id: str, path: str) -> None:
        """"Probar con audio" (C1, Ruling 60), under the room's lock: test
        audio must never end, replace or pollute an agenda talk.
        SoundCheckRefused (a message for the operator) while an agenda talk
        is open in the room or, in ``auto``, when one is due now or within
        the next ``lead_s`` (the autopilot would cut the clip off to open
        it). Otherwise ``worker.play_file(path)``: a test session, which the
        tick leaves alone until the clip ends."""
        worker = self._worker(room_id)
        await self._load_modes()
        async with self._lock(room_id):
            current = worker.talk
            if current is not None and not is_free_talk(current.id):
                raise SoundCheckRefused.open_talk(current)
            if self.mode(room_id) == "auto":
                now = self._clock.wall()
                agenda = await self._agenda(room_id, now)
                soon = self._due(agenda, now) or self._due(agenda, now + self._lead)
                if soon is not None:
                    lead = round(self._lead.total_seconds())
                    raise SoundCheckRefused(
                        f"the agenda talk {soon.title!r} starts at {soon.start.astimezone(self._tz):%H:%M} "
                        f"and the autopilot opens it {lead} s before: test audio would be cut off. "
                        "Try again after it, or switch the room to manual."
                    )
            await worker.play_file(path)

    # ------------------------------------------------------------ internals

    async def _tick_room(self, room_id: str, now: datetime) -> None:
        worker = self._workers[room_id]
        async with self._lock(room_id):
            try:
                if self.mode(room_id) != "auto":
                    return
                agenda = await self._agenda(room_id, now)
                due = self._due(agenda, now)
                if not self._owns(worker, agenda, now, due):
                    return
                current = worker.talk
                if due is not None:
                    if current is None or current.id != due.id:
                        await self._open(worker, due, by="autopilot")
                elif getattr(worker, "testing", False):
                    pass  # C1: a "Probar con audio" clip plays until it ends
                elif current is not None and not self._early_next(current, agenda, now):
                    log.info("autopilot: room %s: nothing scheduled now, stopping %s", room_id, current.id)
                    await self._log(room_id, "autopilot", f"idle: nothing scheduled now (stopped {current.id})")
                    await worker.stop()
            finally:
                # task-11r-brief.md item 7: refresh the room's cached
                # next-agenda-talk (auto or manual: the public list and the
                # station's between-talks screen want it either way) after
                # this tick's own open/stop decision, so it reflects
                # whichever talk is current now -- not the one about to
                # replace it (computing this any earlier could return the
                # very talk this tick is opening: worker.talk isn't due's
                # id yet at the top of this method).
                refresh_next = getattr(worker, "set_next_talk", None)
                if refresh_next is not None:
                    refresh_next(await self.next_talk(room_id))

    def _early_next(self, current: Talk, agenda: list[Talk], now: datetime) -> bool:
        """Ruling 44: whether ``current`` is the room's next agenda talk,
        opened ahead of its slot (by the operator), which then goes on: its
        slot finds it running. It must not have ended, and must start today
        (a rehearsal of tomorrow's first talk is stopped)."""
        if is_free_talk(current.id):
            return False
        today = now.astimezone(self._tz).date()
        upcoming = [t for t in agenda if t.status != "done" and t.end > now]
        return (
            bool(upcoming)
            and upcoming[0].id == current.id
            and upcoming[0].start.astimezone(self._tz).date() == today
        )

    def _owns(self, worker: Worker, agenda: list[Talk], now: datetime, due: Talk | None) -> bool:
        today = now.astimezone(self._tz).date()
        if any(t.start.astimezone(self._tz).date() == today for t in agenda) or due is not None:
            return True
        current = worker.talk
        return current is not None and not is_free_talk(current.id)  # an agenda talk left running

    def _due(self, agenda: list[Talk], now: datetime) -> Talk | None:
        due = [
            t
            for t in agenda
            if t.start - self._lead <= now < t.end
            and not (
                t.status == "done"
                and t.id in self._opened
                and t.actual_end is not None
                and t.actual_end >= t.start - self._lead
            )
        ]
        return max(due, key=lambda t: (t.start, t.id), default=None)

    async def _agenda(self, room_id: str, now: datetime) -> list[Talk]:
        """The room's agenda talks from yesterday to tomorrow (event
        timezone), by start: a slot can cross midnight."""
        today = now.astimezone(self._tz).date()
        talks: list[Talk] = []
        for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
            talks += await self._db.get_talks(room_id, day)
        return sorted((t for t in talks if not is_free_talk(t.id)), key=lambda t: (t.start, t.id))

    async def _open(self, worker: Worker, talk: Talk, *, by: str) -> None:
        """Open ``talk`` on its worker. The operator gets the worker's error;
        the autopilot logs it (once per talk) and tries again next tick."""
        room_id = worker.room.id
        reopened = talk.status == "done"
        talk.actual_end = None
        if by == "operator":
            await worker.start(talk)
        else:
            try:
                await worker.start(talk)
            except Exception as exc:
                if (room_id, talk.id) not in self._failed:
                    self._failed.add((room_id, talk.id))
                    log.warning("autopilot: room %s: could not open %s: %s", room_id, talk.id, exc)
                    await self._log(room_id, "autopilot_error", f"could not open {talk.id}: {exc}", level="error")
                return
            self._failed.discard((room_id, talk.id))
        self._opened.add(talk.id)
        if reopened:  # it is not over any more
            await self._db.update_talk(talk.id, actual_end=None)
        log.info("autopilot: room %s: opened %s (%s)", room_id, talk.id, by)
        await self._log(room_id, "autopilot", f"opened {talk.id} ({by}): {talk.title}")
        self._publish("talk_started", {"room_id": room_id, "talk_id": talk.id, "title": talk.title, "by": by})

    async def _set_mode(self, room_id: str, worker: Worker, mode: str) -> None:
        if self._modes.get(room_id) == mode and worker.room.mode == mode:
            return
        await self._db.set_room_mode(room_id, mode)
        self._modes[room_id] = mode
        worker.room.mode = mode  # type: ignore[assignment]
        log.info("autopilot: room %s: mode %s", room_id, mode)
        await self._log(room_id, "mode", f"mode: {mode}")
        self._publish("room_mode", {"room_id": room_id, "mode": mode})

    async def _load_modes(self) -> None:
        """The modes stored in the database win over the workers' Room (once)."""
        if self._modes_loaded:
            return
        stored = {room.id: room.mode for room in await self._db.get_rooms()}
        if self._modes_loaded:  # another caller got here first
            return
        self._modes_loaded = True
        for room_id, worker in self._workers.items():
            mode = self._modes.get(room_id) or stored.get(room_id) or worker.room.mode
            self._modes[room_id] = mode
            worker.room.mode = mode  # type: ignore[assignment]

    async def _safe_tick(self) -> None:
        try:
            await self.tick()
        except Exception:
            log.exception("autopilot: tick failed")

    async def _log(self, room_id: str, type: str, message: str, *, level: str = "info") -> None:
        try:
            await self._db.log_event(room_id, level, type, message)
        except Exception:
            log.exception("autopilot: could not log %s", type)

    def _publish(self, kind: str, data: dict[str, Any]) -> None:
        if self._events is not None:
            self._events.publish(kind, data)

    def _lock(self, room_id: str) -> asyncio.Lock:
        return self._locks.setdefault(room_id, asyncio.Lock())

    def _worker(self, room_id: str) -> Worker:
        try:
            return self._workers[room_id]
        except KeyError:
            raise KeyError(f"unknown room {room_id!r}") from None


def _zone(tz: str | tzinfo) -> tzinfo:
    if not isinstance(tz, str):
        return tz
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r: the autopilot's days are UTC", tz)
        return timezone.utc

