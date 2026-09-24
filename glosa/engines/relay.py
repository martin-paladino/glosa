"""SessionRelay: one continuous caption stream on top of short engine sessions.

A Gemini Live (Translate) session lives about 10 minutes; talks last 45-60.
The relay opens the next session ahead of time and hands the audio over at a
natural pause, so the captions never stop. **No audio chunk is ever sent to
two sessions**, so there is nothing to deduplicate downstream (spec §3.3).
Session resumption is not used on purpose (reported quality loss in Live
Translate).

Measured in the 10-minute Live Translate run (T0.5,
samples/fixtures/lt_en.jsonl):

- GoAway arrived ~540 s after connect, with ``time_left`` 50 s.
- If the client keeps the session open, the server kills it with close code
  1008 at ~591 s ("client failed to close the connection after receiving a
  GoAway").
- Connecting takes ~0.45 s, and the first output arrives ~4.5 s after the
  audio starts.

Hence the defaults: standby at 510 s and forced switch at 570 s, 21 s before
the kill. Both are counted from the session's *connect*, because the
server's clock starts there.

Session lifecycle: connecting -> (standby ->) active -> draining -> closed.

Rotation
    1. ``standby_at`` s after the active session connected, the next one is
       opened in standby. It receives no audio.
    2. The audio moves to the standby at the first VAD ``pause`` after that,
       or right away if a pause is already under way when the standby is
       ready.
    3. The old session gets ``end_utterance()`` and is closed ``drain_s`` (5)
       s later. Its events are still forwarded while it drains.
    4. With no pause, the switch is forced at ``force_at``.

    A ``go_away`` from the active session starts the same rotation at once:
    standby now, switch at the next pause, forced at
    ``t_go_away + time_left - 15`` s or at ``force_at``, whichever comes
    first. With less than 20 s of notice it switches as soon as the standby
    is up.

Stall watchdog
    Voice that no output has followed for ``stall_timeout`` s (spec §6: "voz
    sin texto de salida durante más de 8 s") calls ``reconnect("stall")``.
    Voice is the real per-chunk activity the caller passes to
    ``feed(chunk, voiced=vad.in_speech)``. It is not inferred from VAD
    events, because EnergyVad emits no ``pause`` after an utterance shorter
    than 1.5 s. Output is any text from the active session. The rules
    (warm-up grace of ``first_output_grace_s`` = 15 s, the VAD's 400 ms tail,
    re-arming only on new voice after a reconnect) live in
    ``glosa.engines.watchdog.StallWatchdog``.

Connecting
    A connected engine is used, as the active session or as the standby,
    only after its first ``events()`` read has had a chance to run and
    reported no ``error``/``closed``. Both engines report a failed connect
    as the first thing ``events()`` yields, without waiting on the network:
    LiveTranslateEngine keeps the exception from ``connect()``, and
    FakeEngine replays an error at t=0. So a dead standby never retires a
    healthy session, and held-back audio is never flushed into a failed one.
    Until then the attempt still counts as the one connection in flight.
    A session that fails later, after a network round trip, is handled like
    any other death.

Failures (the engines never raise: they report ``error`` then ``closed``)
    - Retryable errors, a connect that takes longer than
      ``connect_timeout`` (reported as a retryable error with code 0), and a
      session that closes on its own are retried after 1, 2, 4, 8, 16, 16...
      s, plus up to ``jitter`` x that of random jitter. The count starts
      over once a session opened after the last failure produces output.
    - 402 / ``meta["payment"]`` sets ``payment_blocked`` and retries every 30
      s. The flag clears on the first output of a later session.
    - Non-retryable errors (400, bad config, ...) set ``halted``: no more
      automatic attempts until ``reconnect()`` (the admin's "Reconectar").
    - At most one connection attempt is ever in flight, confirmation
      included.
    - If the active session dies while a standby is ready, the standby takes
      over at once.
    - While no session can take audio, the last ``buffer_s`` s of chunks are
      held back, then sent to the next session. They were never sent
      anywhere else.
    - Every engine is ``close()``d when it is retired (after the drain), when
      its ``closed`` event is seen, and on ``stop()``.

Events
    ``events()`` merges the active and draining sessions' events:
    ``source_delta``/``target_delta``/``source_final``, plus ``go_away`` from
    the active session and ``error`` from any session. Each event carries
    ``meta["session"]``, the session's sequence number (1, 2, ...). Events
    that are not forwarded (a standby's output, the sessions' own
    ``closed``) keep their ``meta["usd"]`` increments: those ride on the next
    forwarded event, so summing ``usd`` over ``events()`` gives the full cost.
    The stream ends with exactly one ``closed``, after ``stop()``.

Stats
    ``stats["rotations"]`` counts planned hand-overs (age or GoAway).
    ``stats["reconnects"]`` counts unplanned ones: ``reconnect()`` calls
    (stall, manual) and deaths of the active session. A failed connect never
    becomes active (see Connecting), so it counts only in
    ``stats["errors"]``, which counts every error by code (0 = timeout,
    network, or unknown).

Timers
    The relay never sleeps. Every deadline (standby, force, drain, backoff,
    payment retry, connect timeout, watchdog) is checked against
    ``clock.now()`` on each ``feed()`` call, that is every ~100 ms of audio.
    A ``pause``, a ``go_away``, a session's death or a standby coming up can
    also switch sessions at once. Nothing else advances the timers. So if
    audio stops flowing, the deadlines wait for the next ``feed()``. The
    relay is deterministic under FakeClock and never moves a shared clock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import random
from collections import deque
from collections.abc import AsyncIterator, Coroutine
from typing import Any, Literal

from glosa.clock import Clock
from glosa.engines.base import Engine, EngineFactory
from glosa.engines.watchdog import StallWatchdog
from glosa.models import AudioChunk, EngineConfig, EngineEvent, VadEvent

log = logging.getLogger(__name__)

BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
PAYMENT_RETRY_S = 30.0
GO_AWAY_URGENT_S = 20.0  # less notice than this: switch as soon as possible
GO_AWAY_MARGIN_S = 15.0  # otherwise switch this long before the server's deadline
STOP_GRACE_S = 2.0  # stop(): how long to wait for the sessions' final events
# Loop turns a new session's first events() read gets to report a failed
# connect (FakeEngine needs 2: one sleep(0), then the yield).
_CONFIRM_TURNS = 5
_TEXT_KINDS = frozenset({"source_delta", "target_delta", "source_final"})

_Role = Literal["connecting", "standby", "active", "draining", "dead"]


@dataclasses.dataclass(eq=False)
class _Session:
    seq: int
    engine: Engine
    role: _Role = "connecting"
    connected_at: float = 0.0
    close_at: float | None = None  # while draining: when to close() it
    closing: bool = False
    saw_closed: bool = False
    send_error_logged: bool = False


def _error_event(t: float, code: int, retryable: bool, text: str) -> EngineEvent:
    return EngineEvent(kind="error", text=text, t_recv=t, meta={"code": code, "retryable": retryable})


async def _next_event(stream: AsyncIterator[EngineEvent]) -> EngineEvent | None:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


class SessionRelay:
    """Relay audio across successive engine sessions (see module docstring).

    Typical use, in a RoomWorker::

        relay = SessionRelay(factory, cfg, clock, **settings.relay.model_dump())
        await relay.start()
        # per 100 ms chunk, in stream order:
        for ev in vad.process(chunk):
            relay.on_vad(ev)
        await relay.feed(chunk, voiced=vad.in_speech)
        # elsewhere: async for ev in relay.events(): ...
        await relay.stop()

    ``payment_blocked`` and ``halted`` are for the room status (red).
    """

    def __init__(
        self,
        factory: EngineFactory,
        cfg: EngineConfig,
        clock: Clock,
        standby_at: float = 510.0,
        force_at: float = 570.0,
        stall_timeout: float = 8.0,
        *,
        first_output_grace_s: float = 15.0,
        connect_timeout: float = 10.0,
        drain_s: float = 5.0,
        buffer_s: float = 2.0,
        jitter: float = 0.25,
        rng: random.Random | None = None,
    ) -> None:
        if not 0 < standby_at <= force_at:
            raise ValueError(f"need 0 < standby_at <= force_at, got {standby_at} and {force_at}")
        self._factory = factory
        self._cfg = cfg
        self._clock = clock
        self.standby_at = standby_at
        self.force_at = force_at
        self.stall_timeout = stall_timeout
        self.first_output_grace_s = first_output_grace_s
        self.connect_timeout = connect_timeout
        self.drain_s = drain_s
        self.buffer_s = buffer_s
        self.jitter = jitter
        self._rng = rng if rng is not None else random.Random()

        self.stats: dict[str, Any] = {"rotations": 0, "reconnects": 0, "errors": {}}
        self.payment_blocked = False
        self.halted = False  # a non-retryable error: waiting for reconnect()

        self._started = False
        self._stopped = False
        self._seq = 0
        self._active: _Session | None = None
        self._standby: _Session | None = None
        self._connecting: _Session | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._connect_deadline = math.inf
        self._draining: list[_Session] = []
        self._pending: deque[AudioChunk] = deque()  # audio no session has taken yet

        self._next_attempt_at: float | None = None  # backoff / payment wait
        self._failures = 0  # consecutive, for the backoff
        self._last_failed_seq = 0

        self._rotation_due = False
        self._standby_deadline = math.inf
        self._force_deadline = math.inf

        self._in_pause = False
        self._watchdog = StallWatchdog(stall_timeout, first_output_grace_s)

        self._out: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._final: EngineEvent | None = None
        self._usd_carry = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pumps: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------ public API

    async def start(self) -> None:
        """Begin connecting the first session. Returns at once: audio fed
        before it is up is held (up to ``buffer_s``) and sent when it is."""
        if self._started or self._stopped:
            return
        self._started = True
        self._maybe_connect()

    async def feed(self, chunk: AudioChunk, voiced: bool = False) -> None:
        """Send one chunk to the active session (exactly one session, ever).

        ``voiced``: the VAD's ``in_speech`` for this chunk, for the watchdog.
        """
        if self._stopped:
            return
        if voiced:
            self._watchdog.on_voice(self._clock.now())
        await self._poll()
        self._pending.append(chunk)
        while self._pending and self._active is not None:
            await self._send(self._active, self._pending.popleft())
        if self._active is None:
            horizon = chunk.t - self.buffer_s
            while self._pending and self._pending[0].t < horizon:
                self._pending.popleft()

    def on_vad(self, ev: VadEvent) -> None:
        """Take a VAD event: a ``pause`` is where a rotation may switch.
        Its timing is read from the clock, not ev.t (ev.t is on the room's
        audio clock)."""
        if self._stopped:
            return
        if ev.kind == "speech_start":
            self._in_pause = False
        elif ev.kind == "pause":
            self._in_pause = True
            self._maybe_switch(self._clock.now())

    async def end_utterance(self) -> None:
        """Pass ``end_utterance()`` to the active session (the glossary
        engine's client-side VAD: call it on each VAD ``pause``). A no-op
        with no confirmed active session or after ``stop()``."""
        if self._stopped or self._active is None:
            return
        await self._end_utterance(self._active)

    async def reconnect(self, reason: str) -> None:
        """Replace the active session now ("stall", "manual", ...). A ready
        standby takes over at once. Otherwise the active one is retired and a
        new one connects, skipping any backoff wait (never a second attempt
        in flight). Also lifts ``halted``."""
        if self._stopped:
            return
        log.warning("relay: reconnect (%s)", reason)
        self.stats["reconnects"] += 1
        self.halted = False
        self._next_attempt_at = None
        old = self._active
        if self._standby is not None:
            self._promote_standby()
        elif old is not None:
            self._active = None
            self._watchdog.reset(self._clock.now())
            self._retire(old)
        self._maybe_connect()

    async def stop(self) -> None:
        """Close every session, let their last events through (up to
        STOP_GRACE_S), then end ``events()`` with one ``closed``."""
        if self._stopped:
            return
        self._stopped = True
        connect_task, self._connect_task = self._connect_task, None
        sessions = [s for s in (self._connecting, self._standby, self._active) if s is not None]
        sessions += self._draining
        self._connecting = self._standby = self._active = None
        self._draining = []
        for s in sessions:  # late output still counts; a `closed` now is expected
            s.role = "draining" if s.role in ("active", "draining") else "dead"
        if connect_task is not None:
            connect_task.cancel()
        await asyncio.gather(*(self._close_engine(s) for s in sessions))
        await self._wait_or_cancel(self._pumps)
        await self._wait_or_cancel(self._tasks)
        meta = {"usd": self._usd_carry} if self._usd_carry else {}
        self._usd_carry = 0.0
        self._final = EngineEvent(kind="closed", t_recv=self._clock.now(), meta=meta)
        self._out.put_nowait(self._final)

    async def events(self) -> AsyncIterator[EngineEvent]:
        """The merged event stream (single consumer). Ends after stop()."""
        while True:
            ev = await self._out.get()
            yield ev
            if ev is self._final:
                return

    # ------------------------------------------------------------ timers

    async def _poll(self) -> None:
        now = self._clock.now()
        if self._connecting is not None and now >= self._connect_deadline:
            self._abandon_connect(now)
        if self._active is not None:
            if now >= self._standby_deadline:
                self._rotation_due = True
            self._maybe_switch(now)
        for s in self._draining:
            if s.close_at is not None and now >= s.close_at:
                s.close_at = None
                self._spawn(self._close_engine(s), f"close-{s.seq}")
        self._maybe_connect(now)
        if self._active is not None and self._watchdog.stalled(now):
            await self.reconnect("stall")

    # ------------------------------------------------------------ switching

    def _maybe_switch(self, now: float) -> None:
        if self._stopped or self._active is None or self._standby is None or not self._rotation_due:
            return
        if self._in_pause or now >= self._force_deadline:
            log.info(
                "relay: rotation %d -> %d (%s)",
                self._active.seq,
                self._standby.seq,
                "pause" if self._in_pause else "forced",
            )
            self._promote_standby()
            self.stats["rotations"] += 1

    def _promote_standby(self) -> None:
        new, old = self._standby, self._active
        assert new is not None
        self._standby = None
        self._activate(new)
        if old is not None:
            self._retire(old)

    def _activate(self, s: _Session) -> None:
        s.role = "active"
        self._active = s
        self._rotation_due = False
        self._standby_deadline = s.connected_at + self.standby_at
        self._force_deadline = s.connected_at + self.force_at
        self._watchdog.reset(self._clock.now())

    def _retire(self, s: _Session) -> None:
        s.role = "draining"
        s.close_at = self._clock.now() + self.drain_s
        self._draining.append(s)
        self._spawn(self._end_utterance(s), f"end-utterance-{s.seq}")

    def _on_go_away(self, ev: EngineEvent) -> None:
        now = self._clock.now()
        left = float(ev.meta.get("time_left_s") or 0.0)
        deadline = now if left < GO_AWAY_URGENT_S else now + left - GO_AWAY_MARGIN_S
        log.info("relay: go_away with %.0f s left, switching by +%.0f s", left, deadline - now)
        self._force_deadline = min(self._force_deadline, deadline)
        self._rotation_due = True
        self._maybe_switch(now)
        self._maybe_connect(now)

    # ------------------------------------------------------------ connecting

    def _maybe_connect(self, now: float | None = None) -> None:
        if self._stopped or not self._started or self.halted or self._connecting is not None:
            return
        if not (self._active is None or (self._rotation_due and self._standby is None)):
            return
        now = self._clock.now() if now is None else now
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return
        self._next_attempt_at = None
        self._seq += 1
        try:
            engine = self._factory(self._cfg)
        except Exception as exc:  # a bad config: retrying won't help
            log.exception("relay: engine factory failed")
            self._report_error(self._seq, _error_event(now, 0, False, f"engine factory failed: {exc!r}"))
            self._last_failed_seq = self._seq
            self.halted = True
            return
        s = _Session(self._seq, engine)
        self._connecting = s
        self._connect_deadline = now + self.connect_timeout
        self._connect_task = self._spawn(self._run_connect(s), f"connect-{s.seq}")

    async def _run_connect(self, s: _Session) -> None:
        try:
            await s.engine.connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # engines report failures via events(); be safe anyway
            log.exception("relay: session %d connect() raised", s.seq)
            if self._connecting is s:
                self._connecting = None
                self._connect_task = None
            self._report_error(s.seq, _error_event(self._clock.now(), 0, True, f"connect() raised {exc!r}"))
            self._fail(s, retryable=True, payment=False)
            await self._close_engine(s)
            return
        if self._connecting is not s or self._stopped:
            await self._close_engine(s)
            return
        s.connected_at = self._clock.now()  # the server's clock starts here
        # Still `_connecting`: the pump confirms it (or reports its failure).
        self._spawn(self._pump(s), f"pump-{s.seq}", self._pumps)

    def _confirm(self, s: _Session) -> None:
        """The session's first read showed no failure: put it to use."""
        if self._connecting is not s or self._stopped:
            return  # abandoned (timeout) or stopping
        self._connecting = None
        self._connect_task = None
        now = self._clock.now()
        if self._active is None:
            self._activate(s)
        elif self._standby is None:
            s.role = "standby"
            self._standby = s
        else:  # nothing needs it any more
            s.role = "dead"
            self._spawn(self._close_engine(s), f"close-{s.seq}")
            return
        self._maybe_switch(now)

    def _abandon_connect(self, now: float) -> None:
        s, task = self._connecting, self._connect_task
        assert s is not None
        self._connecting = None
        self._connect_task = None
        if task is not None:
            task.cancel()
        text = f"connect timed out after {self.connect_timeout:g} s"
        self._report_error(s.seq, _error_event(now, 0, True, text))
        self._fail(s, retryable=True, payment=False)
        self._spawn(self._close_engine(s), f"close-{s.seq}")

    def _fail(self, s: _Session, *, retryable: bool, payment: bool) -> None:
        """A session that was (or was about to be) in use is gone: schedule
        the next attempt, and hand the audio to the standby if there is one."""
        role, s.role = s.role, "dead"
        if self._connecting is s:  # failed while being confirmed
            self._connecting = None
            self._connect_task = None
        if self._stopped or role in ("draining", "dead"):
            return
        now = self._clock.now()
        self._failures += 1
        self._last_failed_seq = max(self._last_failed_seq, s.seq)
        if payment:
            self.payment_blocked = True
            self._next_attempt_at = now + PAYMENT_RETRY_S
        elif retryable:
            base = BACKOFF_S[min(self._failures, len(BACKOFF_S)) - 1]
            self._next_attempt_at = now + base * (1.0 + self.jitter * self._rng.random())
        else:
            self.halted = True
            self._next_attempt_at = None
        if role == "active":
            self._active = None
            self.stats["reconnects"] += 1
            if self._standby is not None:
                self._promote_standby()
        elif role == "standby":
            self._standby = None
        self._maybe_connect(now)

    # ------------------------------------------------------------ events

    async def _pump(self, s: _Session) -> None:
        stream = s.engine.events()
        first = asyncio.get_running_loop().create_task(_next_event(stream), name=f"relay-first-{s.seq}")
        try:
            # A failed connect shows up on the first read, at once: give it
            # a few loop turns before putting the session to use.
            for _ in range(_CONFIRM_TURNS):
                if first.done():
                    break
                await asyncio.sleep(0)
            if not first.done() or (
                first.exception() is None
                and first.result() is not None
                and first.result().kind not in ("error", "closed")  # type: ignore[union-attr]
            ):
                self._confirm(s)
            ev = await first
            if ev is not None:
                self._on_event(s, ev)
                async for ev in stream:
                    self._on_event(s, ev)
            if not s.saw_closed:  # the stream must end with `closed`; act as if it did
                self._on_event(s, EngineEvent(kind="closed", t_recv=self._clock.now()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("relay: session %d events() raised", s.seq)
            if not s.saw_closed:
                now = self._clock.now()
                self._on_event(s, _error_event(now, 0, True, f"events() raised {exc!r}"))
                self._on_event(s, EngineEvent(kind="closed", t_recv=now))
        finally:
            if not first.done():
                first.cancel()
        await self._close_engine(s)

    def _on_event(self, s: _Session, ev: EngineEvent) -> None:
        if ev.kind == "closed":
            s.saw_closed = True
            self._carry(ev)
            if s in self._draining:
                self._draining.remove(s)
            if s.role in ("connecting", "active", "standby"):
                log.warning("relay: session %d closed unexpectedly", s.seq)
                self._fail(s, retryable=True, payment=False)
            s.role = "dead"
            return
        if ev.kind == "error":
            self._report_error(s.seq, ev)
            payment = bool(ev.meta.get("payment")) or ev.meta.get("code") == 402
            self._fail(s, retryable=bool(ev.meta.get("retryable", True)), payment=payment)
            return
        if ev.kind == "go_away" and s.role == "active":
            self._surface(s.seq, ev)
            self._on_go_away(ev)
            return
        if ev.kind in _TEXT_KINDS and s.role in ("active", "draining"):
            if s.role == "active":
                self._watchdog.on_output(self._clock.now())
            if s.seq > self._last_failed_seq:  # a session opened after the last failure works
                self._failures = 0
                self.payment_blocked = False
            self._surface(s.seq, ev)
            return
        self._carry(ev)  # e.g. a standby's output: it has no audio, so it is not real

    def _report_error(self, seq: int, ev: EngineEvent) -> None:
        code = int(ev.meta.get("code") or 0)
        errors: dict[int, int] = self.stats["errors"]
        errors[code] = errors.get(code, 0) + 1
        log.warning("relay: session %d error %d: %s", seq, code, ev.text)
        self._surface(seq, ev)

    def _surface(self, seq: int, ev: EngineEvent) -> None:
        meta = dict(ev.meta)
        meta["session"] = seq
        if self._usd_carry:
            meta["usd"] = float(meta.get("usd") or 0.0) + self._usd_carry
            self._usd_carry = 0.0
        self._out.put_nowait(dataclasses.replace(ev, meta=meta))

    def _carry(self, ev: EngineEvent) -> None:
        usd = ev.meta.get("usd")
        if usd:
            self._usd_carry += float(usd)

    # ------------------------------------------------------------ engine calls

    async def _send(self, s: _Session, chunk: AudioChunk) -> None:
        try:
            await s.engine.send_audio(chunk)
        except Exception:  # its events() will say why the session is gone
            if not s.send_error_logged:  # once per session, not every 100 ms
                s.send_error_logged = True
                log.exception("relay: session %d send_audio failed", s.seq)

    async def _end_utterance(self, s: _Session) -> None:
        try:
            await s.engine.end_utterance()
        except Exception:
            log.exception("relay: session %d end_utterance failed", s.seq)

    async def _close_engine(self, s: _Session) -> None:
        if s.closing:
            return
        s.closing = True
        try:
            await s.engine.close()
        except Exception:
            log.exception("relay: session %d close failed", s.seq)

    # ------------------------------------------------------------ tasks

    def _spawn(
        self,
        coro: Coroutine[Any, Any, None],
        name: str,
        bucket: set[asyncio.Task[Any]] | None = None,
    ) -> asyncio.Task[None]:
        tasks = self._tasks if bucket is None else bucket
        task = asyncio.get_running_loop().create_task(coro, name=f"relay-{name}")
        tasks.add(task)
        task.add_done_callback(lambda t: self._task_done(t, tasks))
        return task

    @staticmethod
    def _task_done(task: asyncio.Task[Any], tasks: set[asyncio.Task[Any]]) -> None:
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("relay: task %s failed", task.get_name(), exc_info=task.exception())

    @staticmethod
    async def _wait_or_cancel(tasks: set[asyncio.Task[Any]]) -> None:
        current = asyncio.current_task()
        pending = {t for t in tasks if not t.done() and t is not current}
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=STOP_GRACE_S)
        for t in late:
            t.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
