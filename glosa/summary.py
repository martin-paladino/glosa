"""Task 17, "¿Qué me perdí?" ("What did I miss?"): a 3-5 bullet summary of
the last SUMMARY_WINDOW_S s of a talk, per language, for someone who walks
into the audience room view late or looks away for a bit.

SummaryScheduler owns one room. Every SUMMARY_EVERY_S s, while the room's
talk is live, it asks Summarizer (gemini-3.5-flash-lite, the same
google-genai client pattern as glosa/text/translator.py's Translator: same
thinking_level MINIMAL, same cost formula) for a fresh summary of each
language the room streams (RoomWorker.stream_langs()), built from the
CLOSED caption segments the room already saved (glosa/db.py
Database.get_segments(talk_id, lang, "live") -- the cleanest source: it is
already just the closed, per-language text, with the t_start/t_end the room
itself timestamps). Unlike Translator, Summarizer makes a single call, no
retry, no fallback model: a 429/503 (or anything else) is simply logged (at
most once a minute) and left for the next tick, SUMMARY_EVERY_S later, to
try again -- simpler, and a summary is never on the critical path of a
caption reaching the screen.

Cost guard: a language is skipped when fewer than MIN_NEW_WORDS words
arrived in its window since that language's last summary (tracked as the
newest t_end already covered); at most one summarize() call is in flight
for the room at a time (a tick that finds the room still busy with the
previous one just skips, the same as a tick with too few new words). A
successful call's cost joins the room's own cost accounting through
RoomWorker.add_external_cost() (component "summary"), the same
_add_cost/COST_FLUSH_S machinery glosa/room.py already uses for the engine
and the translation lane.

Summaries live in memory only (SummaryStore: the latest per room+lang, with
generated_at and the talk_id) and reset -- every stored summary for the room
is dropped -- the moment a new talk starts on it (detected by the talk id
changing between ticks): a late reader must never see a stale talk's bullets
under a fresh one's, even for the few minutes before the new talk earns its
own summary.

``engine_mode: fake``: FakeSummarizer builds deterministic bullets from the
window's last words, no API call, no key -- so ``make demo-fake`` shows the
feature.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import types

from glosa.clock import Clock
from glosa.db import Segment

log = logging.getLogger(__name__)

SUMMARY_EVERY_S = 180  # how often a room is considered for a fresh summary
SUMMARY_WINDOW_S = 300  # how much of the talk (its last ~5 min) a summary covers
MIN_NEW_WORDS = 40  # cost guard: skip a language with fewer new words than this
SUMMARY_TIMEOUT_S = 30.0  # M2: one Gemini call; a hung one must not freeze the room's loop
_FAILURE_LOG_EVERY_S = 60.0
_MAX_BULLETS = 5

_BULLET_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


@dataclass
class Summary:
    talk_id: str
    generated_at: float  # unix epoch seconds (Clock.wall().timestamp(), like CaptionBus's ts)
    bullets: list[str]


@dataclass
class SummarizeResult:
    bullets: list[str]
    usd: float


class SummaryStore:
    """The latest Summary per (room_id, lang), in memory. reset() is what
    gives a new talk a clean slate (SummaryScheduler calls it when it sees
    the talk id change)."""

    def __init__(self) -> None:
        self._latest: dict[tuple[str, str], Summary] = {}

    def get(self, room_id: str, lang: str) -> Summary | None:
        return self._latest.get((room_id, lang))

    def set(self, room_id: str, lang: str, summary: Summary) -> None:
        self._latest[(room_id, lang)] = summary

    def reset(self, room_id: str) -> None:
        for key in [k for k in self._latest if k[0] == room_id]:
            del self._latest[key]


def _build_prompt(title: str, target: str) -> str:
    return (
        f'You are helping someone who just tuned into a live conference talk titled "{title}" (or '
        "looked away for a bit) catch up. Read the caption excerpt below -- the last few minutes of "
        f"the talk -- and write 3 to 5 short bullet points summarising what was said, in {target}. "
        "Keep technical terms exactly as spoken: do not translate or alter them. Never invent content "
        "that is not in the excerpt. Reply with ONLY the bullets, one per line, each starting with "
        '"- ": no heading, no notes, no preamble.'
    )


def _parse_bullets(text: str) -> list[str]:
    bullets = []
    for line in text.splitlines():
        line = _BULLET_MARKER.sub("", line.strip()).strip()
        if line:
            bullets.append(line)
    return bullets[:_MAX_BULLETS]


class Summarizer:
    """gemini-3.5-flash-lite, one call per (window text, target language),
    the talk's title as context. The genai client can be injected
    (`client=`), the same way Translator's can, so tests never spend API
    budget; production code leaves it unset and builds a real
    genai.Client(api_key=...)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        *,
        price_in_per_m: float = 0.30,
        price_out_per_m: float = 2.50,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.price_in_per_m = price_in_per_m
        self.price_out_per_m = price_out_per_m
        self._client = client if client is not None else genai.Client(api_key=api_key)

    async def aclose(self) -> None:
        """Release the client's HTTP connections (the owner calls it when
        done; app.py's lifespan does, at shutdown)."""
        close = getattr(getattr(self._client, "aio", None), "aclose", None)
        if close is not None:
            await close()

    def _cost_usd(self, usage_metadata: Any) -> float:
        if usage_metadata is None:
            return 0.0
        input_tokens = getattr(usage_metadata, "prompt_token_count", None) or 0
        output_tokens = (getattr(usage_metadata, "candidates_token_count", None) or 0) + (
            getattr(usage_metadata, "thoughts_token_count", None) or 0
        )
        return (input_tokens * self.price_in_per_m + output_tokens * self.price_out_per_m) / 1_000_000

    async def summarize(self, text: str, target: str, title: str) -> SummarizeResult:
        config = types.GenerateContentConfig(
            system_instruction=_build_prompt(title, target),
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        )
        async with asyncio.timeout(SUMMARY_TIMEOUT_S):  # M2: google-genai's default timeout is None
            response = await self._client.aio.models.generate_content(model=self.model, contents=text, config=config)
        bullets = _parse_bullets(response.text or "")
        return SummarizeResult(bullets=bullets, usd=self._cost_usd(response.usage_metadata))


class FakeSummarizer:
    """``engine_mode: fake``: bullets built from the window's last words, at
    once and for free, so a demo shows the feature without a Gemini key."""

    async def summarize(self, text: str, target: str, title: str) -> SummarizeResult:
        await asyncio.sleep(0)
        words = text.split()
        if not words:
            return SummarizeResult(bullets=[], usd=0.0)
        tail = words[-30:]
        size = -(-len(tail) // 3)  # ceil(len/3): up to 3 chunks
        chunks = [tail[i : i + size] for i in range(0, len(tail), size)]
        bullets = [f"[{target}] " + " ".join(chunk) for chunk in chunks if chunk][:_MAX_BULLETS]
        return SummarizeResult(bullets=bullets, usd=0.0)


class _RoomLike(Protocol):
    """What SummaryScheduler needs from a RoomWorker: room.id, the current
    talk (or None), the languages it streams, and a way to join its cost
    accounting. A test can hand it a much smaller double."""

    room: Any
    talk: Any

    def stream_langs(self) -> set[str]: ...
    def add_external_cost(self, component: str, usd: float, units: float) -> None: ...


class _DbLike(Protocol):
    async def get_segments(self, talk_id: str, lang: str, version: str) -> list[Segment]: ...


class SummarizerLike(Protocol):
    async def summarize(self, text: str, target: str, title: str) -> SummarizeResult: ...


class SummaryScheduler:
    """One room's summary loop (see the module docstring). Owned by
    app.py's lifespan (one task per room, mirroring the autopilot task) so
    glosa/room.py needs only the small add_external_cost() hook."""

    def __init__(self, worker: _RoomLike, store: SummaryStore, summarizer: SummarizerLike, db: _DbLike, clock: Clock) -> None:
        self._worker = worker
        self._store = store
        self._summarizer = summarizer
        self._db = db
        self._clock = clock
        self._talk_id: str | None = None
        self._marker: dict[str, float] = {}  # lang -> newest t_end already covered by a summary
        self._busy = False
        self._last_failure_log = float("-inf")

    async def run(self, interval_s: float = SUMMARY_EVERY_S) -> None:
        while True:
            await self._clock.sleep(interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("room %s: summary loop failed", self._worker.room.id)

    async def tick(self) -> None:
        talk = self._worker.talk
        if talk is None:
            return
        if talk.id != self._talk_id:
            self._talk_id = talk.id
            self._marker.clear()
            self._store.reset(self._worker.room.id)
        if self._busy:
            return
        self._busy = True
        try:
            for lang in self._worker.stream_langs():
                await self._maybe_summarize(talk, lang)
        finally:
            self._busy = False

    async def _maybe_summarize(self, talk: Any, lang: str) -> None:
        segments = await self._db.get_segments(talk.id, lang, "live")
        if not segments:
            return
        ref = segments[-1].t_end
        window = [s for s in segments if s.t_end >= ref - SUMMARY_WINDOW_S]
        marker = self._marker.get(lang, float("-inf"))
        new_words = sum(len(s.text.split()) for s in window if s.t_end > marker)
        if new_words < MIN_NEW_WORDS:
            return
        text = " ".join(s.text for s in window)
        try:
            result = await self._summarizer.summarize(text, lang, talk.title)
        except Exception:
            self._log_failure()
            return
        self._marker[lang] = ref
        self._store.set(
            self._worker.room.id,
            lang,
            Summary(talk_id=talk.id, generated_at=self._clock.wall().timestamp(), bullets=result.bullets),
        )
        if result.usd > 0:
            self._worker.add_external_cost("summary", result.usd, 1.0)

    def _log_failure(self) -> None:
        now = self._clock.now()
        if now - self._last_failure_log >= _FAILURE_LOG_EVERY_S:
            self._last_failure_log = now
            log.warning("room %s: summary generation failed", self._worker.room.id)
