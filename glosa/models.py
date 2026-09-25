"""Shared dataclasses used across Glosa's pipeline (audio -> engines -> captions).

These are the "global interfaces" every later task builds on: keep them stable,
keep them dependency-free (stdlib only), and do not add fields that aren't in
the spec without checking with the plan first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class AudioChunk:
    """One chunk of raw audio.

    t: seconds since the start of the room's audio stream (the room's audio
    clock, not wall-clock time).
    """

    pcm: bytes
    t: float


@dataclass
class VadEvent:
    """A voice-activity transition detected by our own (server-side) VAD."""

    kind: Literal["speech_start", "pause"]
    t: float


@dataclass
class EngineEvent:
    """One event emitted by an Engine (Live Translate, transcribe-live, fake, ...).

    Text kinds:
      - "source_delta": source-language text. Normally APPENDED to the open
        segment (Live Translate). With meta["interim"] = True (transcribe-live)
        its text is the open segment's WHOLE text so far: it REPLACES the
        open segment, it is not appended.
      - "source_final": the open segment's final text (it replaces and closes
        it); "" means it was not speech after all.
      - "target_delta": translated text, appended.

    meta conventions:
      - kind == "error": {"code": int, "retryable": bool}, plus
        {"payment": True} when credit is exhausted (a 402-style stop).
      - kind == "go_away": {"time_left_s": float}
      - usage accounting (any kind): {"usd": float}. This is an INCREMENT: the
        cost accrued since the previous event of the same engine, not a
        running total. Consumers sum it (e.g. CostTracker.add, db.add_cost).
    """

    kind: Literal[
        "source_delta",
        "target_delta",
        "source_final",
        "go_away",
        "error",
        "closed",
    ]
    text: str = ""
    lang: str | None = None
    t_recv: float = 0.0
    meta: dict = field(default_factory=dict)


@dataclass
class EngineConfig:
    kind: Literal["fast", "glossary", "fake"]
    source_lang: str
    target_lang: str | None
    vocabulary: list[str] = field(default_factory=list)
    fixture_path: str | None = None


@dataclass
class CaptionMsg:
    """JSON payload sent to the audience over SSE. id = Last-Event-ID.

    type:
      - "append": ``text`` is added to the end of segment ``seg``;
      - "set": ``text`` is the WHOLE text of the open segment ``seg`` so far
        and replaces it (the glossary engine's source, rewritten by each
        transcribe-live interim); "" removes it (not speech after all). Only
        the track's open segment is ever set;
      - "close": segment ``seg`` is finished;
      - "talk" / "status": ``data`` (the talk now on, the room's state).

    ts: wall-clock time the message was published, as epoch seconds (set by
    CaptionBus.publish). Lets the audience view show an HH:MM margin next to
    replayed history; None only for messages built by hand (e.g. in tests)
    without going through the bus.
    """

    id: int
    type: Literal["append", "set", "close", "talk", "status"]
    seg: int | None = None
    text: str | None = None
    data: dict | None = None
    ts: float | None = None


@dataclass
class GlossaryTerm:
    term: str
    keep_in_english: bool
    translation: str | None = None


@dataclass
class Room:
    id: str
    slug: str
    name: str
    source_type: Literal["file", "url", "youtube", "emitter"]
    source_url: str | None
    mode: Literal["auto", "manual"]
    public_token: str
    default_targets: list[str]


@dataclass
class Talk:
    id: str
    room_id: str
    title: str
    speakers: list[str]
    language: str
    targets: list[str]
    engine: Literal["fast", "glossary"]
    start: datetime
    end: datetime
    abstract: str
    tags: list[str]
    glossary: list[GlossaryTerm]
    status: Literal["scheduled", "live", "done"]
    actual_start: datetime | None
    actual_end: datetime | None


@dataclass
class RoomStatus:
    state: Literal["green", "yellow", "red", "idle"]
    level_db: float
    latency_p50_s: float | None
    quality: float | None
    cost_usd: float
    talk_id: str | None
    detail: str
    gated_s: float = 0.0  # Task 19: cumulative seconds the silence gate has saved (not sent)
