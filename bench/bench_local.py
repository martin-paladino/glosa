"""bench/bench_local.py: Task 16 -- honest, real-time measurement of
``engine_mode: local`` (LocalParakeetEngine + LocalTranslator, MLX, no cloud
API) with 1 room then 2 rooms, on samples/en_clip.opus / samples/es_clip.opus.
Needs the "local" extra installed (``uv sync --extra local``, Apple silicon
only) and the two models already downloaded (glosa/engines/local.py's
PARAKEET_MODEL_ID, glosa/text/local_translator.py's TRANSLATEGEMMA_MODEL_ID
-- first run downloads them from Hugging Face, ~4.5 GB).

Drives a real RoomWorker per room (glosa.web.app.make_engine_factory,
realtime=True, real AudioIngest/ffmpeg) exactly like bench/bench.py's
``run_one`` does for the cloud engines -- reuses bench.json3's progress-lag
helpers (``load_words``/``spoken_curve``/``screen_curve``/``progress_lag``),
does not fork them. CPU%/RSS of this process are sampled every 0.5 s via
``ps`` (no new dependency); ``mlx.core.get_peak_memory()`` gives MLX's own
peak unified-memory use.

Usage:
    uv run python bench/bench_local.py --rooms 1
    uv run python bench/bench_local.py --rooms 2
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
while _SCRIPT_DIR in sys.path:  # see bench/bench.py's own comment: keep `bench` a package, not this script
    sys.path.remove(_SCRIPT_DIR)
sys.path.insert(0, str(ROOT))

from bench.json3 import load_words, progress_lag, screen_curve, spoken_curve  # noqa: E402
from glosa.captions.bus import CaptionBus  # noqa: E402
from glosa.clock import RealClock  # noqa: E402
from glosa.config import RoomCfg, Settings  # noqa: E402
from glosa.db import init_db  # noqa: E402
from glosa.models import Room, Talk  # noqa: E402
from glosa.room import RoomWorker  # noqa: E402
from glosa.web.app import make_engine_factory  # noqa: E402

CLIPS = [
    {
        "name": "en_clip", "audio": ROOT / "samples" / "en_clip.opus",
        "reference_json3": ROOT / "samples" / "reference" / "en_clip.en.json3",
        "source_lang": "en", "target_lang": "es", "talk_engine": "fast",  # local mode coerces this to glossary
    },
    {
        "name": "es_clip", "audio": ROOT / "samples" / "es_clip.opus",
        "reference_json3": ROOT / "samples" / "reference" / "es_clip.es.json3",
        "source_lang": "es", "target_lang": "en", "talk_engine": "glossary",
    },
]

TIMEOUT_S = 130.0  # ~93s clip + tail + generous slack for local model warmup mid-run


class CpuSampler:
    """Samples this process's %CPU and RSS (MB) via `ps` every `period_s`,
    in a background asyncio task -- no psutil dependency."""

    def __init__(self, period_s: float = 0.5) -> None:
        self.period_s = period_s
        self.samples: list[tuple[float, float]] = []  # (cpu_pct, rss_mb)
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        pid = str(__import__("os").getpid())
        while True:
            try:
                out = subprocess.run(
                    ["ps", "-o", "%cpu=,rss=", "-p", pid], capture_output=True, text=True, check=True
                ).stdout.strip()
                cpu_str, rss_str = out.split()
                # locale can format decimals with a comma (e.g. "0,0"); ps always uses '.' internally
                self.samples.append((float(cpu_str.replace(",", ".")), float(rss_str.replace(",", ".")) / 1024))
            except Exception:
                pass
            await asyncio.sleep(self.period_s)

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def summary(self) -> str:
        if not self.samples:
            return "no samples"
        cpu = [s[0] for s in self.samples]
        rss = [s[1] for s in self.samples]
        return f"CPU% avg={sum(cpu) / len(cpu):.0f} max={max(cpu):.0f} | RSS MB avg={sum(rss) / len(rss):.0f} max={max(rss):.0f}"


def build_room_and_talk(clip: dict) -> tuple[Room, Talk]:
    rid = f"local-{clip['name']}"
    room = Room(
        id=rid, slug=rid, name=f"Local bench {clip['name']}", source_type="file",
        source_url=str(clip["audio"]), mode="auto", public_token=f"tok-{rid}",
        default_targets=[clip["target_lang"]],
    )
    start = datetime(2026, 9, 25, tzinfo=timezone.utc)
    talk = Talk(
        id=f"{clip['name']}-local", room_id=rid, title=f"Local bench {clip['name']}", speakers=[],
        language=clip["source_lang"], targets=[clip["target_lang"]], engine=clip["talk_engine"],
        start=start, end=start + timedelta(hours=1), abstract="", tags=[], glossary=[],
        status="scheduled", actual_start=None, actual_end=None,
    )
    return room, talk


async def run_room(clip: dict, settings: Settings, clock: RealClock, engine_factory, db_path: Path) -> dict:
    room, talk = build_room_and_talk(clip)
    bus = CaptionBus(clock=clock)
    events: list[dict] = []
    orig_publish = bus.publish

    def spy_publish(room_id, lang, type, **payload):  # noqa: A002
        msg = orig_publish(room_id, lang, type, **payload)
        events.append({"t": round(clock.now(), 3), "lang": lang, "type": type, **payload})
        return msg

    bus.publish = spy_publish  # type: ignore[method-assign]
    db = init_db(db_path)
    worker = RoomWorker(room, settings, bus, db, clock, engine_factory, realtime=True)
    wall_start = time.monotonic()
    try:
        async def run() -> None:
            await worker.start(talk)
            while worker.talk is not None:
                await asyncio.sleep(0.2)

        await asyncio.wait_for(run(), timeout=TIMEOUT_S)
    finally:
        await worker.stop()
    wall_s = time.monotonic() - wall_start
    translated = await db.get_segments(talk.id, clip["target_lang"], "live")
    source_segs = await db.get_segments(talk.id, clip["source_lang"], "live")
    db.close()
    return {
        "events": events, "wall_s": wall_s, "audio_s": worker.audio_s,
        "translated": [s.text for s in translated if s.text],
        "source": [s.text for s in source_segs if s.text],
    }


async def main_async(n_rooms: int) -> None:
    clips = CLIPS[:n_rooms]
    tmp_dir = Path(tempfile.mkdtemp(prefix="glosa-bench-local-"))
    settings = Settings(
        gemini_api_key="unused-in-local-mode", admin_password="bench-password-not-real",
        engine_mode="local",
        rooms=[
            RoomCfg(
                id=f"local-{c['name']}", name=c["name"], source_type="file", source_url=str(c["audio"]),
                default_targets=[c["target_lang"]], language=c["source_lang"],
            )
            for c in clips
        ],
    )
    clock = RealClock()
    engine_factory = make_engine_factory(settings, clock)  # ONE factory: the shared MLX models, 1 or 2 rooms

    sampler = CpuSampler()
    sampler.start()
    print(f"local bench: {n_rooms} room(s) real-time, clips {[c['name'] for c in clips]}")
    t0 = time.monotonic()
    results = await asyncio.gather(
        *(run_room(clip, settings, clock, engine_factory, tmp_dir / f"{clip['name']}.db") for clip in clips)
    )
    total_wall_s = time.monotonic() - t0
    sampler.stop()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        import mlx.core as mx
        peak_mem_gb = mx.get_peak_memory() / 1e9
    except Exception:
        peak_mem_gb = None

    print(f"\n=== {n_rooms}-room run: total wall {total_wall_s:.1f}s ===")
    print(sampler.summary())
    if peak_mem_gb is not None:
        print(f"MLX peak unified memory: {peak_mem_gb:.2f} GB")

    for clip, result in zip(clips, results):
        words = load_words(clip["reference_json3"])
        spoken = spoken_curve(words)
        src_screen = screen_curve(result["events"], clip["source_lang"])
        trg_screen = screen_curve(result["events"], clip["target_lang"])
        src_p50, src_p90, src_n = progress_lag(spoken, src_screen)
        trg_p50, trg_p90, trg_n = progress_lag(spoken, trg_screen)
        print(f"\n--- {clip['name']} ({clip['source_lang']} -> {clip['target_lang']}) ---")
        print(f"audio_s={result['audio_s']:.1f} wall_s={result['wall_s']:.1f}")
        print(f"source latency  p50={src_p50} p90={src_p90} (n={src_n})")
        print(f"translation lag p50={trg_p50} p90={trg_p90} (n={trg_n})")
        print(f"source segments: {len(result['source'])}, translated segments: {len(result['translated'])}")
        print("first 10 translated segments:")
        for i, text in enumerate(result["translated"][:10]):
            print(f"  {i + 1}. {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rooms", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    asyncio.run(main_async(args.rooms))


if __name__ == "__main__":
    main()
