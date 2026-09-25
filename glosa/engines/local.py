"""LocalParakeetEngine: Task 16's 100%-local speech-to-text engine
(``engine_mode: local``) -- Parakeet (``mlx-community/parakeet-tdt-0.6b-v3``)
via the ``parakeet-mlx`` package, run in-process on Apple silicon. No cloud
API, no API key, ``meta["usd"]`` is always 0.

Same event shapes as the "glossary" engine, transcribe-live
(glosa/engines/transcribe.py): ``source_delta`` interims (``meta={"interim":
True}``, the OPEN SEGMENT'S WHOLE TEXT so far -- it replaces, not appends),
``source_final`` closing the segment, exactly one ``closed`` at the end.
``glosa/room.py``'s hybrid VAD calls ``end_utterance()`` on every VAD
"pause" for a "glossary"-kind engine (``_feed``) -- this engine treats that
as the boundary of one utterance, matching transcribe-live's contract
closely enough for the rest of the pipeline (LivePipeline, segmenter, lane)
to work unchanged. Unlike transcribe-live, there is no "stale repeated
text" problem to correct for (we own the whole transcription, not a remote
server's own segmentation), so there is no ``_unstale``-style logic here --
only a generation counter so a slow interim's result is dropped if
``end_utterance()`` has already closed that segment (see ``_generation``).

Streaming vs. rolling window: parakeet-mlx does offer a true streaming
decoder (``BaseParakeet.transcribe_stream`` -> ``StreamingParakeet``, with
``add_audio()``/``.result``), but it mutates the shared model's encoder
attention mode for as long as it is open and keeps its own KV cache per
open utterance -- correct to share across N concurrently-open utterances on
one process-wide model only with careful lifecycle bookkeeping. Given the
measured batch speed (~30x real time on the M4 build machine, confirmed
2026-09-25: ~0.2-0.25s to transcribe a 6s clip once warmed up -- see the
task brief and task-16-report.md), this engine instead re-transcribes the
ROLLING WINDOW of the open utterance's buffered audio with the model's
plain batch decode path at most every ``INTERIM_PERIOD_S`` seconds, and
once more (the whole buffered utterance) on ``end_utterance()``. Simpler,
far less code to get right in a hackathon, and fast enough at Task 16's own
target scale (1-2 rooms per machine).

Lazy import: this module must IMPORT cleanly without mlx/parakeet-mlx
installed (Linux/Docker installs never have them -- pyproject's "local"
extra is Apple-silicon-only, see ``sys_platform``/``platform_machine``
markers). Building a real engine (no ``transcribe=`` fake injected) without
the extra raises ``ConfigError`` immediately, at construction -- not
lazily, not just when it is finally used.

One shared model instance per process: loading the model is expensive
(~90s cold on the M4, ~2.3 GB of weights) and MLX/Metal calls are not meant
to run concurrently from multiple Python threads at once, so multiple
rooms' ``LocalParakeetEngine`` instances (each its own session, like every
other engine) share ONE process-wide ``_SharedParakeetModel``: one
``asyncio.Lock`` serializes every call into it (a queue of one), and the
model itself loads lazily -- once -- on the first real call.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import AsyncIterator, Callable

from glosa.clock import Clock
from glosa.config import ConfigError
from glosa.models import AudioChunk, EngineConfig, EngineEvent

log = logging.getLogger(__name__)

PARAKEET_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"
INTERIM_PERIOD_S = 1.0  # re-transcribe the open utterance's buffer at most this often

# Blocking; PCM16 mono 16 kHz bytes -> text. Always called off the event
# loop (asyncio.to_thread), one call in flight at a time (_SharedParakeetModel's lock).
TranscribeFn = Callable[[bytes], str]

_shared_model: "_SharedParakeetModel | None" = None
_shared_model_lock = asyncio.Lock()  # guards creating/replacing the module-level singleton


def _check_available() -> None:
    """Cheap availability check (import only, no weights): building a real
    LocalParakeetEngine (no ``transcribe=`` fake) without the ``local``
    extra installed fails now, with a clear message, not on first use."""
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError as exc:
        raise ConfigError(
            "engine_mode 'local' needs the optional 'local' extra (parakeet-mlx, mlx-lm; "
            "Apple silicon only): install with `uv sync --extra local`"
        ) from exc


def _load_transcribe_fn() -> TranscribeFn:
    """The real transcribe function, backed by one loaded parakeet-mlx
    model. Blocking (model load + first-call MLX warmup can take
    ~1-2 minutes); always called via ``asyncio.to_thread``."""
    from parakeet_mlx import from_pretrained
    from parakeet_mlx.audio import get_logmel
    from parakeet_mlx.parakeet import DecodingConfig
    import mlx.core as mx
    import numpy as np

    model = from_pretrained(PARAKEET_MODEL_ID)
    sample_rate = model.preprocessor_config.sample_rate
    if sample_rate != 16000:  # glosa's audio pipeline is PCM16 mono 16 kHz throughout
        raise ConfigError(f"{PARAKEET_MODEL_ID}: expected a 16 kHz model, got {sample_rate} Hz")

    def transcribe(pcm: bytes) -> str:
        if not pcm:
            return ""
        audio = mx.array(np.frombuffer(pcm, dtype=np.int16)).astype(mx.float32) / 32768.0
        mel = get_logmel(audio, model.preprocessor_config)
        result = model.generate(mel, decoding_config=DecodingConfig())[0]
        return result.text.strip()

    return transcribe


class _SharedParakeetModel:
    """Process-wide: the one loaded model (or an injected fake) and the one
    lock serializing every call into it.

    MLX's compute streams are thread-local as of mlx 0.31+ (confirmed
    2026-09-25 against this repo's own pinned version, and a known issue
    across the MLX ecosystem -- mlx-lm #1181/#1256, mlx-vlm #1049, among
    others): a stream created on one OS thread cannot be used from another,
    so ``asyncio.to_thread`` (which borrows worker threads from a shared,
    unbounded pool -- a different one on every call) crashes with
    "RuntimeError: There is no Stream(cpu, 1) in current thread" the moment
    two calls land on different threads. The fix used here (also the
    community's documented workaround): one dedicated single-worker
    ``ThreadPoolExecutor``, so the model is BOTH loaded AND ever after
    called from the exact same OS thread for the whole process's life."""

    def __init__(self, transcribe_fn: TranscribeFn | None) -> None:
        self._transcribe_fn = transcribe_fn  # None: real model, loaded lazily on first use
        self._call_lock = asyncio.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet-mlx")
        # Test-observable: how many transcribe() calls were inside the lock
        # at once (must never exceed 1 -- see tests/engines/test_local.py).
        self.calls_in_flight = 0
        self.max_calls_in_flight = 0

    async def transcribe(self, pcm: bytes) -> str:
        async with self._call_lock:
            self.calls_in_flight += 1
            self.max_calls_in_flight = max(self.max_calls_in_flight, self.calls_in_flight)
            loop = asyncio.get_running_loop()
            try:
                if self._transcribe_fn is None:
                    self._transcribe_fn = await loop.run_in_executor(self._executor, _load_transcribe_fn)
                return await loop.run_in_executor(self._executor, self._transcribe_fn, pcm)
            finally:
                self.calls_in_flight -= 1


async def _get_shared_model(transcribe_fn: TranscribeFn | None) -> _SharedParakeetModel:
    """The process-wide ``_SharedParakeetModel``, created on first call.
    ``transcribe_fn`` only matters for that first call (real code always
    passes None; tests that inject a fake are expected to
    ``reset_shared_model()`` first so their fake is the one that sticks)."""
    global _shared_model
    async with _shared_model_lock:
        if _shared_model is None:
            _shared_model = _SharedParakeetModel(transcribe_fn)
        return _shared_model


def reset_shared_model() -> None:
    """Test-only: drop the process-wide singleton so the next engine built
    starts fresh (each test gets its own injected fake)."""
    global _shared_model
    model, _shared_model = _shared_model, None
    if model is not None:
        model._executor.shutdown(wait=False, cancel_futures=True)


async def shutdown_shared_model(timeout: float) -> None:
    """App shutdown (glosa/web/app.py's lifespan; task-16-review.md
    Important #1): stop the shared MLX thread, waiting at most ``timeout``
    s. A call still in flight (a cold model load can take ~90 s) is not
    waited for past that: logged, then left to the interpreter's exit."""
    global _shared_model
    model, _shared_model = _shared_model, None
    if model is not None:
        await shutdown_executor(model._executor, timeout, "parakeet-mlx")


async def shutdown_executor(executor: concurrent.futures.ThreadPoolExecutor, timeout: float, name: str) -> None:
    """``executor.shutdown`` (queued calls cancelled) without blocking the
    event loop, waiting up to ``timeout`` s for its thread(s) to finish."""
    executor.shutdown(wait=False, cancel_futures=True)
    threads = list(getattr(executor, "_threads", ()))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while any(t.is_alive() for t in threads):
        if loop.time() >= deadline:
            log.warning("%s: a call is still running after %s s: not waiting for it", name, timeout)
            return
        await asyncio.sleep(0.02)


class LocalParakeetEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        clock: Clock,
        *,
        transcribe: TranscribeFn | None = None,
        interim_period_s: float = INTERIM_PERIOD_S,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self._injected_transcribe = transcribe
        if transcribe is None:
            _check_available()  # fail now, synchronously -- not on first real use
        self._interim_period_s = interim_period_s
        self._model: _SharedParakeetModel | None = None
        self._buf = bytearray()  # the open utterance's PCM so far
        self._open_text: str | None = None
        self._last_interim_at: float | None = None
        self._generation = 0  # bumped by end_utterance(): drops a stale interim result
        self._interim_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._closing = False
        self._ended = False

    async def connect(self) -> None:
        self._model = await _get_shared_model(self._injected_transcribe)

    def _can_send(self) -> bool:
        return not self._closing and not self._ended

    async def send_audio(self, chunk: AudioChunk) -> None:
        if not self._can_send():
            return
        self._buf += chunk.pcm
        now = self._clock.now()
        if self._last_interim_at is None or now - self._last_interim_at >= self._interim_period_s:
            self._last_interim_at = now
            self._maybe_interim()

    def _maybe_interim(self) -> None:
        if not self._buf or (self._interim_task is not None and not self._interim_task.done()):
            return  # one interim transcribe in flight at a time; a slow one is skipped, not queued
        pcm = bytes(self._buf)
        self._interim_task = self._spawn(self._run_interim(pcm, self._generation))

    async def _run_interim(self, pcm: bytes, generation: int) -> None:
        assert self._model is not None
        try:
            text = await self._model.transcribe(pcm)
        except Exception:
            log.exception("local: interim transcribe failed (dropped; the next interim/final may recover)")
            return
        if self._closing or self._ended or generation != self._generation:
            return  # end_utterance() already closed this segment: a late interim would reopen it
        if text and text != self._open_text:
            self._open_text = text
            await self._queue.put(
                EngineEvent(
                    kind="source_delta", text=text, lang=self.cfg.source_lang,
                    t_recv=self._clock.now(), meta={"interim": True, "usd": 0.0},
                )
            )

    async def end_utterance(self) -> None:
        if not self._can_send():
            return
        pcm = bytes(self._buf)
        self._buf.clear()
        self._open_text = None
        self._generation += 1
        generation = self._generation
        if self._interim_task is not None and not self._interim_task.done():
            self._interim_task.cancel()
        if not pcm:
            return  # nothing spoken since the last final: no event (unlike transcribe-live's empty final)
        self._spawn(self._run_final(pcm, generation))

    async def _run_final(self, pcm: bytes, generation: int) -> None:
        assert self._model is not None
        try:
            text = await self._model.transcribe(pcm)
        except Exception:
            log.exception("local: final transcribe failed; closing the segment empty")
            text = ""
        if self._closing or self._ended:
            return
        await self._queue.put(
            EngineEvent(
                kind="source_final", text=text, lang=self.cfg.source_lang,
                t_recv=self._clock.now(), meta={"usd": 0.0},
            )
        )

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for task in list(self._bg_tasks):
            task.cancel()
        await self._queue.put(EngineEvent(kind="closed", t_recv=self._clock.now(), meta={"usd": 0.0}))

    async def events(self) -> AsyncIterator[EngineEvent]:
        while True:
            event = await self._queue.get()
            yield event
            if event.kind == "closed":
                self._ended = True
                return

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task
