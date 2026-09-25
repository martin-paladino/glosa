"""Tests for LivePipeline: source text (transcribe-live interims + finals, or
Live Translate deltas) -> P1 segments -> per-target translations delivered in
strict index order (task-10c-brief.md).

Everything runs on a FakeClock with a fake ``translate`` whose completion the
tests control (gates) or whose latency is simulated by advancing the clock,
so no API budget is spent. The last test replays the real transcribe-live
recording samples/fixtures/tr_es.jsonl through FakeEngine.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from glosa.clock import FakeClock
from glosa.engines.fake import FakeEngine
from glosa.models import EngineConfig, GlossaryTerm
from glosa.text.pipeline import LivePipeline, TranslatedSegment
from glosa.text.segmenter import Segmenter
from glosa.text.translator import Translation

FIXTURE = Path(__file__).resolve().parents[2] / "samples" / "fixtures" / "tr_es.jsonl"

GLOSSARY = [GlossaryTerm(term="Kubernetes", keep_in_english=True)]


@dataclass
class Call:
    segment: str
    target: str
    glossary: list[GlossaryTerm]
    context: list[tuple[str, str | None]]


class FakeTranslate:
    """Stands in for Translator.translate. With gated=True every call waits
    until the test calls release(segment, target); latency_s advances the
    FakeClock before answering; fail_on makes those (segment, target) pairs
    raise; hang_on makes them never answer.
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        gated: bool = False,
        latency_s: float = 0.0,
        usd: float = 0.001,
        fail_on: set[tuple[str, str]] | None = None,
        hang_on: set[tuple[str, str]] | None = None,
    ) -> None:
        self.clock = clock
        self.gated = gated
        self.latency_s = latency_s
        self.usd = usd
        self.fail_on = fail_on or set()
        self.hang_on = hang_on or set()
        self.calls: list[Call] = []
        self.active = 0
        self.peak = 0
        self._gates: dict[tuple[str, str], asyncio.Event] = {}

    def _gate(self, segment: str, target: str) -> asyncio.Event:
        return self._gates.setdefault((segment, target), asyncio.Event())

    def release(self, segment: str, target: str) -> None:
        self._gate(segment, target).set()

    async def __call__(
        self, segment: str, target: str, glossary: list[GlossaryTerm], context: list[tuple[str, str | None]]
    ) -> Translation:
        self.calls.append(Call(segment, target, glossary, list(context)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if (segment, target) in self.hang_on:
                await asyncio.Event().wait()
            if self.gated:
                await self._gate(segment, target).wait()
            await asyncio.sleep(0)
            self.clock.advance(self.latency_s)
            if (segment, target) in self.fail_on:
                raise RuntimeError(f"boom: {segment!r} -> {target}")
            return Translation(text=f"[{target}] {segment}", latency_s=self.latency_s, usd=self.usd)
        finally:
            self.active -= 1


class Sink:
    """Collects on_segment calls (sync)."""

    def __init__(self) -> None:
        self.out: list[TranslatedSegment] = []

    def __call__(self, seg: TranslatedSegment) -> None:
        self.out.append(seg)

    def by_target(self, target: str) -> list[TranslatedSegment]:
        return [s for s in self.out if s.target == target]


def make_pipeline(
    clock: FakeClock,
    translate: FakeTranslate,
    sink,
    *,
    targets: list[str] | None = None,
    glossary: list[GlossaryTerm] | None = None,
    **kwargs,
) -> LivePipeline:
    return LivePipeline(
        targets=targets if targets is not None else ["es"],
        translate=translate,
        glossary=glossary if glossary is not None else GLOSSARY,
        clock=clock,
        segmenter_factory=Segmenter,
        on_segment=sink,
        **kwargs,
    )


async def settle(cond=None, rounds: int = 200) -> None:
    """Yield to the event loop until cond() holds (or just `rounds` times)."""
    for _ in range(rounds):
        if cond is not None and cond():
            return
        await asyncio.sleep(0)
    if cond is not None:
        assert cond(), "condition never became true"


def pipeline_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("glosa-pipeline")]


def words(n: int, start: int = 1) -> list[str]:
    return [f"w{i}" for i in range(start, start + n)]


# --- interims (transcribe-live: accumulated text that replaces) ---------------


async def test_growing_interims_cut_at_fourteen_words_and_final_adds_the_rest() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    ws = words(20)
    for n in range(1, len(ws) + 1):  # one more word per interim, as transcribe-live does
        pipe.interim(" ".join(ws[:n]), t=0.1 * n)
    pipe.final(" ".join(ws), t=2.5)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == [" ".join(ws[:14]), " ".join(ws[14:])]
    assert [(s.index, s.source, s.text) for s in sink.out] == [
        (0, " ".join(ws[:14]), "[es] " + " ".join(ws[:14])),
        (1, " ".join(ws[14:]), "[es] " + " ".join(ws[14:])),
    ]


async def test_growing_interims_cut_at_punctuation_and_translate_each_cut_once() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    for i, text in enumerate(
        [
            "Por cierto",
            "Por cierto,",  # comma with < 5 words: no cut
            "Por cierto, cuando ustedes reciben la factura de",
            "Por cierto, cuando ustedes reciben la factura de Cloud,",  # comma with 9 words: cut
            "Por cierto, cuando ustedes reciben la factura de Cloud, les va a",
            "Por cierto, cuando ustedes reciben la factura de Cloud, les va a decir algo.",
            "Por cierto, cuando ustedes reciben la factura de Cloud, les va a decir algo. Y",
        ]
    ):
        pipe.interim(text, t=0.5 * i)
    pipe.final("Por cierto, cuando ustedes reciben la factura de Cloud, les va a decir algo. Y nada.", t=4.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == [
        "Por cierto, cuando ustedes reciben la factura de Cloud,",
        "les va a decir algo.",
        "Y nada.",
    ]


async def test_interim_rewriting_the_committed_prefix_does_not_retranslate_it() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("el control plane de Kubernetes, a", t=0.0)  # cuts "... Kubernetes,"
    pipe.interim("el control plane de Kubernetes ha salido", t=0.5)  # rewrites the committed word
    pipe.interim("El Control Plane De Kubernetes ha salido caro", t=1.0)  # rewrites the whole prefix
    pipe.final("El control plane de Kubernetes ha salido caro.", t=1.5)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["el control plane de Kubernetes,", "ha salido caro."]


async def test_rewrites_of_uncommitted_words_are_honored() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("les va a decir algo como no", t=0.0)
    pipe.interim("les va a decir algo como, no sé, el", t=0.5)  # the model adds a comma later
    pipe.final("les va a decir algo como, no sé, el control plane.", t=1.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["les va a decir algo como,", "no sé, el control plane."]


async def test_interim_that_shrinks_below_the_committed_prefix_does_nothing() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    ws = words(16)
    pipe.interim(" ".join(ws), t=0.0)  # cuts w1..w14, w15 w16 stay open
    pipe.interim(" ".join(ws[:10]), t=0.5)  # shrinks below the 14 committed words
    pipe.interim(" ".join(ws[:12]), t=1.0)  # still below
    pipe.interim(" ".join(words(17)), t=1.5)  # grows again: w15..w17 are new
    pipe.final(None, t=2.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == [" ".join(ws[:14]), "w15 w16 w17"]


async def test_two_utterances_number_indices_continuously() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("Hola a todos.", t=0.0)
    pipe.interim("Hola a todos. Bienvenidos", t=0.5)
    pipe.final("Hola a todos. Bienvenidos a Nerdearla.", t=1.0)
    pipe.interim("Hoy", t=1.5)  # a new utterance: starts from word 0, not a shrink
    pipe.interim("Hoy hablamos de costos.", t=2.0)
    pipe.final(None, t=2.5)
    await pipe.drain()

    assert [(s.index, s.source) for s in sink.out] == [
        (0, "Hola a todos."),
        (1, "Bienvenidos a Nerdearla."),
        (2, "Hoy hablamos de costos."),
    ]


async def test_final_none_uses_the_last_interim_and_empty_final_drops_the_open_tail() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("sin puntuación todavía", t=0.0)
    pipe.final(None, t=0.5)  # None: flush what there is
    pipe.interim("eh mm", t=1.0)
    pipe.final("", t=1.5)  # "": transcribe-live says it was not speech after all
    pipe.interim("y seguimos", t=2.0)
    pipe.final(None, t=2.5)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["sin puntuación todavía", "y seguimos"]


async def test_a_punctuation_cut_inside_a_word_is_not_lost_nor_duplicated() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("we use gemini 3.", t=0.0)  # the segmenter cuts after "3.": nothing follows it yet
    pipe.interim("we use gemini 3.5 flash lite", t=0.5)
    pipe.final(None, t=1.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["we use gemini 3.", "5 flash lite"]


async def test_a_period_inside_a_token_that_arrives_whole_does_not_cut() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("we use gemini 3.5 with Node.js", t=0.0)
    pipe.final("we use gemini 3.5 with Node.js on k8s.io.", t=0.5)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["we use gemini 3.5 with Node.js on k8s.io."]


async def test_punctuation_added_to_a_word_committed_by_tick_is_not_a_new_segment() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("uno dos tres", t=0.0)
    pipe.tick(3.0)  # time cut: "uno dos tres"
    pipe.interim("uno dos tres, cuatro cinco", t=3.5)  # "tres" gains a comma after the cut
    pipe.final(None, t=4.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["uno dos tres", "cuatro cinco"]


async def test_punctuation_only_segments_are_not_translated() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("Bueno... entonces sí.", t=0.0)  # the segmenter cuts "Bueno.", ".", "."
    pipe.final(None, t=0.5)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["Bueno.", "entonces sí."]
    assert [s.index for s in sink.out] == [0, 1]


# --- deltas (Live Translate: text that is appended) ---------------------------


async def test_live_translate_deltas_append_and_final_none_flushes_on_pause() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["pt"])

    for i, delta in enumerate(["Great starting scenario, for", " sure. There's", " also a provisioning", " agent"]):
        pipe.delta(delta, t=0.5 * i)
    await settle(lambda: len(tr.calls) == 1)
    assert [c.segment for c in tr.calls] == ["Great starting scenario, for sure."]

    pipe.final(None, t=2.0)  # VAD pause
    pipe.delta(" So it", t=2.5)  # the next utterance starts with a leading space
    pipe.delta(" works.", t=3.0)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == [
        "Great starting scenario, for sure.",
        "There's also a provisioning agent",
        "So it works.",
    ]
    assert [s.index for s in sink.by_target("pt")] == [0, 1, 2]


async def test_a_word_split_across_deltas_by_a_time_cut_keeps_its_tail() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.delta("we run Kuber", t=0.0)
    pipe.tick(3.0)  # time cut in the middle of a word
    pipe.delta("netes everywhere.", t=3.2)
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["we run Kuber", "netes everywhere."]


# --- tick ---------------------------------------------------------------------


async def test_tick_cuts_by_time_counting_from_when_the_segment_opened() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("sin", t=10.0)
    pipe.interim("sin puntuación", t=11.0)  # later interims do not restart the timer
    pipe.interim("sin puntuación ni pausa", t=12.0)
    pipe.tick(12.9)
    await settle()
    assert tr.calls == []

    pipe.tick(13.0)  # 3.0 s since "sin" opened the segment
    pipe.interim("sin puntuación ni pausa y sigue", t=13.2)
    pipe.tick(16.1)  # the timer restarted at 13.2 with "y sigue"
    await settle()
    assert [c.segment for c in tr.calls] == ["sin puntuación ni pausa"]

    pipe.tick(16.3)
    await pipe.drain()
    assert [c.segment for c in tr.calls] == ["sin puntuación ni pausa", "y sigue"]


async def test_tick_timer_restarts_after_a_cut() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("uno dos", t=0.0)
    pipe.interim("uno dos tres cuatro cinco, seis", t=2.0)  # cut; "seis" opens a segment at 2.0
    pipe.tick(4.9)
    await settle()
    assert [c.segment for c in tr.calls] == ["uno dos tres cuatro cinco,"]

    pipe.tick(5.0)
    await pipe.drain()
    assert [c.segment for c in tr.calls] == ["uno dos tres cuatro cinco,", "seis"]


async def test_tick_with_nothing_open_is_a_noop() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.tick(100.0)
    pipe.interim("Listo.", t=100.5)
    pipe.tick(200.0)
    await pipe.drain()
    assert [c.segment for c in tr.calls] == ["Listo."]


# --- translation: parallel targets, order, failures, concurrency ---------------


async def test_two_targets_in_parallel_deliver_in_order_per_target() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["es", "pt"])

    pipe.interim("First sentence here. Second one", t=0.0)
    pipe.final("First sentence here. Second one.", t=0.5)
    await settle(lambda: len(tr.calls) == 4)
    assert tr.active == 4  # every (segment, target) translation runs at once

    tr.release("Second one.", "es")  # index 1 finishes before index 0
    await settle(lambda: tr.active == 3)
    assert sink.out == []

    tr.release("First sentence here.", "pt")
    await settle(lambda: len(sink.out) == 1)
    assert [(s.target, s.index) for s in sink.out] == [("pt", 0)]

    tr.release("First sentence here.", "es")
    await settle(lambda: len(sink.out) == 3)
    assert [(s.target, s.index) for s in sink.by_target("es")] == [("es", 0), ("es", 1)]

    tr.release("Second one.", "pt")
    await pipe.drain()
    assert [(s.target, s.index, s.text) for s in sink.by_target("pt")] == [
        ("pt", 0, "[pt] First sentence here."),
        ("pt", 1, "[pt] Second one."),
    ]


async def test_failed_translation_yields_none_and_does_not_block_the_next(caplog) -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock, fail_on={("Uno.", "es")})
    sink = Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["es", "en"])

    with caplog.at_level(logging.WARNING, logger="glosa.text.pipeline"):
        pipe.interim("Uno. Dos.", t=0.0)
        await pipe.drain()

    assert [(s.index, s.text) for s in sink.by_target("es")] == [(0, None), (1, "[es] Dos.")]
    assert [(s.index, s.text) for s in sink.by_target("en")] == [(0, "[en] Uno."), (1, "[en] Dos.")]
    failed = sink.by_target("es")[0]
    assert failed.usd == 0.0
    assert "boom" in caplog.text
    stats = pipe.stats
    assert (stats["segments"], stats["translated"], stats["failed"]) == (2, 3, 1)


async def test_a_translation_that_never_answers_times_out_as_a_failure() -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock, hang_on={("Uno.", "es")})
    sink = Sink()
    pipe = make_pipeline(clock, tr, sink, translate_timeout_s=0.05)

    pipe.interim("Uno. Dos.", t=0.0)
    await pipe.drain(timeout_s=5.0)

    assert [(s.index, s.text) for s in sink.out] == [(0, None), (1, "[es] Dos.")]
    assert pipe.stats["failed"] == 1


async def test_max_inflight_bounds_concurrent_translations() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["es", "pt"], max_inflight=3)

    pipe.interim("Uno. Dos. Tres.", t=0.0)  # 3 segments x 2 targets = 6 translations
    await settle(lambda: len(tr.calls) == 3)
    await settle()
    assert tr.active == 3 and len(tr.calls) == 3
    assert pipe.stats["inflight"] == 3 and pipe.stats["queued"] == 3
    # the queue is FIFO: segment 0 for both targets, then segment 1 for es
    assert [(c.segment, c.target) for c in tr.calls] == [("Uno.", "es"), ("Uno.", "pt"), ("Dos.", "es")]

    for seg in ["Uno.", "Dos.", "Tres."]:
        for target in ["es", "pt"]:
            tr.release(seg, target)
            await settle()
            assert tr.active <= 3
    await pipe.drain()

    assert tr.peak == 3
    assert len(sink.out) == 6


async def test_context_is_the_previous_source_segments_and_glossary_is_passed_as_is() -> None:
    clock = FakeClock()
    # max_inflight=1: one segment's translation is always done before the next is requested,
    # so context always carries the previous segments' translations too (see the "never waits"
    # test below for what happens when it is NOT done yet).
    tr, sink = FakeTranslate(clock), Sink()
    glossary = [GlossaryTerm(term="control plane", keep_in_english=False, translation="plano de control")]
    pipe = make_pipeline(clock, tr, sink, glossary=glossary, max_inflight=1)

    pipe.interim("Uno. Dos.", t=0.0)
    pipe.final("Uno. Dos. Tres.", t=0.5)
    pipe.final("Cuatro.", t=1.0)  # context carries across utterances
    await pipe.drain()

    assert [(c.segment, c.context) for c in tr.calls] == [
        ("Uno.", []),
        ("Dos.", [("Uno.", "[es] Uno.")]),
        ("Tres.", [("Uno.", "[es] Uno."), ("Dos.", "[es] Dos.")]),
        ("Cuatro.", [("Dos.", "[es] Dos."), ("Tres.", "[es] Tres.")]),
    ]
    assert all(c.glossary is glossary for c in tr.calls)


async def test_context_never_waits_for_a_still_running_translation() -> None:
    """"use the most recent already-completed context available at request
    time; never wait for it" (task-16q-brief.md): a segment whose predecessor
    hasn't finished translating yet is sent with that predecessor's
    translation missing, not delayed."""
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink, max_inflight=2)

    pipe.final("Uno.", t=0.0)
    pipe.final("Dos.", t=0.5)
    await settle()  # both jobs dispatched; "Uno." -> es is still gated (running)

    dos_call = next(c for c in tr.calls if c.segment == "Dos.")
    assert dos_call.context == [("Uno.", None)]

    tr.release("Uno.", "es")
    tr.release("Dos.", "es")
    await pipe.drain()


async def test_context_n_is_configurable() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink, context_n=0)

    pipe.final("Uno. Dos.", t=0.0)
    await pipe.drain()
    assert [c.context for c in tr.calls] == [[], []]


async def test_latency_is_measured_with_the_clock_from_cut_to_answer_and_usd_is_summed() -> None:
    clock = FakeClock(start=100.0)
    tr, sink = FakeTranslate(clock, latency_s=1.5, usd=0.002), Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["es", "en"], max_inflight=1)

    pipe.interim("Hola.", t=7.0)  # the caller's clock (t) is not the latency clock
    await pipe.drain()

    # es answers 1.5 s after the cut; en waited in the queue behind it, and that counts
    assert [(s.target, s.latency_s) for s in sink.out] == [("es", 1.5), ("en", 3.0)]
    assert all(s.usd == 0.002 for s in sink.out)
    stats = pipe.stats
    assert stats["usd_total"] == pytest.approx(0.004)
    assert stats["latencies_s"] == [1.5, 3.0]
    assert stats["latency_p50_s"] == pytest.approx(2.25)


async def test_on_segment_can_be_async() -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock)
    got: list[tuple[str, int]] = []

    async def on_segment(seg: TranslatedSegment) -> None:
        await asyncio.sleep(0)
        got.append((seg.target, seg.index))

    pipe = make_pipeline(clock, tr, on_segment, targets=["es", "pt"])
    pipe.final("Uno. Dos. Tres.", t=0.0)
    await pipe.drain()

    assert [i for tgt, i in got if tgt == "es"] == [0, 1, 2]
    assert [i for tgt, i in got if tgt == "pt"] == [0, 1, 2]


async def test_an_on_segment_error_is_logged_and_delivery_continues(caplog) -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock)
    got: list[int] = []

    def on_segment(seg: TranslatedSegment) -> None:
        if seg.index == 0:
            raise ValueError("subscriber exploded")
        got.append(seg.index)

    pipe = make_pipeline(clock, tr, on_segment)
    with caplog.at_level(logging.ERROR, logger="glosa.text.pipeline"):
        pipe.final("Uno. Dos.", t=0.0)
        await pipe.drain()

    assert got == [1]
    assert "subscriber exploded" in caplog.text


async def test_the_sync_methods_only_enqueue() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.final("Uno. Dos.", t=0.0)
    assert tr.calls == []  # nothing ran inline: the work is on the pipeline's own tasks
    assert pipe.stats["segments"] == 2 and pipe.stats["queued"] == 2
    assert all(t.get_name().startswith("glosa-pipeline-worker") for t in pipeline_tasks())
    await pipe.drain()
    assert len(sink.out) == 2


async def test_an_empty_translation_is_a_failure() -> None:
    clock = FakeClock()
    sink = Sink()

    async def empty(segment, target, glossary, context) -> Translation:
        return Translation(text="", latency_s=0.0, usd=0.001)

    pipe = make_pipeline(clock, empty, sink)
    pipe.final("Uno.", t=0.0)
    await pipe.drain()

    assert [(s.source, s.text) for s in sink.out] == [("Uno.", None)]
    assert (pipe.stats["translated"], pipe.stats["failed"]) == (0, 1)


async def test_a_job_that_waited_too_long_since_its_cut_is_dropped_unsent() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink, max_inflight=1, max_age_s=8.0)

    pipe.final("Uno.", t=0.0)  # taken by the only worker at once
    pipe.final("Dos.", t=0.1)  # queued behind it
    await settle(lambda: len(tr.calls) == 1)
    clock.advance(8.5)  # "Uno." is stuck: "Dos." ages in the queue
    pipe.final("Tres.", t=8.5)
    tr.release("Uno.", "es")
    tr.release("Tres.", "es")
    await pipe.drain()

    assert [c.segment for c in tr.calls] == ["Uno.", "Tres."]  # "Dos." was never sent
    assert [(s.source, s.text) for s in sink.out] == [("Uno.", "[es] Uno."), ("Dos.", None), ("Tres.", "[es] Tres.")]
    assert pipe.stats["failed"] == 1 and pipe.stats["dropped"] == 1
    await pipe.aclose()


async def test_max_age_none_translates_every_job_however_late() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink, max_inflight=1, max_age_s=None)

    pipe.final("Uno. Dos.", t=0.0)
    await settle(lambda: len(tr.calls) == 1)
    clock.advance(60.0)
    tr.release("Uno.", "es")
    tr.release("Dos.", "es")
    await pipe.drain()

    assert [s.text for s in sink.out] == ["[es] Uno.", "[es] Dos."]


async def test_a_translate_that_raises_cancelled_error_by_itself_is_a_failure_that_blocks_nothing() -> None:
    """A library that raises CancelledError without our task being cancelled:
    the segment still gets its (failed) result, the worker survives, and the
    segments after it are delivered."""
    clock = FakeClock()
    sink = Sink()
    calls: list[str] = []

    async def translate(segment, target, glossary, context) -> Translation:
        calls.append(segment)
        if segment == "Uno.":
            raise asyncio.CancelledError()
        return Translation(text=f"[{target}] {segment}", latency_s=0.0, usd=0.0)

    pipe = make_pipeline(clock, translate, sink, max_inflight=1)
    pipe.final("Uno. Dos.", t=0.0)
    await pipe.drain()
    pipe.final("Tres.", t=1.0)
    await pipe.drain()

    assert calls == ["Uno.", "Dos.", "Tres."]
    assert [(s.source, s.text) for s in sink.out] == [("Uno.", None), ("Dos.", "[es] Dos."), ("Tres.", "[es] Tres.")]
    await pipe.aclose()


async def test_segments_carry_the_time_they_opened_and_were_cut() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("Hola a", t=10.0)
    pipe.interim("Hola a todos.", t=10.5)  # cut: opened at 10.0, cut at 10.5
    pipe.interim("Hola a todos. Bienvenidos", t=11.0)
    pipe.final("Hola a todos. Bienvenidos. Uno. Dos.", t=12.0)  # three cuts at once share 11.0-12.0
    await pipe.drain()

    spans = [(s.source, s.t_start, s.t_end) for s in sink.out]
    assert spans[0] == ("Hola a todos.", 10.0, 10.5)
    assert [s for s, _, _ in spans[1:]] == ["Bienvenidos.", "Uno.", "Dos."]
    starts = [a for _, a, _ in spans[1:]]
    ends = [b for _, _, b in spans[1:]]
    assert starts[0] == 11.0 and ends[-1] == 12.0
    assert all(a < b for a, b in zip(starts, ends))
    assert ends[:-1] == pytest.approx(starts[1:])  # consecutive, never overlapping


# --- lifecycle ------------------------------------------------------------------


async def test_drain_flushes_the_open_utterance_and_waits_for_pending_translations() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock, gated=True), Sink()
    pipe = make_pipeline(clock, tr, sink)

    pipe.interim("Uno. y lo que quedó abierto", t=0.0)

    async def release_later() -> None:
        await settle(lambda: len(tr.calls) == 2)
        tr.release("Uno.", "es")
        tr.release("y lo que quedó abierto", "es")

    releaser = asyncio.create_task(release_later())
    await pipe.drain()
    await releaser

    assert [s.source for s in sink.out] == ["Uno.", "y lo que quedó abierto"]
    assert pipe.stats["inflight"] == 0 and pipe.stats["queued"] == 0
    assert pipeline_tasks() == []  # the workers are stopped after draining

    pipe.final("Otra.", t=5.0)  # still usable afterwards
    tr.release("Otra.", "es")
    await pipe.drain()
    assert [(s.index, s.source) for s in sink.out][-1] == (2, "Otra.")
    await pipe.aclose()


async def test_drain_with_nothing_pending_returns_at_once() -> None:
    clock = FakeClock()
    pipe = make_pipeline(clock, FakeTranslate(clock), Sink())
    await pipe.drain()
    await pipe.aclose()
    assert pipeline_tasks() == []


async def test_drain_timeout_cancels_what_is_left(caplog) -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock, hang_on={("Uno.", "es")})
    sink = Sink()
    pipe = make_pipeline(clock, tr, sink, translate_timeout_s=None)

    pipe.final("Uno.", t=0.0)
    with caplog.at_level(logging.WARNING, logger="glosa.text.pipeline"):
        await pipe.drain(timeout_s=0.05)

    assert sink.out == []
    assert "drain" in caplog.text
    assert pipeline_tasks() == []
    assert tr.active == 0  # the hung call was cancelled


async def test_aclose_cancels_everything_without_waiting() -> None:
    clock = FakeClock()
    tr = FakeTranslate(clock, hang_on={("Uno.", "es"), ("Dos.", "es")})
    sink = Sink()
    pipe = make_pipeline(clock, tr, sink, translate_timeout_s=None)

    pipe.final("Uno. Dos. Tres.", t=0.0)
    await settle(lambda: tr.active == 2)
    tasks = pipeline_tasks()
    assert tasks

    await pipe.aclose()

    assert all(t.done() for t in tasks)
    assert pipeline_tasks() == []
    assert tr.active == 0
    pipe.interim("Después del cierre.", t=1.0)  # ignored once closed
    pipe.final(None, t=1.5)
    await settle()
    assert pipeline_tasks() == []
    assert pipe.stats["segments"] == 3
    await pipe.aclose()  # idempotent


# --- replay of a real transcribe-live session ------------------------------------


async def test_replay_of_the_real_transcribe_live_fixture() -> None:
    clock = FakeClock()
    tr, sink = FakeTranslate(clock), Sink()
    pipe = make_pipeline(clock, tr, sink, targets=["en"])
    engine = FakeEngine(EngineConfig(kind="fake", source_lang="es", target_lang=None, fixture_path=str(FIXTURE)), clock)
    await engine.connect()

    finals: list[str] = []
    ticks = 0
    async for ev in engine.events():
        while (ticks + 1) * 0.1 <= ev.t_recv:  # a RoomWorker ticks every ~100 ms
            ticks += 1
            pipe.tick(ticks * 0.1)
        if ev.kind == "source_delta" and ev.meta.get("interim"):
            pipe.interim(ev.text, ev.t_recv)
        elif ev.kind == "source_final":
            pipe.final(ev.text, ev.t_recv)
            finals.append(ev.text)
    await engine.close()
    await pipe.drain()

    sources = [s.source for s in sink.out]
    counts = [len(s.split()) for s in sources]
    assert len(finals) == 8
    assert 15 <= len(sources) <= 40
    assert max(counts) <= 14
    assert [s.index for s in sink.out] == list(range(len(sources)))
    assert [c.segment for c in tr.calls] == sources  # each cut translated exactly once
    # what was translated is (almost) what was said: rewrites of the committed
    # prefix are the only accepted divergence
    said = " ".join(finals).lower().split()
    translated = " ".join(sources).lower().split()
    assert difflib.SequenceMatcher(a=said, b=translated, autojunk=False).ratio() >= 0.9
