"""TalkCheckScheduler and free sessions (final-review-A I2): a free session
has no agenda to disagree with, so it is never checked. Its own module:
tests/test_talk_check.py skips as a whole without the optional "jev" extra
(typesafe_sdk), and this needs none of it (glosa.talk_check imports it
lazily).
"""

from __future__ import annotations

from dataclasses import dataclass

from glosa.clock import FakeClock
from glosa.db import Segment
from glosa.models import Talk
from glosa.talk_check import MIN_WORDS, TalkCheckResult, TalkCheckScheduler, TalkMismatchStore

FREE_ID = "free-r1-20260924T100000"


def _talk(talk_id: str, title: str) -> Talk:
    return Talk(
        id=talk_id, room_id="r1", title=title, speakers=[], language="en", targets=["es"], engine="fast",
        start=None, end=None, abstract="", tags=[], glossary=[], status="live", actual_start=None, actual_end=None,
    )


class FakeDb:
    def __init__(self, talk_id: str, text: str) -> None:
        self.calls: list[tuple[str, str]] = []
        self._segment = Segment(
            id=0, talk_id=talk_id, room_id="r1", lang="en", kind="source", version="live", text=text,
            t_start=0.0, t_end=30.0, created_at="",
        )

    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Segment]:
        self.calls.append((talk_id, lang))
        return [self._segment] if talk_id == self._segment.talk_id else []


@dataclass
class _Room:
    id: str = "r1"


class FakeWorker:
    def __init__(self, talk: Talk) -> None:
        self.room = _Room()
        self.talk = talk


class FakeAutopilot:
    def mode(self, room_id: str) -> str:
        return "auto"

    async def next_talk(self, room_id: str) -> Talk | None:
        return _talk("t2", "Next Talk")


class MismatchChecker:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self, text: str, current: Talk, next_talk: Talk | None) -> TalkCheckResult | None:
        self.calls += 1
        return TalkCheckResult(label="next", confidence=0.9)

    async def aclose(self) -> None:
        pass


async def test_a_free_session_makes_no_calls_and_raises_no_suggestion() -> None:
    words = " ".join(f"w{i}" for i in range(MIN_WORDS * 3))
    db = FakeDb(FREE_ID, words)
    checker = MismatchChecker()
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(FakeWorker(_talk(FREE_ID, "Sesión libre")), FakeAutopilot(), store, checker, db,
                                   FakeClock())

    await scheduler.tick()
    await scheduler.tick()

    assert checker.calls == 0 and db.calls == []
    assert store.get("r1") is None


async def test_an_agenda_talk_is_still_checked() -> None:
    """The same setup with an agenda talk does ask (and suggests after two
    mismatches): the free-session guard is the only difference."""
    words = " ".join(f"w{i}" for i in range(MIN_WORDS * 3))
    db = FakeDb("t1", words)
    checker = MismatchChecker()
    store = TalkMismatchStore()
    scheduler = TalkCheckScheduler(FakeWorker(_talk("t1", "Charla")), FakeAutopilot(), store, checker, db, FakeClock())

    await scheduler.tick()
    db._segment = Segment(**{**db._segment.__dict__, "text": words + " " + words, "t_end": 60.0})
    await scheduler.tick()

    assert checker.calls == 2
    assert store.get("r1") is not None
