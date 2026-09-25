"""bench/bench.py: Task 15c -- run the four (clip x engine) combinations
through the REAL RoomWorker (glosa/room.py, unmodified) and measure
latency, quality, glossary-term accuracy and cost, per
.superpowers/sdd/2026-09-24-glosa/task-15c-brief.md.

Usage:
    uv run python bench/bench.py               # dry run: engine_mode
                                                 # "fake" (FakeEngine, no
                                                 # network), proves the
                                                 # harness wiring end to
                                                 # end. Spends nothing,
                                                 # writes nothing under
                                                 # bench/.
    uv run python bench/bench.py --live         # the real bench: 4 real
                                                 # room runs + judge calls
                                                 # (~US$0.15, hard cap
                                                 # US$0.50 -- see
                                                 # SPEND_CAP_USD below).
                                                 # Writes bench/raw/*.jsonl
                                                 # + *.meta.json,
                                                 # bench/reference/*.txt,
                                                 # bench/results.md.
    uv run python bench/bench.py --live --only en_clip:glossary
                                                 # re-run just one
                                                 # (clip, engine) combo
                                                 # (e.g. after a failure);
                                                 # repeatable.
    uv run python bench/bench.py --recompute    # Task 15c fix round 1:
                                                 # regenerate results.md from
                                                 # the committed
                                                 # bench/raw/*.jsonl + the
                                                 # samples/reference/*.json3
                                                 # -- no network, no spend,
                                                 # no RoomWorker; fidelity/
                                                 # fluency come from the
                                                 # original live run's own
                                                 # judge output
                                                 # (PRIOR_JUDGE_SCORES).

Architecture: this script does NOT reimplement engine selection or the
glossary-vs-fast wiring -- it drives the same glosa.web.app.make_engine_factory
(kind "fast"/"glossary" picked from Talk.engine by glosa/room.py itself,
untouched) that production uses, exactly like tests/test_room.py's
``test_live_translate_room_smoke``/``test_live_glossary_room`` (its "one
real RoomWorker per run" approach is reused here rather than reinvented; see
the task brief). ``engine_mode`` on the Settings passed to
make_engine_factory is "fake" for the dry run (FakeEngine, no key needed)
and "live" for ``--live`` (the real Gemini APIs, gemini_api_key loaded from
.env -- see ``resolve_api_key()`` -- and NEVER printed/logged/written to
any file this script produces; every commit greps its staged diff for the
Gemini key's literal prefix, per the task brief).

Every published CaptionMsg (glosa/captions/bus.py) is recorded, via a
CaptionBus.publish spy, with its arrival time on a RealClock started just
before the room run begins (so ``t`` is ~seconds since the audio started,
directly comparable to the samples/reference/*.json3 timings, which are
already re-based to start at 0 at the clip's start -- see
samples/README.md's "Caption references" section). That recording is the
whole of bench/raw/<clip>_<engine>.jsonl; a *.meta.json sidecar next to it
carries the run's cost/audio duration (not derivable from the caption
messages alone) so results.md's numbers can be recomputed from bench/raw/
without spending anything (only the quality/reference-translation numbers
need the network to redo).

Latency method (Task 15c fix round 1: **progress-lag**, see
bench/json3.py's module docstring and
.superpowers/sdd/2026-09-24-glosa/task-15c-fix1.md -- this replaces the
original ``match_next`` "earliest unclaimed event at/after each reference
utterance's end" metric, which measured event density, not latency, and
produced implausible numbers, e.g. a "26 s" source latency for the
glossary engine independently measured at ~0.9 s): for each track,
``bench.json3.progress_lag`` compares the cumulative-words-spoken curve
(from the json3 word timings) against the cumulative-words-on-screen curve
(replayed from that track's recorded append/set/close events), both
normalised by their own final totals, and reports p50/p90 of the lag at
progress points 5%, 10%, ..., 95%. Source latency uses the source-language
track; translation latency the target-language track. Investigating this
fix also settled a second inconsistency the original report carried: it
claimed "fast never publishes a source-language track" while its own
results table showed a source-latency number for fast anyway. The raw
recordings settle it -- fast (Live Translate) DOES publish a
source-language track (see e.g. bench/raw/en_clip_fast.jsonl's 'en'
append/close events, alongside its 'es' translation track): both engines
get a real source-latency number now.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = str(Path(__file__).resolve().parent)  # == ROOT / "bench"
# Running `python bench/bench.py` directly makes Python auto-insert this
# file's own directory (ROOT/bench) as sys.path[0]. Since that directory
# itself contains this very file (bench.py), leaving it in sys.path makes
# `import bench` resolve to THIS SCRIPT as a plain top-level module (a
# regular-module match anywhere in sys.path wins over the namespace-package
# match at ROOT/bench, PEP 420) instead of the bench/ package -- breaking
# every `from bench.<module> import ...` below (and self-importing this
# file recursively). Drop it before adding ROOT, so `bench` resolves as the
# package.
while _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
sys.path.insert(0, str(ROOT))  # `glosa` and `bench` importable when run as a bare script

from dotenv import dotenv_values  # noqa: E402
from google import genai  # noqa: E402

from bench.json3 import full_text, load_words, progress_lag, screen_curve, spoken_curve  # noqa: E402
from bench.judge import JUDGE_MODEL, build_reference_translation, judge_translation  # noqa: E402
from bench.report import RunStats, render_table  # noqa: E402
from bench.terms import Term, load_terms, overall_pct, score_terms  # noqa: E402
from glosa.captions.bus import CaptionBus  # noqa: E402
from glosa.clock import RealClock  # noqa: E402
from glosa.config import RoomCfg, Settings  # noqa: E402
from glosa.db import init_db  # noqa: E402
from glosa.models import GlossaryTerm, Room, Talk  # noqa: E402
from glosa.room import RoomWorker  # noqa: E402
from glosa.web.app import make_engine_factory  # noqa: E402

SAMPLES_DIR = ROOT / "samples"
TERMS_YAML = Path(__file__).with_name("terms.yaml")
RAW_DIR = Path(__file__).with_name("raw")
REFERENCE_DIR = Path(__file__).with_name("reference")
RESULTS_MD = Path(__file__).with_name("results.md")

SPEND_CAP_USD = 0.50  # hard cap for the whole task (bench + judge), per the brief
DRY_RUN_TIMEOUT_S = 15.0  # the dry run only needs to see the harness produce output
LIVE_RUN_TIMEOUT_S = 220.0  # ~93s clip + 5s tail + generous network/startup slack

FAST_PRICE_NOTE = "gemini-3.5-live-translate-preview"
GLOSSARY_TRANSCRIBE_NOTE = "gemini-3.5-transcribe-live"
GLOSSARY_TRANSLATE_NOTE = "gemini-3.5-flash-lite"

LANG_NAMES = {"en": "English", "es": "Spanish"}
ENGINES = ("fast", "glossary")

CLIPS = [
    {
        "name": "en_clip",
        "audio": SAMPLES_DIR / "en_clip.opus",
        "reference_json3": SAMPLES_DIR / "reference" / "en_clip.en.json3",
        "source_lang": "en",
        "target_lang": "es",
    },
    {
        "name": "es_clip",
        "audio": SAMPLES_DIR / "es_clip.opus",
        "reference_json3": SAMPLES_DIR / "reference" / "es_clip.es.json3",
        "source_lang": "es",
        "target_lang": "en",
    },
]
CLIPS_BY_NAME = {clip["name"]: clip for clip in CLIPS}


def resolve_api_key() -> str:
    """GEMINI_API_KEY from the worktree's own .env, else the main checkout's
    (this worktree, glosa-wt/t15c-bench, has none of its own -- the brief
    points at the main repo's). Never printed/logged."""
    candidates = [ROOT / ".env", ROOT.parent.parent / "glosa" / ".env"]
    for path in candidates:
        if path.is_file():
            key = (dotenv_values(path) or {}).get("GEMINI_API_KEY")
            if key:
                return key
    tried = ", ".join(str(p) for p in candidates)
    raise SystemExit(f"GEMINI_API_KEY not found (tried: {tried})")


def to_glossary_terms(terms: list[Term]) -> list[GlossaryTerm]:
    """bench/terms.yaml's terms -> the Talk.glossary the room actually uses.
    A term meant to be translated (``keep_in_english`` False) needs an
    explicit ``translation``: glosa/text/translator.py's
    ``_build_system_instruction`` treats keep_in_english=False *without*
    one the same as keep_in_english=True -- "leave it as is, untranslated"
    -- so a bare ``GlossaryTerm(term, False)`` silently tells the glossary
    engine's translator to keep that term in English, the opposite of what
    bench/terms.yaml's ``targets`` (the accepted translated spellings)
    means. Its first accepted target spelling is used as that translation."""
    return [
        GlossaryTerm(t.term, t.keep_in_english, translation=None if t.keep_in_english else t.targets[0])
        for t in terms
    ]


def build_room_and_talk(clip: dict, engine: str, glossary: list[GlossaryTerm]) -> tuple[Room, Talk]:
    rid = f"bench-{clip['name']}-{engine}"
    room = Room(
        id=rid, slug=rid, name=f"Bench {clip['name']} ({engine})", source_type="file",
        source_url=str(clip["audio"]), mode="auto", public_token=f"tok-{rid}",
        default_targets=[clip["target_lang"]],
    )
    start = datetime(2026, 9, 24, tzinfo=timezone.utc)
    talk = Talk(
        id=f"{clip['name']}-{engine}", room_id=rid, title=f"Bench {clip['name']} ({engine})",
        speakers=[], language=clip["source_lang"], targets=[clip["target_lang"]], engine=engine,
        start=start, end=start + timedelta(hours=1), abstract="", tags=[],
        glossary=glossary, status="scheduled", actual_start=None, actual_end=None,
    )
    return room, talk


def build_settings(api_key: str, engine_mode: str, room: Room, clip: dict) -> Settings:
    return Settings(
        gemini_api_key=api_key,
        admin_password="bench-password-ok",
        engine_mode=engine_mode,  # type: ignore[arg-type]
        rooms=[
            RoomCfg(
                id=room.id, name=room.name, source_type="file", source_url=room.source_url,
                default_targets=[clip["target_lang"]], language=clip["source_lang"],
            )
        ],
    )


class RunResult:
    def __init__(self, events: list[dict], cost_usd: float, audio_s: float, wall_s: float, translated_text: str):
        self.events = events
        self.cost_usd = cost_usd
        self.audio_s = audio_s
        self.wall_s = wall_s
        self.translated_text = translated_text


async def run_one(clip: dict, engine: str, terms: list[Term], api_key: str, *, live: bool, timeout_s: float) -> RunResult:
    """One (clip, engine) combination through a real RoomWorker + real
    AudioIngest, at real-time pace, recording every bus message. ``live``
    False: engine_mode "fake" (FakeEngine replaying a canned fixture, no
    network) -- only used by the dry run, to prove this function's wiring."""
    glossary = to_glossary_terms(terms)
    room, talk = build_room_and_talk(clip, engine, glossary)
    settings = build_settings(api_key, "live" if live else "fake", room, clip)
    clock = RealClock()
    bus = CaptionBus(clock=clock)
    events: list[dict] = []
    orig_publish = bus.publish

    def spy_publish(room_id, lang, type, **payload):  # noqa: A002
        msg = orig_publish(room_id, lang, type, **payload)
        events.append({"t": round(clock.now(), 3), "lang": lang, "type": type, **payload})
        return msg

    bus.publish = spy_publish  # type: ignore[method-assign]
    tmp_dir = tempfile.mkdtemp(prefix=f"glosa-bench-{clip['name']}-{engine}-")
    db = init_db(Path(tmp_dir) / "bench.db")
    engine_factory = make_engine_factory(settings, clock)
    worker = RoomWorker(room, settings, bus, db, clock, engine_factory, realtime=live)
    wall_start = time.monotonic()
    try:
        async def run() -> None:
            await worker.start(talk)
            while worker.talk is not None:
                await asyncio.sleep(0.2)

        await asyncio.wait_for(run(), timeout=timeout_s)
    finally:
        await worker.stop()
    wall_s = time.monotonic() - wall_start
    audio_s = worker.audio_s
    cost_usd = await db.total_cost()
    translated = await db.get_segments(talk.id, clip["target_lang"], "live")
    translated_text = " ".join(s.text for s in translated if s.text)
    db.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return RunResult(events=events, cost_usd=cost_usd, audio_s=audio_s, wall_s=wall_s, translated_text=translated_text)


def write_raw(clip_name: str, engine: str, result: RunResult) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{clip_name}_{engine}.jsonl"
    with raw_path.open("w", encoding="utf-8") as f:
        for event in result.events:
            f.write(json.dumps(event, default=str) + "\n")
    meta_path = RAW_DIR / f"{clip_name}_{engine}.meta.json"
    meta_path.write_text(
        json.dumps(
            {"cost_usd": result.cost_usd, "audio_s": result.audio_s, "wall_s": round(result.wall_s, 2)}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


async def get_reference_translation(client, clip: dict, source_text: str) -> tuple[str, float]:
    """The ONE cached full-text reference translation for ``clip`` (task
    brief: made once, reused by every engine's judge call). Cached under
    bench/reference/ -- a re-run only pays for it once."""
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = REFERENCE_DIR / f"{clip['name']}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8"), 0.0
    text, usd = await build_reference_translation(
        client, source_text, LANG_NAMES[clip["source_lang"]], LANG_NAMES[clip["target_lang"]]
    )
    path.write_text(text + "\n", encoding="utf-8")
    return text, usd


def parse_only(only: list[str] | None) -> list[tuple[str, str]]:
    """--only clip:engine (repeatable) -> the filtered list of (clip_name,
    engine) pairs to run; empty/None -> every combination."""
    all_combos = [(clip["name"], engine) for clip in CLIPS for engine in ENGINES]
    if not only:
        return all_combos
    wanted = set()
    for item in only:
        name, _, engine = item.partition(":")
        wanted.add((name, engine))
    return [combo for combo in all_combos if combo in wanted]


async def dry_run(combos: list[tuple[str, str]]) -> None:
    """engine_mode fake, a generous-but-bounded wait (not the full
    real-time clip): proves RoomWorker + make_engine_factory + the bus spy
    produce caption messages end to end, before any API key is spent."""
    print(f"dry run (engine_mode=fake, no network) for {len(combos)} combo(s)")
    for clip_name, engine in combos:
        clip = CLIPS_BY_NAME[clip_name]
        terms = load_terms(TERMS_YAML, clip_name)
        result = await run_one(clip, engine, terms, "dry-run-key", live=False, timeout_s=DRY_RUN_TIMEOUT_S)
        assert result.events, f"{clip_name}/{engine}: no bus messages recorded"
        print(
            f"  OK {clip_name}/{engine}: {len(result.events)} bus messages, "
            f"cost=${result.cost_usd:.4f} (fake), audio_s={result.audio_s}"
        )
    print("dry run OK: harness wiring proven end to end, no API key touched")


def recommendation(all_stats: list[RunStats]) -> str:
    """A data-driven paragraph (the numbers actually observed this run),
    not the controller's final call -- config.example.yaml is untouched."""
    by_engine: dict[str, list[RunStats]] = {"fast": [], "glossary": []}
    for s in all_stats:
        by_engine[s.engine].append(s)

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else float("nan")

    fast, glossary = by_engine["fast"], by_engine["glossary"]
    lines = []
    if fast and glossary:
        lines.append(
            f"Averaged over both clips: fast scored fidelity {avg([s.fidelity for s in fast]):.1f}/5, "
            f"fluency {avg([s.fluency for s in fast]):.1f}/5, {avg([s.term_pct for s in fast]):.0f}% glossary "
            f"terms, at ${avg([s.cost_per_hour for s in fast]):.2f}/h. Glossary scored fidelity "
            f"{avg([s.fidelity for s in glossary]):.1f}/5, fluency {avg([s.fluency for s in glossary]):.1f}/5, "
            f"{avg([s.term_pct for s in glossary]):.0f}% glossary terms, at "
            f"${avg([s.cost_per_hour for s in glossary]):.2f}/h."
        )
        fast_trans_p50 = [s.trans_lat_p50 for s in fast if s.trans_lat_p50 is not None]
        glossary_trans_p50 = [s.trans_lat_p50 for s in glossary if s.trans_lat_p50 is not None]
        fast_trans_p90 = [s.trans_lat_p90 for s in fast if s.trans_lat_p90 is not None]
        glossary_trans_p90 = [s.trans_lat_p90 for s in glossary if s.trans_lat_p90 is not None]
        if fast_trans_p50 and glossary_trans_p50:
            lines.append(
                f"Translation progress-lag (avg p50/p90 across both clips): fast {avg(fast_trans_p50):.2f}s / "
                f"{avg(fast_trans_p90):.2f}s, glossary {avg(glossary_trans_p50):.2f}s / "
                f"{avg(glossary_trans_p90):.2f}s -- on this run, glossary's median lag is actually at or "
                "below fast's on both clips (its two-stage pipeline, transcribe then translate, does not "
                "show up as a clearly worse p50 here), but its p90 tail is consistently worse than fast's on "
                "both clips; read both numbers, not just the median."
            )
    lines.append(
        "This is one run of ~93 s clips each, not a statistically robust sample -- read the deltas as a "
        "signal for the controller's call on `default_engine_en`, not as a verdict on their own."
    )
    return " ".join(lines)


def sanity_check_note(clip: dict, words: list, events: list[dict]) -> str:
    """Task 15c fix round 1's required sanity check: print the json3
    reference's first 3 words' times against the first event on that
    clip's source-language caption track, for one clip -- proof the
    reference transcript and the recorded bus events share the same clock
    (t=0 at the clip's start, see samples/README.md), not a silent offset
    the progress-lag numbers would otherwise hide."""
    first_words = ", ".join(f"{w.text!r}@{w.start_s:.2f}s" for w in words[:3])
    source_times = sorted(
        e["t"] for e in events if e.get("lang") == clip["source_lang"] and e.get("type") in ("append", "set")
    )
    first_event = f"{source_times[0]:.2f}s" if source_times else "(no source-track events)"
    return (
        f"- **Sanity check (word-timing alignment, `{clip['name']}`)**: the json3 reference's first 3 "
        f"words land at {first_words}; the first event on its source-language ('{clip['source_lang']}') "
        f"caption track arrives at {first_event}. Both are seconds since the clip's own start (t=0) -- a "
        "large, implausible gap here would mean the reference and the recording are misaligned, not that "
        "the system is that slow.\n"
    )


def render_results_md(
    stats_by_clip: dict[str, list[RunStats]],
    errors: list[str],
    intro: str,
    sanity_check: str,
) -> str:
    all_stats = [s for stats in stats_by_clip.values() for s in stats]
    parts = [
        "# Task 15c: `fast` vs `glossary` engine bench\n",
        "One real `RoomWorker` run per (clip, engine) combination, real-time pace, real Gemini APIs "
        "(`--live`). Source: `bench/bench.py`; raw per-message recordings: `bench/raw/*.jsonl` "
        "(+ `*.meta.json` for cost/duration); cached reference translations: `bench/reference/*.txt`.\n",
        intro,
    ]
    for clip in CLIPS:
        stats = stats_by_clip.get(clip["name"]) or []
        if not stats:
            continue
        parts.append(
            f"## {clip['name']} ({LANG_NAMES[clip['source_lang']]} -> {LANG_NAMES[clip['target_lang']]})\n"
        )
        parts.append(render_table(stats))
    parts.append("## Method\n")
    parts.append(
        "- **Latency (progress-lag, Task 15c fix round 1)**: for each track, compare two cumulative "
        "curves -- S(t), words spoken by time t (json3 word timings) and C(t), words visible on that "
        "caption track at time t (replayed from the recorded `append`/`set`/`close` events; `set` "
        "*replaces* a segment's text rather than adding to it, and the running max keeps the curve "
        "monotone through any ASR revision). Each curve is normalised by its own final total (a "
        "translation is not the same length as its source), then lag(f) = t_C(f) - t_S(f) is measured at "
        "progress points f = 5%, 10%, ..., 95%; the table reports p50/p90 over those 19 points "
        "(`bench/json3.py`'s `progress_lag`). Source latency uses the source-language track; translation "
        "latency the target-language track. `—` means that track had no events at all. This replaces "
        "the original `match_next` metric (\"earliest unclaimed caption-bus event at or after each "
        "reference utterance's end\"), which measured event density, not latency, and produced "
        "implausible numbers (e.g. a 26s \"source latency\" for the glossary engine independently "
        "measured at ~0.9s) whenever a track emitted frequent interim events.\n"
        "- **`fast`'s source track**: the original report claimed fast \"never publishes a "
        "source-language track\" while its own results table showed a source-latency number for it "
        "anyway -- a genuine inconsistency. The raw recordings resolve it: fast (Live Translate) DOES "
        "publish a source-language track (e.g. `bench/raw/en_clip_fast.jsonl` has 101 `append` + 21 "
        "`close` events on its 'en' track, alongside 99 `append` + 21 `close` on 'es'); both engines get "
        "a real source-latency number in the table above.\n" + sanity_check + "- **Quality**: one "
        "reference translation per clip from the json3 transcript "
        f"(`{JUDGE_MODEL}`, thinking LOW), cached under `bench/reference/`; then the same model "
        "(thinking LOW, JSON output) scores each engine's full translated output against the "
        "source transcript and that reference, fidelity and fluency 1-5, with a one-line "
        "justification (`bench/judge.py`; the prompts are in that file, unchanged per run). This is a "
        "single run per (clip, engine): an earlier, uncommitted live attempt on `es_clip`/`glossary` had "
        "scored fidelity/fluency 4/4 on essentially the same output where the committed run scored 3/2 "
        "-- LLM-judge 1-5 scores on freeform paragraph translation are noisy at n=1; read fidelity/fluency "
        "as directional, not precise. Each run's one-line judge justification is not stored anywhere on "
        "disk (`bench/bench.py` only ever held it in memory before rendering this table) -- it is not "
        "recoverable now without re-running the judge, which this fix round does not do (no new API "
        "calls; recompute-from-raw only).\n"
        "- **Glossary terms**: `bench/terms.yaml` lists each clip's technical terms with accepted "
        "source/target spellings; `bench/terms.py` counts case/accent-insensitive substring "
        "occurrences in the source transcript and hits in the engine's translated output. Reported "
        "as % of source occurrences with a hit.\n"
        "- **Cost**: from each run's `bench/raw/*.meta.json` (`Database.total_cost()`, glosa/db.py, "
        "the same per-component cost accounting `RoomStatus.cost_usd` reads from, recorded at run "
        "time); $/h scales it by the audio actually processed (`audio_s`).\n"
    )
    parts.append("## Recommendation\n")
    parts.append(recommendation(all_stats) if all_stats else "No successful runs to compare.")
    if errors:
        parts.append("\n\n## Errors\n")
        parts.append("\n".join(f"- {e}" for e in errors))
    return "\n".join(parts) + "\n"


async def live_run(combos: list[tuple[str, str]]) -> None:
    api_key = resolve_api_key()
    client = genai.Client(api_key=api_key)
    spend = 0.0
    errors: list[str] = []
    stats_by_clip: dict[str, list[RunStats]] = {}
    clip_words: dict[str, list] = {}
    sanity_events: dict[str, list[dict]] = {}

    needed_clips = sorted({name for name, _ in combos})
    for clip_name in needed_clips:
        clip = CLIPS_BY_NAME[clip_name]
        words = load_words(clip["reference_json3"])
        clip_words[clip_name] = words
        source_text = full_text(words)
        spoken = spoken_curve(words)
        terms = load_terms(TERMS_YAML, clip_name)
        stats_by_clip.setdefault(clip_name, [])

        if spend >= SPEND_CAP_USD:
            errors.append(f"{clip_name}: skipped, spend cap already reached (${spend:.4f})")
            continue
        try:
            reference_translation, ref_usd = await get_reference_translation(client, clip, source_text)
            spend += ref_usd
            print(f"{clip_name}: reference translation ready (+${ref_usd:.4f}, total ${spend:.4f})")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{clip_name}: reference translation failed: {exc!r}")
            continue

        for engine in ENGINES:
            if (clip_name, engine) not in combos:
                continue
            if spend >= SPEND_CAP_USD:
                errors.append(f"{clip_name}/{engine}: skipped, spend cap already reached (${spend:.4f})")
                continue
            print(f"{clip_name}/{engine}: running live room ({clip['source_lang']} -> {clip['target_lang']})...")
            try:
                result = await run_one(clip, engine, terms, api_key, live=True, timeout_s=LIVE_RUN_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{clip_name}/{engine}: room run failed: {exc!r}")
                continue
            spend += result.cost_usd
            write_raw(clip_name, engine, result)
            sanity_events.setdefault(clip_name, result.events)
            print(
                f"{clip_name}/{engine}: room done, {len(result.events)} messages, "
                f"cost=${result.cost_usd:.4f} (total ${spend:.4f}), audio_s={result.audio_s}"
            )
            if spend > SPEND_CAP_USD:
                errors.append(f"{clip_name}/{engine}: spend cap exceeded (${spend:.4f}) -- stopping")
                break

            source_screen = screen_curve(result.events, clip["source_lang"])
            trans_screen = screen_curve(result.events, clip["target_lang"])
            src_p50, src_p90, src_n = progress_lag(spoken, source_screen)
            trans_p50, trans_p90, trans_n = progress_lag(spoken, trans_screen)

            try:
                fidelity, fluency, justification, judge_usd = await judge_translation(
                    client, source_text, reference_translation, result.translated_text,
                    LANG_NAMES[clip["source_lang"]], LANG_NAMES[clip["target_lang"]],
                )
                spend += judge_usd
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{clip_name}/{engine}: judge call failed: {exc!r}")
                fidelity, fluency, justification = 0, 0, f"judge call failed: {exc!r}"

            term_reports = score_terms(terms, source_text, result.translated_text)
            term_pct = overall_pct(term_reports)
            occurrences = sum(r.occurrences for r in term_reports.values())
            hits = sum(r.hits for r in term_reports.values())
            cost_per_hour = (result.cost_usd / result.audio_s * 3600) if result.audio_s else 0.0

            stats_by_clip[clip_name].append(
                RunStats(
                    clip=clip_name, engine=engine,
                    source_lat_p50=src_p50, source_lat_p90=src_p90, source_lat_n=src_n,
                    trans_lat_p50=trans_p50, trans_lat_p90=trans_p90, trans_lat_n=trans_n,
                    fidelity=fidelity, fluency=fluency, justification=justification,
                    term_pct=term_pct, term_occurrences=occurrences, term_hits=hits,
                    cost_usd=result.cost_usd, cost_per_hour=cost_per_hour,
                )
            )
            print(f"{clip_name}/{engine}: fidelity={fidelity} fluency={fluency} terms={term_pct:.0f}% (total spend ${spend:.4f})")

    sanity_clip_name = next((c["name"] for c in CLIPS if c["name"] in sanity_events), None)
    sanity = (
        sanity_check_note(CLIPS_BY_NAME[sanity_clip_name], clip_words[sanity_clip_name], sanity_events[sanity_clip_name])
        if sanity_clip_name
        else ""
    )
    intro = (
        f"Date: 2026-09-24. Models: fast engine `{FAST_PRICE_NOTE}`; glossary engine "
        f"`{GLOSSARY_TRANSCRIBE_NOTE}` (VERBATIM + customVocabulary) -> `{GLOSSARY_TRANSLATE_NOTE}` "
        f"translation (talk glossary as context); judge and reference translation `{JUDGE_MODEL}` "
        "(thinking LOW). Real spend this run: "
        f"US${spend:.4f} (cap US${SPEND_CAP_USD:.2f}).\n"
    )
    RESULTS_MD.write_text(render_results_md(stats_by_clip, errors, intro, sanity), encoding="utf-8")
    print(f"\nwrote {RESULTS_MD} -- total real spend this run: ${spend:.4f} (cap ${SPEND_CAP_USD:.2f})")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")


def reconstruct_final_text(events: list[dict], lang: str) -> str:
    """The final on-screen text of every segment on ``lang``'s track,
    concatenated in segment order -- purely from the recorded bus events
    (``append`` concatenates, ``set`` replaces, ``close`` carries no text;
    same replay rules as ``bench.json3.screen_curve``, but returning the
    settled text instead of a word-count curve). Used by ``recompute()`` to
    rebuild the ``translated_text`` bench.py's live run read back from the
    DB, without a DB or the network -- this is what the original run's own
    glossary-term scoring worked from."""
    seg_text: dict[object, str] = {}
    for event in sorted(events, key=lambda e: e.get("t", 0.0)):
        if event.get("lang") != lang:
            continue
        if event.get("type") == "append":
            seg_text[event.get("seg")] = seg_text.get(event.get("seg"), "") + (event.get("text") or "")
        elif event.get("type") == "set":
            seg_text[event.get("seg")] = event.get("text") or ""
    return " ".join(seg_text[seg] for seg in sorted(seg_text, key=lambda s: (s is None, s)))


# The first live run's own LLM-judge fidelity/fluency scores (commit
# 6dcfd6c's bench/results.md, https://ai.studio -- gemini-3.8-flash,
# thinking LOW): not recomputable from bench/raw/*.jsonl (the judge needs
# the network, which this fix round does not call -- see task-15c-fix1.md,
# "do NOT re-run the judge"). Each run's one-line justification text was
# never persisted to disk in the first place (bench.py only ever held it in
# memory before rendering the table); unavailable, noted in results.md.
PRIOR_JUDGE_SCORES: dict[tuple[str, str], tuple[int, int]] = {
    ("en_clip", "fast"): (4, 3),
    ("en_clip", "glossary"): (3, 2),
    ("es_clip", "fast"): (5, 4),
    ("es_clip", "glossary"): (3, 2),
}
JUSTIFICATION_NOT_SAVED = (
    "not saved by the original live run (bench.py held it only in memory); not recoverable without "
    "re-running the judge, which this fix round does not do"
)


def recompute_run(clip: dict, engine: str, terms: list[Term]) -> RunStats | None:
    """One (clip, engine) row, rebuilt purely from the committed
    bench/raw/<clip>_<engine>.jsonl + .meta.json and samples/reference/
    *.json3 -- no network, no RoomWorker. ``None`` if that combo's raw
    files are missing (nothing to recompute)."""
    raw_path = RAW_DIR / f"{clip['name']}_{engine}.jsonl"
    meta_path = RAW_DIR / f"{clip['name']}_{engine}.meta.json"
    if not raw_path.is_file() or not meta_path.is_file():
        return None
    events = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    words = load_words(clip["reference_json3"])
    spoken = spoken_curve(words)
    source_text = full_text(words)
    src_p50, src_p90, src_n = progress_lag(spoken, screen_curve(events, clip["source_lang"]))
    trans_p50, trans_p90, trans_n = progress_lag(spoken, screen_curve(events, clip["target_lang"]))

    translated_text = reconstruct_final_text(events, clip["target_lang"])
    term_reports = score_terms(terms, source_text, translated_text)
    term_pct = overall_pct(term_reports)
    occurrences = sum(r.occurrences for r in term_reports.values())
    hits = sum(r.hits for r in term_reports.values())

    fidelity, fluency = PRIOR_JUDGE_SCORES[(clip["name"], engine)]
    cost_usd = meta["cost_usd"]
    audio_s = meta["audio_s"]
    cost_per_hour = (cost_usd / audio_s * 3600) if audio_s else 0.0

    return RunStats(
        clip=clip["name"], engine=engine,
        source_lat_p50=src_p50, source_lat_p90=src_p90, source_lat_n=src_n,
        trans_lat_p50=trans_p50, trans_lat_p90=trans_p90, trans_lat_n=trans_n,
        fidelity=fidelity, fluency=fluency, justification=JUSTIFICATION_NOT_SAVED,
        term_pct=term_pct, term_occurrences=occurrences, term_hits=hits,
        cost_usd=cost_usd, cost_per_hour=cost_per_hour,
    )


def recompute() -> None:
    """Task 15c fix round 1: regenerate bench/results.md from the committed
    bench/raw/*.jsonl + *.meta.json + samples/reference/*.json3 -- no live
    API calls, no RoomWorker, no network. Latency (progress-lag), glossary
    terms and cost are recomputed for real; fidelity/fluency come from the
    original live run's own judge output (PRIOR_JUDGE_SCORES -- the judge
    itself is not re-run)."""
    stats_by_clip: dict[str, list[RunStats]] = {}
    sanity_clip_name: str | None = None
    sanity_words: list | None = None
    sanity_events: list[dict] | None = None
    for clip in CLIPS:
        terms = load_terms(TERMS_YAML, clip["name"])
        stats_by_clip[clip["name"]] = []
        for engine in ENGINES:
            stats = recompute_run(clip, engine, terms)
            if stats is None:
                print(f"{clip['name']}/{engine}: no bench/raw/*.jsonl -- skipped")
                continue
            stats_by_clip[clip["name"]].append(stats)
            print(
                f"{clip['name']}/{engine}: source_lag={stats.source_lat_p50}/{stats.source_lat_p90} "
                f"(n={stats.source_lat_n}) trans_lag={stats.trans_lat_p50}/{stats.trans_lat_p90} "
                f"(n={stats.trans_lat_n}) terms={stats.term_pct:.0f}%"
            )
            if sanity_clip_name is None:
                raw_path = RAW_DIR / f"{clip['name']}_{engine}.jsonl"
                sanity_events = [
                    json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()
                ]
                sanity_words = load_words(clip["reference_json3"])
                sanity_clip_name = clip["name"]

    sanity = (
        sanity_check_note(CLIPS_BY_NAME[sanity_clip_name], sanity_words, sanity_events) if sanity_clip_name else ""
    )
    intro = (
        f"Date: 2026-09-24 (original live run) / 2026-09-25 (this recompute). Models: fast engine "
        f"`{FAST_PRICE_NOTE}`; glossary engine `{GLOSSARY_TRANSCRIBE_NOTE}` (VERBATIM + customVocabulary) "
        f"-> `{GLOSSARY_TRANSLATE_NOTE}` translation (talk glossary as context); judge and reference "
        f"translation `{JUDGE_MODEL}` (thinking LOW). **Recomputed from bench/raw/*.jsonl -- no new API "
        f"calls this round** (Task 15c fix round 1: the latency metric was wrong, see Method below); "
        f"original live run's real spend: US$0.1601 (cap US${SPEND_CAP_USD:.2f}).\n"
    )
    RESULTS_MD.write_text(render_results_md(stats_by_clip, [], intro, sanity), encoding="utf-8")
    print(f"\nwrote {RESULTS_MD} -- recomputed from raw, no live API calls this round")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="spend real API credit (see SPEND_CAP_USD)")
    parser.add_argument(
        "--recompute", action="store_true",
        help="regenerate bench/results.md from committed bench/raw/*.jsonl -- no network, no spend",
    )
    parser.add_argument(
        "--only", action="append", default=None,
        help="clip:engine (e.g. en_clip:fast), repeatable; default: all 4 combinations",
    )
    args = parser.parse_args()
    if args.recompute:
        recompute()
        return
    combos = parse_only(args.only)
    if args.live:
        asyncio.run(live_run(combos))
    else:
        asyncio.run(dry_run(combos))


if __name__ == "__main__":
    main()
