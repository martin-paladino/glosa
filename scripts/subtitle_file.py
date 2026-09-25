#!/usr/bin/env python3
"""scripts/subtitle_file.py (task-21a): turn any ffmpeg-readable video/audio
file into English/Spanish subtitles using Glosa's REAL pipeline -- made for
the hackathon demo video, whose subtitles must be produced by Glosa itself.

Usage::

    uv run python scripts/subtitle_file.py INPUT --lang es --targets en \\
        [--engine glossary|fast] [--out-dir DIR] [--glossary "term=translation,..."]

Drives one real RoomWorker (glosa/room.py, unmodified) on INPUT as a "file"
source, at real-time pace (``realtime=True`` -- the live Gemini APIs need
audio delivered at real speed, same as bench/bench.py's ``--live`` runs and
tests/test_room.py's own live tests), engine_mode "live" (the real key --
see ``resolve_api_key()``, reused from bench/bench.py: this task's worktree
has no .env of its own, the sibling main checkout does; the key is never
printed, logged or written to any file this script produces).

Writes ``<stem>.<lang>.vtt`` and ``<stem>.<lang>.srt`` for the source
language and every ``--targets`` language, via glosa/exports.py's
``render()`` -- the SAME renderer glosa/web/public_api.py's ``GET
/exports`` route uses, with the same ``shift_s`` rule (task-11r-brief.md
Ruling 2, see ``_shift_s()`` below): the room's current-run latency p50 if
there is enough signal, else ``Settings.default_export_shift_s``. Prints
progress while the room runs and the real cost (``Database.total_cost()``)
at the end.

``--engine`` picks the Gemini engine kind (default "glossary", per the task
brief: "fast has no Spanish-source advantage and costs 3x"): "glossary"
transcribes verbatim then translates (glosa/engines/transcribe.py +
glosa/text/translator.py); "fast" is a single Live Translate hop
(glosa/engines/live_translate.py).

Reused, not copied, from bench/bench.py (untouched by this task): the
"one real RoomWorker per run" pattern (build a Room/Talk, a file-source
RoomCfg, drive the worker until ``worker.talk`` is ``None`` again, read
segments back from a scratch DB) and ``resolve_api_key()`` itself, imported
directly (same worktree layout, same lookup).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
# Same guard bench/bench.py uses: running this file directly as a script
# would otherwise auto-insert scripts/ into sys.path[0], which (PEP 420)
# can shadow a later `import scripts...` with this module itself.
while _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
sys.path.insert(0, str(ROOT))

from bench.bench import resolve_api_key  # noqa: E402  (small shared helper, reused not copied)
from glosa.captions.bus import CaptionBus  # noqa: E402
from glosa.clock import RealClock  # noqa: E402
from glosa.config import RoomCfg, Settings  # noqa: E402
from glosa.db import Database, init_db  # noqa: E402
from glosa.exports import ExportSegment, render  # noqa: E402
from glosa.models import GlossaryTerm, Room, Talk  # noqa: E402
from glosa.room import RoomWorker  # noqa: E402
from glosa.web.app import make_engine_factory  # noqa: E402

ENGINES = ("fast", "glossary")
DEFAULT_ENGINE = "glossary"
FORMATS = ("vtt", "srt")
_ADMIN_PASSWORD_PLACEHOLDER = "subtitle-file-tool-not-a-server"  # never used: no web server runs


class SubtitleFileError(RuntimeError):
    """A clear, user-facing error: bad input, no API key, etc."""


@dataclass
class Result:
    cost_usd: float
    audio_s: float
    written: list[Path]


def _require_input_file(path: Path) -> None:
    if not path.is_file():
        raise SubtitleFileError(f"input file not found: {path}")


def parse_glossary(spec: str | None) -> list[GlossaryTerm]:
    """"term=translation,term2,..." -> Talk.glossary. A bare term (no "=")
    means keep_in_english=True; glosa/text/translator.py's
    _build_system_instruction treats keep_in_english=False with no
    translation the same as keep_in_english=True, so a term meant to be
    translated always needs its "=translation" (see bench/bench.py's
    to_glossary_terms, which makes the same choice from terms.yaml)."""
    if not spec:
        return []
    terms: list[GlossaryTerm] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        term, sep, translation = item.partition("=")
        term = term.strip()
        if not term:
            continue
        if sep:
            terms.append(GlossaryTerm(term, False, translation=translation.strip()))
        else:
            terms.append(GlossaryTerm(term, True))
    return terms


def _build_room_and_talk(
    input_path: Path, source_lang: str, targets: list[str], engine: str, glossary: list[GlossaryTerm]
) -> tuple[Room, Talk]:
    rid = "subtitle-file"
    room = Room(
        id=rid, slug=rid, name=input_path.stem, source_type="file", source_url=str(input_path),
        mode="auto", public_token="tok-subtitle-file", default_targets=list(targets),
    )
    start = datetime.now(timezone.utc)
    talk = Talk(
        id=f"{input_path.stem}-{int(start.timestamp())}", room_id=rid, title=input_path.stem, speakers=[],
        language=source_lang, targets=list(targets), engine=engine, start=start, end=start + timedelta(hours=6),
        abstract="", tags=[], glossary=glossary, status="scheduled", actual_start=None, actual_end=None,
    )
    return room, talk


def _shift_s(worker: RoomWorker, settings: Settings) -> float:
    """The same rule glosa/web/public_api.py's export route applies
    (task-11r-brief.md Ruling 2): the room's CURRENT run's latency p50 if
    there is enough signal, else the configured default. By the time we
    render here the run has just ended (RoomWorker clears its run on talk
    end -- see RoomWorker.latency_p50()'s own docstring), so this normally
    falls through to default_export_shift_s, exactly what the route itself
    would do for a finished talk (its live run is long gone by export time
    there too)."""
    p50 = worker.latency_p50()
    return p50 if p50 is not None else settings.default_export_shift_s


async def _write_exports(
    db: Database, talk_id: str, langs: list[str], shift_s: float, out_dir: Path, stem: str
) -> list[Path]:
    """<stem>.<lang>.vtt and <stem>.<lang>.srt for every lang in ``langs``
    (source first, then each target), via glosa/exports.py's render() --
    the same renderer glosa/web/public_api.py's export route uses. Version
    is always "live": this script never builds a "corrected" export."""
    written: list[Path] = []
    for lang in langs:
        segments = await db.get_segments(talk_id, lang, "live")
        export_segs = [ExportSegment(text=s.text, t_start=s.t_start, t_end=s.t_end) for s in segments]
        for fmt in FORMATS:
            body = render(fmt, export_segs, shift_s=shift_s)
            path = out_dir / f"{stem}.{lang}.{fmt}"
            path.write_text(body, encoding="utf-8")
            written.append(path)
    return written


async def run_subtitle_file(
    input_path: Path,
    *,
    source_lang: str,
    targets: list[str],
    engine: str = DEFAULT_ENGINE,
    glossary: list[GlossaryTerm] | None = None,
    out_dir: Path | None = None,
    api_key: str,
    engine_mode: str = "live",
    fake_fixture: str | None = None,
    quiet: bool = False,
) -> Result:
    """Drive one real RoomWorker on ``input_path`` and write VTT/SRT for
    ``source_lang`` and every ``targets`` language. ``engine_mode``/
    ``fake_fixture`` exist for tests (engine_mode "fake": FakeEngine
    replaying a recording, no network -- see tests/test_subtitle_file.py);
    the CLI (``main()`` below) always uses the default "live"."""
    _require_input_file(input_path)
    out_dir = Path(out_dir) if out_dir is not None else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = list(targets)
    glossary = glossary or []

    room, talk = _build_room_and_talk(input_path, source_lang, targets, engine, glossary)
    settings = Settings(
        gemini_api_key=api_key,
        admin_password=_ADMIN_PASSWORD_PLACEHOLDER,
        engine_mode=engine_mode,  # type: ignore[arg-type]
        fake_fixture=fake_fixture,
        rooms=[
            RoomCfg(
                id=room.id, name=room.name, source_type="file", source_url=room.source_url,
                default_targets=targets, language=source_lang,
            )
        ],
    )
    clock = RealClock()
    bus = CaptionBus(clock=clock)
    tmp_dir = Path(tempfile.mkdtemp(prefix="glosa-subtitle-file-"))
    db = init_db(tmp_dir / "subtitle.db")
    engine_factory = make_engine_factory(settings, clock)
    worker = RoomWorker(room, settings, bus, db, clock, engine_factory, realtime=True)

    if not quiet:
        print(f"subtitling {input_path.name}: {source_lang} -> {', '.join(targets)} (engine={engine})")
    shift_s = settings.default_export_shift_s
    measured_shift: float | None = None
    try:
        await worker.start(talk)
        last_reported = -5.0
        while worker.talk is not None:
            await asyncio.sleep(0.5)
            live_p50 = worker.latency_p50()  # sampled while the run is live: gone once the talk ends
            if live_p50 is not None:
                measured_shift = live_p50
            if not quiet and worker.audio_s - last_reported >= 5.0:
                print(f"  ...{worker.audio_s:.1f}s processed")
                last_reported = worker.audio_s
        shift_s = measured_shift if measured_shift is not None else _shift_s(worker, settings)
    finally:
        await worker.stop()

    written = await _write_exports(db, talk.id, [source_lang, *targets], shift_s, out_dir, input_path.stem)
    cost_usd = await db.total_cost()
    audio_s = worker.audio_s
    db.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if not quiet:
        print("wrote:")
        for path in written:
            print(f"  {path}")
        print(f"cost: ${cost_usd:.4f}  ({audio_s:.1f}s of audio, shift {shift_s:.2f}s)")
    return Result(cost_usd=cost_usd, audio_s=audio_s, written=written)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Subtitle any ffmpeg-readable video/audio file with Glosa's real pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="ffmpeg-readable video/audio file, e.g. a .mp4/.mov/.opus")
    parser.add_argument("--lang", required=True, help="source language spoken in the file, e.g. es")
    parser.add_argument(
        "--targets", required=True, help='target language(s) to translate into, comma-separated, e.g. "en" or "en,fr"'
    )
    parser.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE, help=f"engine kind (default: {DEFAULT_ENGINE})")
    parser.add_argument("--out-dir", type=Path, default=None, dest="out_dir", help="output directory (default: next to INPUT)")
    parser.add_argument("--glossary", default=None, help='glossary terms, e.g. "Kubernetes,namespaces=espacios de nombres"')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _require_input_file(args.input)
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        if not targets:
            raise SubtitleFileError("--targets must name at least one language")
        api_key = resolve_api_key()
        glossary = parse_glossary(args.glossary)
        asyncio.run(
            run_subtitle_file(
                args.input, source_lang=args.lang, targets=targets, engine=args.engine,
                glossary=glossary, out_dir=args.out_dir, api_key=api_key,
            )
        )
    except SubtitleFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
