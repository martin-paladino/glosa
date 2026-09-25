"""Task 18: Jev suggests "switch to manual?" when a room's captions no
longer match what the agenda says is happening.

Rooms in **auto** mode follow the agenda (glosa/scheduler.py Autopilot).
Reality drifts: a talk runs late, the next speaker starts early, there is
Q&A, or a break. Every TALK_CHECK_EVERY_S s, for each room in auto mode with
a talk live, TalkCheckScheduler asks Jev (TypeSafe SDK, "jev-latest") ONE
multiple-choice question -- the SDK's Choice primitive (confirmed present
in typesafe-sdk>=0.7.1, so this never falls back to Noul): given the last
~60 s of the room's source-language captions and the agenda context (the
current talk's title/abstract/speakers, and the next agenda talk's), which
of four things is happening: ``scheduled`` (the talk on the agenda now),
``next`` (the next agenda talk already started early), ``qa`` (audience
Q&A of the scheduled talk -- NOT a mismatch, it still belongs to it), or
``break`` (no talk: silence, applause, room chatter).

Two mismatches in a row (``next`` or ``break``) raise ONE admin suggestion
for the room (TalkMismatchStore, read by glosa/web/admin_stream.py as an
"info"/yellow Atención issue, kind "talk_mismatch") -- never changes the
room's mode or talk by itself. It clears the moment a later check agrees
(``scheduled``/``qa``), the talk changes, or the room leaves auto mode.

Pattern: glosa/room_quality.py + glosa/quality.py, folded into one module
since -- like glosa/summary.py's SummaryScheduler -- this is a slow,
independent poller, not something on the hot caption path that needs a
backgrounded call. typesafe_sdk is never imported at this module's top
level: build_talk_checker() imports it lazily, only once a caller has
confirmed settings.typesafe_api_key is set, so Glosa stays usable without
the "jev" extra installed. TalkChecker.check() then imports Choice the
same way -- by the time a TalkChecker exists at all, typesafe_sdk is
already in sys.modules, so that's just a cheap, cached lookup, never a
fresh attempt (see glosa/room_quality.py's module docstring for the same
reasoning).

Cost accounting: glosa/config.py's Prices has no Jev price (Jev's Noul/
Choice calls are not billed per-token the way Gemini calls are), so no
cost is invented and no add_external_cost() call is made here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from glosa.clock import Clock
from glosa.models import Talk
from glosa.room import is_free_talk

logger = logging.getLogger(__name__)
log = logger  # both spellings used elsewhere in this codebase; keep one object

# Every this many seconds, per room (a module constant per spec).
TALK_CHECK_EVERY_S = 30.0
# How much of the talk's recent source-language captions a check looks at.
CHECK_WINDOW_S = 60.0
# Cost guard: skip a check with fewer than this many new words since the
# last one that actually ran.
MIN_WORDS = 40
# Two mismatches in a row raise the suggestion.
MISMATCH_STREAK = 2
# A "next" or "break" answer is a mismatch; "qa" is NOT (it still belongs
# to the scheduled talk).
MISMATCH_LABELS = frozenset({"next", "break"})
LABELS = frozenset({"scheduled", "next", "qa", "break"})

_MODEL = "jev-latest"
_QUESTION_KEY = "talk_check"
_WARNING_INTERVAL_S = 60.0  # rate-limit "Jev call failed" warnings

# Same indirection as glosa/quality.py: lets a test control just this clock
# (for the failure-warning rate limit) without skewing asyncio's own timing.
_monotonic = time.monotonic

_INSTRUCTIONS = (
    "You are watching a live conference room's caption feed, in the room's own "
    "source language (not a translation). Given a short excerpt of the last "
    "captions, decide which of the following best describes what is happening "
    "on stage right now."
)


def _describe(talk: Talk) -> str:
    speakers = ", ".join(talk.speakers) if talk.speakers else "an unnamed speaker"
    heading = f'"{talk.title}" by {speakers}'
    return f"{heading}. {talk.abstract}" if talk.abstract else heading


def build_criteria(current: Talk, next_talk: Talk | None) -> dict[str, str]:
    """The Choice question's four criteria, filled in with the agenda's
    current and (if any) next talk. Exposed (not ``_``-prefixed) so tests
    and the live check can inspect exactly what Jev is asked."""
    next_desc = (
        _describe(next_talk) if next_talk is not None else "a different talk than the one currently scheduled"
    )
    return {
        "scheduled": f"The captions are about the talk currently on the agenda: {_describe(current)}",
        "next": (
            "The captions are about the NEXT agenda talk, which appears to have started early, "
            f"replacing the scheduled one: {next_desc}"
        ),
        "qa": (
            "The captions are audience question-and-answer time following the scheduled talk above "
            "-- not a new talk starting."
        ),
        "break": (
            "The captions are a break: silence, applause, music, or room chatter unrelated to any talk "
            "-- nothing is being presented."
        ),
    }


@dataclass(frozen=True)
class TalkCheckResult:
    label: str  # one of LABELS
    confidence: float


class TalkCheckerLike(Protocol):
    async def check(self, text: str, current: Talk, next_talk: Talk | None) -> TalkCheckResult | None: ...

    async def aclose(self) -> None: ...


class TalkChecker:
    """One Choice question per call: which of LABELS best matches ``text``
    (the room's recent source-language captions) given the agenda context.

    client: a TypeSafeClient-shaped object with an async system_one()
    (dependency injection for tests; production code leaves it to
    build_talk_checker(), which constructs a real AsyncTypeSafeClient).

    failures: count of check() calls that errored or timed out -- Jev is
    optional, so these never raise into the caller; they just return None.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.failures = 0
        self._last_warning_at: float | None = None

    async def check(
        self, text: str, current: Talk, next_talk: Talk | None, *, timeout_s: float = 5.0
    ) -> TalkCheckResult | None:
        from typesafe_sdk import Choice  # see module docstring: already imported by now

        criteria = build_criteria(current, next_talk)
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._client.system_one(
                    state={"captions": text},
                    questions={_QUESTION_KEY: Choice(instructions=_INSTRUCTIONS, criteria=criteria)},
                    model=_MODEL,
                )
            answer = response.answers[_QUESTION_KEY]
            return TalkCheckResult(label=answer.choice, confidence=answer.confidence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Jev is optional: never break the room
            self.failures += 1
            self._log_failure(exc)
            return None

    def _log_failure(self, exc: Exception) -> None:
        now = _monotonic()
        if self._last_warning_at is None or now - self._last_warning_at >= _WARNING_INTERVAL_S:
            logger.warning("Jev talk check failed, suggestion stays off for now: %s", exc)
            self._last_warning_at = now

    async def aclose(self) -> None:
        await self._client.aclose()


def build_talk_checker(api_key: str) -> TalkChecker | None:
    """Build a TalkChecker for ``api_key`` (the caller already checked
    settings.typesafe_api_key is truthy before calling this). Imports
    typesafe_sdk lazily -- see module docstring. On ImportError (the "jev"
    extra isn't installed) logs one warning and returns None: the feature
    stays off exactly like without a key at all."""
    try:
        from typesafe_sdk import AsyncTypeSafeClient
    except ImportError:
        logger.warning(
            "TYPESAFE_API_KEY is set but the 'jev' extra is not installed: talk check stays off"
        )
        return None
    return TalkChecker(AsyncTypeSafeClient(api_key=api_key))


@dataclass(frozen=True)
class TalkMismatchSuggestion:
    """One room's current "switch to manual?" suggestion (or none)."""

    talk_id: str  # the scheduled talk this suggestion is about
    guess: str  # "next" | "break" -- the mismatch check() last returned
    next_title: str | None  # the agenda's next talk's title, when known


class TalkMismatchStore:
    """The latest TalkMismatchSuggestion per room, in memory. Written only
    by TalkCheckScheduler, read only by glosa/web/admin_stream.py's
    snapshot (mirrors glosa.summary.SummaryStore's shape)."""

    def __init__(self) -> None:
        self._latest: dict[str, TalkMismatchSuggestion] = {}

    def get(self, room_id: str) -> TalkMismatchSuggestion | None:
        return self._latest.get(room_id)

    def set(self, room_id: str, suggestion: TalkMismatchSuggestion) -> None:
        self._latest[room_id] = suggestion

    def clear(self, room_id: str) -> None:
        self._latest.pop(room_id, None)


class _RoomLike(Protocol):
    """What TalkCheckScheduler needs from a RoomWorker: its room id and the
    current talk (or None). A test can hand it a much smaller double."""

    room: Any
    talk: Any


class _AutopilotLike(Protocol):
    """The part of glosa.scheduler.Autopilot this needs: the room's mode
    (sync -- Autopilot.mode() never awaits) and its next agenda talk."""

    def mode(self, room_id: str) -> str: ...

    async def next_talk(self, room_id: str) -> Talk | None: ...


class _DbLike(Protocol):
    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Any]: ...


class TalkCheckScheduler:
    """One room's talk-check loop. Owned by app.py's lifespan (one task per
    room, mirroring the autopilot and summary tasks) so glosa/room.py needs
    no new hook at all -- unlike QualityFeed/SummaryScheduler, this never
    touches the room's own cost accounting or caption pipeline.

    checker: None means the feature is off (no key, extra missing, or
    engine_mode "fake" -- that gate is app.py's make_talk_checker(), not
    here): tick() then makes no calls at all, the same as a manual room or
    an idle one.
    """

    def __init__(
        self,
        worker: _RoomLike,
        autopilot: _AutopilotLike,
        store: TalkMismatchStore,
        checker: TalkCheckerLike | None,
        db: _DbLike,
        clock: Clock,
    ) -> None:
        self._worker = worker
        self._autopilot = autopilot
        self._store = store
        self._checker = checker
        self._db = db
        self._clock = clock
        self._talk_id: str | None = None
        self._marker = float("-inf")  # newest t_end of source captions already checked, this talk
        self._streak = 0  # consecutive mismatches (next or break), this talk
        self._busy = False

    async def run(self, interval_s: float = TALK_CHECK_EVERY_S) -> None:
        while True:
            await self._clock.sleep(interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("room %s: talk check loop failed", self._worker.room.id)

    async def tick(self) -> None:
        room_id = self._worker.room.id
        talk = self._worker.talk
        # final-review-A I2: a free session has no agenda to disagree with.
        if talk is None or is_free_talk(talk.id) or self._checker is None or self._autopilot.mode(room_id) != "auto":
            self._reset(room_id)
            return
        if talk.id != self._talk_id:
            self._reset(room_id)
            self._talk_id = talk.id
        if self._busy:
            return  # one call in flight per room: a tick that finds it busy just skips
        self._busy = True
        try:
            await self._maybe_check(talk, room_id)
        finally:
            self._busy = False

    def _reset(self, room_id: str) -> None:
        """A new talk, a room that left auto, or one with no talk running:
        forget the streak and the source-text marker, and dismiss any
        suggestion still showing for it."""
        self._talk_id = None
        self._marker = float("-inf")
        self._streak = 0
        self._store.clear(room_id)

    async def _maybe_check(self, talk: Talk, room_id: str) -> None:
        assert self._checker is not None
        segments = await self._db.get_segments(talk.id, talk.language, "live")
        if not segments:
            return
        ref = segments[-1].t_end
        window = [s for s in segments if s.t_end >= ref - CHECK_WINDOW_S]
        new_words = sum(len(s.text.split()) for s in window if s.t_end > self._marker)
        if new_words < MIN_WORDS:
            return
        text = " ".join(s.text for s in window)
        try:
            next_talk = await self._autopilot.next_talk(room_id)
        except Exception:
            log.exception("room %s: talk check: could not read the next agenda talk", room_id)
            next_talk = None
        try:
            result = await self._checker.check(text, talk, next_talk)
        except asyncio.CancelledError:
            raise
        except Exception:  # a misbehaving injected checker must not break the room either
            log.exception("room %s: talk check failed unexpectedly", room_id)
            return
        if result is None:
            return  # TalkChecker already logged it; retry next tick, marker not advanced
        self._marker = ref
        if result.label in MISMATCH_LABELS:
            self._streak += 1
            if self._streak >= MISMATCH_STREAK:
                self._store.set(
                    room_id,
                    TalkMismatchSuggestion(
                        talk_id=talk.id,
                        # "next" with no next talk on the agenda (e.g. the day's
                        # last talk) can only mean the scheduled talk is over:
                        # say "break" rather than an empty «» title.
                        guess=result.label if result.label != "next" or next_talk is not None else "break",
                        next_title=next_talk.title if next_talk is not None else None,
                    ),
                )
        else:  # "scheduled" or "qa": agreement clears a pending suggestion
            self._streak = 0
            self._store.clear(room_id)
