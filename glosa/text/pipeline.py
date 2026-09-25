"""LivePipeline: live source text -> P1 segments -> per-target translations,
delivered in strict segment order per target.

It serves the two cases where Glosa translates text rather than audio:

- the "glossary" engine: transcribe-live sends ``interim`` texts (the WHOLE
  open utterance so far, rewriting it every ~0.5 s) and a ``final`` that
  closes it; every segment is translated to every target;
- the "fast" engine with extra targets: Live Translate sends source
  ``delta`` texts that are appended; the caller closes the utterance with
  ``final(None)`` on a VAD pause.

Committed prefix (the P1 heuristic, measured before the vibeathon at
3.4/4.9 s): the pipeline keeps the open utterance's text plus how much of it
is already "committed", i.e. cut into a segment and sent to translation.
Committed text is never translated again, even if a later interim rewrites
it (that small divergence is accepted); if an interim shrinks below the
committed prefix nothing happens until more text (or the final) arrives. The
committed position is counted in words, since interims rewrite punctuation
and casing but rarely the word count. It also records how much of the next
word is committed, because the Segmenter can cut inside a token ("3.5" at
the ".") and a time cut can land between two deltas of one word
("Kuber" + "netes"); the rest of such a word goes on to the next segment,
unless it is only punctuation (the model adding a comma to a word that was
already cut), which is dropped.

Each segment carries ``t_start``/``t_end`` in the caller's ``t``: when its
text started arriving and when it was cut. Several cuts made by one call (a
final with three sentences) share that call's span in proportion to their
length, so their spans follow each other without overlapping.

Segmenting: on every interim/delta/tick/final the uncommitted tail is run
through a FRESH Segmenter (from ``segmenter_factory``) fed with the time the
open segment started, so rewrites of uncommitted words are honoured (e.g. a
comma that appears later) and ``tick`` cuts 3.0 s after the segment opened,
exactly as a single Segmenter fed append-only text would. Segments that are
only punctuation (the Segmenter cuts "..." into "." pieces) are dropped.

Translation: each segment gets the next ``index`` (0, 1, 2... for the life of
the pipeline) and one job per target, with the ``context_n`` previous SOURCE
segments as context, paired with THEIR translation into that same target once
it is done -- built at request time (when a worker is about to call
``translate``, not when the segment was emitted) from whichever of those
translations have already completed; a still-running one is never waited
for, so the pairing may be ``(source, None)``. Jobs run FIFO on
``max_inflight`` worker tasks, so at most that many translations are in
flight across all targets. A failure (an
exception, no answer within ``translate_timeout_s``, or an empty
translation) yields ``text=None`` and does not hold back the next segment.
A job still queued ``max_age_s`` (8 s) after its cut is not sent at all: it
yields ``text=None`` too (a caption that late is no use live, and sending it
would only make the ones behind it later). Every job gets its result
recorded, whatever happens to its translation, so a target's order never
waits for an index that will not come. A per-target reorder buffer calls
``on_segment`` (sync or async) strictly in index order for each target, one
call at a time per target; keep it quick, it runs on a translation worker. ``latency_s`` is ``clock.now()`` from the cut to
the answer, so it includes the time spent queued.

The public methods ``interim/delta/final/tick`` are synchronous and cheap:
they only segment and enqueue. The work runs on the pipeline's own named
tasks ("glosa-pipeline-worker-N"), started lazily from the running loop;
``drain()`` flushes and waits for everything, ``aclose()`` cancels it all.
Not thread-safe; call everything from the event loop that runs the
pipeline, and never ``drain()`` from inside ``on_segment``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import statistics
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from glosa.clock import Clock
from glosa.models import GlossaryTerm
from glosa.text.segmenter import Segmenter
from glosa.text.translator import Translation

log = logging.getLogger(__name__)

TranslateFn = Callable[[str, str, list[GlossaryTerm], list[tuple[str, str | None]]], Awaitable[Translation]]

# Per-target cache of recently completed translations, used to give the next
# segment's context its predecessors' translations once they land (never
# waited for, see _run). Far larger than context_n ever needs; it just bounds
# memory over a long-running talk.
_CONTEXT_CACHE_SIZE = 32


@dataclass
class TranslatedSegment:
    target: str  # target language
    index: int  # order of the source segment (0, 1, 2...) within the run
    source: str  # source text of the segment
    text: str | None  # translation (None if it failed: the caller decides, e.g. not to publish)
    latency_s: float  # from when the segment was cut until the translation arrived
    usd: float  # spent on it, also when it failed (an empty answer is still billed)
    t_start: float = 0.0  # caller's t: when the segment's text started arriving
    t_end: float = 0.0  # caller's t: when it was cut


@dataclass
class _Job:
    index: int
    target: str
    source: str
    context_sources: list[tuple[int, str]]  # (index, source) of the context_n segments before this one
    cut_at: float  # clock.now() when the segment was cut
    t_start: float  # caller's t, see TranslatedSegment
    t_end: float


def _has_alnum(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


class LivePipeline:
    def __init__(
        self,
        *,
        targets: list[str],
        translate: TranslateFn,
        glossary: list[GlossaryTerm],
        clock: Clock,
        segmenter_factory: Callable[[], Segmenter],
        on_segment: Callable[[TranslatedSegment], Awaitable[None] | None],
        max_inflight: int = 4,
        context_n: int = 2,
        translate_timeout_s: float | None = 10.0,
        max_age_s: float | None = 8.0,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        if context_n < 0:
            raise ValueError("context_n must be >= 0")
        self._targets = list(dict.fromkeys(targets))
        self._translate = translate
        self._glossary = glossary  # passed as is to every translate() call
        self._clock = clock
        self._segmenter_factory = segmenter_factory
        self._on_segment = on_segment
        self._max_inflight = max_inflight
        self._translate_timeout_s = translate_timeout_s
        self._max_age_s = max_age_s

        # open utterance
        self._text = ""  # its whole text so far (latest interim, or the deltas appended)
        self._k = 0  # index of the first word that is not fully committed
        self._partial = ""  # committed prefix of word _k ("" = none of it)
        self._open_since: float | None = None  # caller's t when the open segment started
        self._last_t = 0.0

        # segments and translation jobs
        self._context_n = context_n
        self._next_index = 0
        self._recent_sources: deque[tuple[int, str]] = deque(maxlen=context_n)
        self._recent_translations: dict[str, dict[int, str]] = {t: {} for t in self._targets}
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._closed = False

        # per-target reorder buffers
        self._ready: dict[str, dict[int, TranslatedSegment]] = {t: {} for t in self._targets}
        self._next_out: dict[str, int] = {t: 0 for t in self._targets}
        self._deliver_locks: dict[str, asyncio.Lock] = {t: asyncio.Lock() for t in self._targets}

        # stats
        self._segments = 0
        self._translated = 0
        self._failed = 0
        self._dropped = 0
        self._inflight = 0
        self._usd_total = 0.0
        self._latencies: list[float] = []

    # --- source text (sync: segment and enqueue, never block) -------------------

    def interim(self, text: str, t: float) -> None:
        """The ACCUMULATED text of the open utterance (replaces the previous one)."""
        if self._closed:
            return
        self._text = text
        self._advance(t)

    def delta(self, text: str, t: float) -> None:
        """Text APPENDED to the open utterance (Live Translate)."""
        if self._closed:
            return
        self._text += text
        self._advance(t)

    def final(self, text: str | None, t: float) -> None:
        """Close the open utterance: ``text`` is its final text, or None to use
        what there is. An empty final ("" : transcribe-live decided it was not
        speech) commits nothing more. The next interim starts a new utterance.
        """
        if self._closed:
            return
        if text is not None:
            self._text = text
        self._advance(t, flush=True)
        self._reset_utterance()

    def tick(self, t: float) -> None:
        """Call periodically: lets the Segmenter cut the open segment by time."""
        if self._closed:
            return
        self._advance(t, tick=True)

    # --- lifecycle ----------------------------------------------------------------

    async def drain(self, timeout_s: float = 10.0) -> None:
        """Flush the open utterance and wait (up to timeout_s) until every
        pending translation has been delivered; then stop the workers (the
        pipeline stays usable). On timeout, whatever is left is cancelled and
        the pipeline is closed, as in aclose().
        """
        if self._closed:
            return
        self.final(None, self._last_t)
        self._ensure_workers()
        try:
            async with asyncio.timeout(timeout_s):
                await self._queue.join()
        except TimeoutError:
            log.warning(
                "pipeline drain timed out after %.1fs: %d translation(s) in flight, %d queued; cancelling",
                timeout_s,
                self._inflight,
                self._queue.qsize(),
            )
            await self.aclose()
            return
        await self._stop_workers()

    async def aclose(self) -> None:
        """Cancel everything without waiting for pending translations. Idempotent."""
        self._closed = True
        self._reset_utterance()
        while True:  # discard queued jobs (and release anyone blocked in join())
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
        await self._stop_workers()
        for ready in self._ready.values():
            ready.clear()

    @property
    def stats(self) -> dict:
        """segments: source segments cut; translated/failed: per (segment, target)
        translation; dropped: the failed ones never sent (older than max_age_s);
        inflight: translations running now; queued: waiting for a worker;
        usd_total (failed ones included); latencies_s of the successful
        translations and their p50.
        """
        return {
            "segments": self._segments,
            "translated": self._translated,
            "failed": self._failed,
            "dropped": self._dropped,
            "inflight": self._inflight,
            "queued": self._queue.qsize(),
            "usd_total": self._usd_total,
            "latency_p50_s": statistics.median(self._latencies) if self._latencies else None,
            "latencies_s": list(self._latencies),
        }

    # --- segmenting -------------------------------------------------------------------

    def _reset_utterance(self) -> None:
        self._text = ""
        self._k = 0
        self._partial = ""
        self._open_since = None

    def _tail_pieces(self, words: list[str]) -> list[tuple[int, int]]:
        """The uncommitted tail as (word index, start char) pieces."""
        start = self._k
        pieces: list[tuple[int, int]] = []
        if self._partial:
            start += 1
            if self._k < len(words):
                word = words[self._k]
                rest = word[len(self._partial) :]
                # the rest of a word cut in the middle goes on; a rewritten
                # word, or punctuation added to a committed word, does not
                if word.startswith(self._partial) and _has_alnum(rest):
                    pieces.append((self._k, len(self._partial)))
        pieces.extend((i, 0) for i in range(start, len(words)))
        return pieces

    def _advance(self, t: float, *, tick: bool = False, flush: bool = False) -> None:
        self._last_t = t
        words = self._text.split()
        pieces = self._tail_pieces(words)
        if not pieces:  # nothing beyond the committed prefix (or a shrink below it)
            self._open_since = None
            return
        tokens = [words[i][s:] for i, s in pieces]
        tail = " ".join(tokens)
        opened = self._open_since if self._open_since is not None else t

        segmenter = self._segmenter_factory()
        cuts = segmenter.feed(tail, opened)
        if tick:
            cuts += segmenter.tick(t)
        if flush:
            cuts += segmenter.flush()
        if not cuts:
            self._open_since = opened
            return

        end = 0  # chars of `tail` consumed by the cuts
        total = sum(len(cut) for cut in cuts)
        done = 0
        for cut in cuts:
            end = tail.index(cut, end) + len(cut)
            start_t = opened + (t - opened) * done / total
            done += len(cut)
            if _has_alnum(cut):
                self._emit(cut, start_t, opened + (t - opened) * done / total)

        # map `end` back to a committed position in the utterance's words
        offset = 0
        for n, ((wi, start), token) in enumerate(zip(pieces, tokens)):
            if end <= offset + len(token):
                word = words[wi]
                consumed = start + (end - offset)
                at_tail_end = n == len(tokens) - 1 and end == offset + len(token)
                if consumed == len(word) and not at_tail_end:
                    # whole word, followed by a space: fully committed
                    self._k, self._partial = wi + 1, ""
                else:
                    # cut inside the word, or at the very end of the tail (the
                    # word may still grow: "Kuber" + "netes", "3." + "5")
                    self._k, self._partial = wi, word[:consumed]
                self._open_since = None if at_tail_end else t
                return
            offset += len(token) + 1
        raise AssertionError("segment cuts ran past the end of the tail")  # pragma: no cover

    def _emit(self, source: str, t_start: float, t_end: float) -> None:
        index = self._next_index
        self._next_index += 1
        self._segments += 1
        context_sources = list(self._recent_sources)
        self._recent_sources.append((index, source))
        cut_at = self._clock.now()
        for target in self._targets:
            self._queue.put_nowait(_Job(index, target, source, context_sources, cut_at, t_start, t_end))
        self._ensure_workers()

    # --- translating and delivering ------------------------------------------------------

    def _ensure_workers(self) -> None:
        if self._closed or self._workers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet: jobs wait in the queue until drain() or the next call
        for n in range(self._max_inflight):
            task = loop.create_task(self._worker(), name=f"glosa-pipeline-worker-{n}")
            task.add_done_callback(self._log_worker_exit)
            self._workers.append(task)

    @staticmethod
    def _log_worker_exit(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("pipeline worker %s died", task.get_name(), exc_info=task.exception())

    async def _stop_workers(self) -> None:
        workers, self._workers = self._workers, []
        current = asyncio.current_task()
        for task in workers:
            task.cancel()
        await asyncio.gather(*(w for w in workers if w is not current), return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._run(job)
            except Exception:
                log.exception("pipeline job failed: segment %d -> %s", job.index, job.target)
            finally:
                self._queue.task_done()

    async def _run(self, job: _Job) -> None:
        text: str | None = None
        usd = 0.0
        try:
            waited = self._clock.now() - job.cut_at
            if self._max_age_s is not None and waited > self._max_age_s:
                self._dropped += 1
                log.warning(
                    "segment %d -> %s waited %.1fs since its cut: dropped untranslated", job.index, job.target, waited
                )
            else:
                self._inflight += 1
                try:
                    # built here, at request time, from whatever translations of the context
                    # segments are already done -- never waited for (see module docstring)
                    done = self._recent_translations[job.target]
                    context = [(src, done.get(idx)) for idx, src in job.context_sources]
                    async with asyncio.timeout(self._translate_timeout_s):
                        translation = await self._translate(job.source, job.target, self._glossary, context)
                    usd = translation.usd
                    text = (translation.text or "").strip() or None
                finally:
                    self._inflight -= 1
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # aclose() / drain(): really cancelled
            # raised by the translate call itself: just a failure
            log.warning("translation of segment %d to %s failed: CancelledError", job.index, job.target)
        except Exception as exc:  # noqa: BLE001 - any failure (API error, timeout, bug) -> text=None
            log.warning("translation of segment %d to %s failed: %r", job.index, job.target, exc)
        finally:  # every job gets its result, or its target's order would wait for it forever
            latency_s = self._clock.now() - job.cut_at
            self._usd_total += usd
            if text is None:
                self._failed += 1
            else:
                self._translated += 1
                self._latencies.append(latency_s)
                cache = self._recent_translations[job.target]
                cache[job.index] = text
                while len(cache) > _CONTEXT_CACHE_SIZE:
                    cache.pop(next(iter(cache)))
            self._ready[job.target][job.index] = TranslatedSegment(
                target=job.target,
                index=job.index,
                source=job.source,
                text=text,
                latency_s=latency_s,
                usd=usd,
                t_start=job.t_start,
                t_end=job.t_end,
            )
        await self._deliver(job.target)

    async def _deliver(self, target: str) -> None:
        """Hand every ready segment of `target` to on_segment, in index order,
        one call at a time (whoever holds the lock also delivers what others
        finished meanwhile)."""
        async with self._deliver_locks[target]:
            ready = self._ready[target]
            while self._next_out[target] in ready:
                seg = ready.pop(self._next_out[target])
                self._next_out[target] += 1
                try:
                    result = self._on_segment(seg)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    log.exception("on_segment failed for segment %d -> %s", seg.index, seg.target)
