"""FakeEngine: replays a recorded engine session (JSONL) with its original
timing, so everything downstream of the engine can be tested without the API.

The recording format is the one scripts/verify_live.py writes (T0.5), one JSON
object per line:

    {"t": 4.504, "kind": "source_delta", "text": "Great starting", "raw_type": "...",
     "meta": {"lang": "en", "finished": null}}

- ``t`` is seconds since the session connected; each record is emitted when
  ``clock.now() - <time of connect()>`` reaches it (FakeClock.sleep just
  advances time, so tests run instantly and deterministically).
- ``source_delta`` / ``target_delta`` / ``source_final`` pass through
  (``target_final`` becomes ``target_delta``: EngineEvent has no target_final);
  ``go_away`` takes ``meta.time_left_s`` or parses ``meta.time_left_raw``
  ("50s"); ``error`` takes ``meta.code`` / ``meta.retryable`` (/ ``payment``)
  and ends the session, as a real error does. Anything else (usage,
  session_resumption_update, ...) is skipped.
- The stream always ends with exactly one ``closed`` event (end of file, a
  ``closed`` record, an ``error`` record, or close()). A recording whose
  first record is an error at t=0 simulates a failed connect.

``fail_after_s`` simulates a hung session: records later than that are never
emitted and the stream goes silent (it neither emits nor ends) until close().
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from glosa.clock import Clock
from glosa.models import AudioChunk, EngineConfig, EngineEvent

_TEXT_KINDS = {"source_delta", "target_delta", "source_final"}
_KIND_ALIASES = {"target_final": "target_delta"}


class FakeEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        clock: Clock,
        fail_after_s: float | None = None,
    ) -> None:
        if not cfg.fixture_path:
            raise ValueError("FakeEngine needs cfg.fixture_path (a JSONL recording)")
        self.cfg = cfg
        self._clock = clock
        self._fail_after_s = fail_after_s
        self._records = _load_jsonl(Path(cfg.fixture_path))
        self._t0: float | None = None
        self._closed = asyncio.Event()

    async def connect(self) -> None:
        self._t0 = self._clock.now()

    async def send_audio(self, chunk: AudioChunk) -> None:
        return None

    async def end_utterance(self) -> None:
        return None

    async def close(self) -> None:
        self._closed.set()

    async def events(self) -> AsyncIterator[EngineEvent]:
        if self._t0 is None:
            raise RuntimeError("FakeEngine.events() called before connect()")
        for rec in self._records:
            t = float(rec["t"])
            if self._fail_after_s is not None and t > self._fail_after_s:
                await self._closed.wait()  # simulated hang: silent until close()
            if self._closed.is_set():
                break
            delay = self._t0 + t - self._clock.now()
            if delay > 0:
                await self._clock.sleep(delay)
            await asyncio.sleep(0)  # FakeClock.sleep never suspends: let other tasks run
            if self._closed.is_set():
                break
            event = self._to_event(rec)
            if event is None:
                continue
            yield event
            if event.kind == "closed":
                return
            if event.kind == "error":  # an error ends the session, as in the real engine
                break
        yield EngineEvent(kind="closed", t_recv=self._clock.now())

    def _to_event(self, rec: dict) -> EngineEvent | None:
        kind = _KIND_ALIASES.get(rec["kind"], rec["kind"])
        meta = rec.get("meta") or {}
        now = self._clock.now()
        if kind in _TEXT_KINDS:
            default_lang = self.cfg.target_lang if kind == "target_delta" else self.cfg.source_lang
            return EngineEvent(
                kind=kind,
                text=rec.get("text", ""),
                lang=meta.get("lang") or default_lang,
                t_recv=now,
            )
        if kind == "go_away":
            return EngineEvent(kind="go_away", t_recv=now, meta={"time_left_s": _time_left_s(meta)})
        if kind == "error":
            err = {"code": int(meta.get("code") or 0), "retryable": bool(meta.get("retryable", True))}
            if meta.get("payment"):
                err["payment"] = True
            return EngineEvent(kind="error", text=rec.get("text", ""), t_recv=now, meta=err)
        if kind == "closed":
            return EngineEvent(kind="closed", t_recv=now)
        return None


def _load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _time_left_s(meta: dict) -> float:
    if meta.get("time_left_s") is not None:
        return float(meta["time_left_s"])
    raw = meta.get("time_left_raw")  # protobuf Duration as JSON, e.g. "50s"
    return float(str(raw).rstrip("s")) if raw else 0.0
