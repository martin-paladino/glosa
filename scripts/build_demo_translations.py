#!/usr/bin/env python3
"""Builds samples/fixtures/tr_es_en.json: the Spanish demo-fake room's real
English translations, so ``make demo-fake`` shows real captions in the `en`
track instead of FakeTranslator's placeholder ("[en] <source text>").

This is NOT a pytest test -- it spends real API budget (a few cents at
most) and is meant to be run by hand, rarely, only when
samples/fixtures/tr_es.jsonl changes. It:

1. Drives a RoomWorker exactly like tests/test_room.py's glossary-talk tests
   do (DrivenClock/FakeEngine, no ffmpeg, no wall-clock wait), replaying the
   full samples/fixtures/tr_es.jsonl recording for a talk with no glossary
   (matching config.demo-fake.yaml's demo-es room -- see its top comment),
   and collects every segment text TranslationLane hands to the translator.
2. Translates each segment, in order, with the real Translator
   (gemini-3.5-flash-lite, the same system-instruction prompt production
   uses, including rolling context of the last couple of segments) --
   exactly the production code path, just with a real Translator instead of
   FakeTranslator.
3. Writes {segment text: English translation} to samples/fixtures/tr_es_en.json.

Usage::

    uv run python scripts/build_demo_translations.py

The Gemini key comes from ``--env-path`` (default: this repo's own .env;
pass another checkout's .env if this one is a worktree without its own --
see samples/README.md). The key is read with dotenv_values() and passed
straight to Translator; it is never printed or written anywhere.

Aborts before any call that would push total spend over --budget-usd
(default $0.02).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_room as tr  # noqa: E402  (reuses its offline RoomWorker harness)

from glosa.captions.bus import CaptionBus  # noqa: E402
from glosa.db import init_db  # noqa: E402
from glosa.text.translator import Translator  # noqa: E402

FIXTURE_OUT = ROOT / "samples" / "fixtures" / "tr_es_en.json"
# Just past tr_es.jsonl's last record (t=60.433s of simulated time): long
# enough to flush every segment, short enough that the relay does not
# rotate to a second session (which would replay the fixture from the top).
RUN_FOR_S = 61.0


class BudgetExceeded(RuntimeError):
    pass


class RealTranslateCollector:
    """``translate=`` for RoomWorker: the real Translator.translate(), with
    every (segment -> translation) pair recorded, in call order, and a hard
    stop before any call that would push total spend over ``budget_usd``."""

    def __init__(self, translator: Translator, budget_usd: float) -> None:
        self._translator = translator
        self._budget_usd = budget_usd
        self.spent_usd = 0.0
        self.translations: dict[str, str] = {}

    async def __call__(self, segment, target, glossary, context):
        if self.spent_usd >= self._budget_usd:
            raise BudgetExceeded(f"stopping before translating {segment!r}: already spent ${self.spent_usd:.4f}")
        result = await self._translator.translate(segment, target, glossary, context)
        self.spent_usd += result.usd
        self.translations.setdefault(segment, result.text)
        print(f"  ${result.usd:.5f}  {segment!r} -> {result.text!r}", file=sys.stderr)
        return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", default=str(ROOT / ".env"), help="path to the .env with GEMINI_API_KEY")
    parser.add_argument("--budget-usd", type=float, default=0.02, help="hard cap on real spend")
    parser.add_argument("--out", default=str(FIXTURE_OUT), help="where to write the fixture")
    args = parser.parse_args()

    api_key = dotenv_values(args.env_path).get("GEMINI_API_KEY")
    if not api_key:
        sys.exit(f"no GEMINI_API_KEY in {args.env_path}")

    translator = Translator(api_key)
    collector = RealTranslateCollector(translator, args.budget_usd)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = init_db(Path(tmp) / "glosa.db")
            clock = tr.DrivenClock()
            bus = CaptionBus(clock=clock)
            factory = tr.Factory(clock, tr.TR_ES)
            worker = tr._worker(
                tr._room(targets=["en"]),
                tr._settings(("r1", "es")),
                bus,
                db,
                clock,
                factory,
                tr.IngestFactory(),
                translate=collector,
            )
            await worker.start(tr._talk("g1"))  # language es, targets ("en",), no glossary: matches the demo
            try:
                await tr.run_for(clock, RUN_FOR_S)
            except BudgetExceeded as exc:
                print(f"budget cap hit: {exc}", file=sys.stderr)
            await worker.stop()
            db.close()
    finally:
        await translator.aclose()

    print(f"\n{len(collector.translations)} segments, ${collector.spent_usd:.4f} spent", file=sys.stderr)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(collector.translations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
