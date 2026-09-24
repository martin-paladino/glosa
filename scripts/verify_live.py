#!/usr/bin/env python3
"""Live verification of the Gemini Live Translate API. This is NOT a pytest
test (it spends real API budget) — run it manually, see T0.5 in
.superpowers/sdd/2026-09-24-glosa/task-0-brief.md.

It downloads short clips of real conference talks with the project's yt-dlp,
streams them to a `gemini-3.5-live-translate-preview` session at real-time
pace (PCM16 mono 16kHz, 100ms / 3200-byte chunks, per globals.md), and
records every server message to a JSONL file with its arrival time, so we
know: which config fields actually work, when GoAway arrives and with what
timeLeft, whether echoTargetLanguage actually un-mutes target-language
audio, and what a session's natural lifetime/usage looks like.

Modes:
    uv run python scripts/verify_live.py smoke
        ~30s of the EN long clip, target=es, echo=True. Confirms the SDK
        field names, model name and event shapes all work before spending
        real minutes of Live Translate time. Writes data/verify/smoke.*

    uv run python scripts/verify_live.py echo
        ~2min of the ES clip, target=es, run twice: echoTargetLanguage=True
        then False. Confirms whether target-language input still produces
        text with echo enabled vs. disabled. Writes data/verify/echo_*.

    uv run python scripts/verify_live.py full
        ~26min of the EN long clip, target=es, echo=True, one session.
        Records ALL messages (with timing) to samples/fixtures/lt_en.jsonl
        and a human log to data/verify/verify_en.log. Meant to be launched
        detached (nohup) since it runs for ~25+ minutes:

            nohup uv run python scripts/verify_live.py full \\
                > data/verify/verify_en.nohup.out 2>&1 &
            disown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # allow `import glosa` when run as a bare script

from glosa.config import Settings  # noqa: E402

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

LOG = logging.getLogger("verify_live")

MODEL = "gemini-3.5-live-translate-preview"
CHUNK_BYTES = 3200  # 100ms of PCM16 mono @ 16kHz, per globals.md
CHUNK_S = 0.1
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono

EN_LONG_URL = "https://www.youtube.com/watch?v=wyGMy5ic7PE"
EN_LONG_START_S = 5 * 60
EN_LONG_DURATION_S = 26 * 60

ES_ECHO_URL = "https://www.youtube.com/watch?v=VJCrOw2uxbk"
ES_ECHO_START_S = 5 * 60
ES_ECHO_DURATION_S = 2 * 60

SMOKE_DURATION_S = 30

SAMPLES_LONG = ROOT / "samples" / "long"
FIXTURES_DIR = ROOT / "samples" / "fixtures"
VERIFY_DATA_DIR = ROOT / "data" / "verify"


# --------------------------------------------------------------------------
# Clip download (project's yt-dlp, not the outdated system one) + transcode
# --------------------------------------------------------------------------


def download_clip(url: str, start_s: int, duration_s: int, dest_stem: Path) -> Path:
    """Download [start_s, start_s+duration_s) of url as bestaudio via `uv run
    yt-dlp`, then transcode to raw PCM16 mono 16kHz with ffmpeg. Idempotent:
    reuses dest_stem.pcm if it already exists and is non-empty.
    """
    pcm_path = dest_stem.with_suffix(".pcm")
    if pcm_path.exists() and pcm_path.stat().st_size > 0:
        duration = pcm_path.stat().st_size / BYTES_PER_SECOND
        LOG.info("using cached %s (%.1fs of audio)", pcm_path, duration)
        return pcm_path

    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    end_s = start_s + duration_s
    section = f"*{start_s}-{end_s}"
    raw_template = f"{dest_stem}.%(ext)s"

    LOG.info("downloading %s section %ss-%ss via `uv run yt-dlp`", url, start_s, end_s)
    subprocess.run(
        [
            "uv",
            "run",
            "yt-dlp",
            "-f",
            "bestaudio",
            "--download-sections",
            section,
            "-o",
            raw_template,
            "--no-playlist",
            url,
        ],
        cwd=str(ROOT),
        check=True,
    )

    candidates = sorted(
        p for p in dest_stem.parent.glob(dest_stem.name + ".*") if p.suffix != ".pcm"
    )
    if not candidates:
        raise RuntimeError(f"yt-dlp did not produce an audio file for {dest_stem}")
    src = candidates[0]

    LOG.info("transcoding %s -> %s (pcm16 mono 16kHz)", src, pcm_path)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            str(pcm_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    duration = pcm_path.stat().st_size / BYTES_PER_SECOND
    LOG.info("ready: %s (%.1fs of audio)", pcm_path, duration)
    return pcm_path


# --------------------------------------------------------------------------
# One Live Translate session: stream audio in, record every message out
# --------------------------------------------------------------------------


class LiveTranslateProbe:
    """Opens one Live Translate session, streams a PCM clip at real-time
    pace, and records every server message with its arrival time.
    """

    def __init__(
        self,
        client: genai.Client,
        target_lang: str,
        echo_target_language: bool,
        jsonl_path: Path,
        log_path: Path,
    ) -> None:
        self.client = client
        self.target_lang = target_lang
        self.echo_target_language = echo_target_language
        self.jsonl_path = jsonl_path
        self.log_path = log_path
        self.t0 = 0.0
        self.counts: dict[str, int] = {}
        self.go_away: tuple[float, str | None] | None = None

        for path in (jsonl_path, log_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def _record(self, kind: str, text: str = "", raw_type: str = "", **meta: object) -> None:
        t = time.monotonic() - self.t0
        self.counts[kind] = self.counts.get(kind, 0) + 1
        line = {"t": round(t, 3), "kind": kind, "text": text, "raw_type": raw_type}
        if meta:
            line["meta"] = meta
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _log(self, msg: str) -> None:
        line = f"[{time.monotonic() - self.t0:8.2f}s] {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _handle_message(self, msg: types.LiveServerMessage) -> None:
        sc = msg.server_content
        if sc is not None:
            it = sc.input_transcription
            if it is not None and it.text:
                kind = "source_final" if it.finished else "source_delta"
                self._record(
                    kind,
                    text=it.text,
                    raw_type="server_content.input_transcription",
                    lang=it.language_code,
                    finished=it.finished,
                )
            ot = sc.output_transcription
            if ot is not None and ot.text:
                kind = "target_final" if ot.finished else "target_delta"
                self._record(
                    kind,
                    text=ot.text,
                    raw_type="server_content.output_transcription",
                    lang=ot.language_code,
                    finished=ot.finished,
                )
            if sc.turn_complete:
                self._record("turn_complete", raw_type="server_content.turn_complete")
            if sc.interrupted:
                self._record("interrupted", raw_type="server_content.interrupted")
        if msg.go_away is not None:
            time_left = msg.go_away.time_left
            elapsed = time.monotonic() - self.t0
            self.go_away = (elapsed, time_left)
            self._record("go_away", raw_type="go_away", time_left_raw=time_left)
            self._log(f"GoAway received: time_left={time_left}")
        if msg.usage_metadata is not None:
            um = msg.usage_metadata
            self._record(
                "usage",
                raw_type="usage_metadata",
                prompt_tokens=um.prompt_token_count,
                response_tokens=um.response_token_count,
                total_tokens=um.total_token_count,
            )
        if msg.setup_complete is not None:
            self._record("setup_complete", raw_type="setup_complete")
        if msg.session_resumption_update is not None:
            self._record("session_resumption_update", raw_type="session_resumption_update")

    async def run(self, pcm_path: Path, max_wall_s: float) -> None:
        self.t0 = time.monotonic()
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=self.target_lang,
                echo_target_language=self.echo_target_language,
            ),
        )
        self._log(
            f"connecting model={MODEL} target={self.target_lang} "
            f"echo={self.echo_target_language} clip={pcm_path.name}"
        )
        try:
            async with self.client.aio.live.connect(model=MODEL, config=config) as session:
                self._log("connected")
                send_done = asyncio.Event()

                async def sender() -> None:
                    try:
                        with pcm_path.open("rb") as f:
                            next_send = time.monotonic()
                            sent_s = 0.0
                            while True:
                                chunk = f.read(CHUNK_BYTES)
                                if not chunk:
                                    break
                                if time.monotonic() - self.t0 > max_wall_s:
                                    self._log("sender: hit max_wall_s, stopping early")
                                    break
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        data=chunk, mime_type=f"audio/pcm;rate={SAMPLE_RATE}"
                                    )
                                )
                                sent_s += len(chunk) / BYTES_PER_SECOND
                                next_send += CHUNK_S
                                delay = next_send - time.monotonic()
                                if delay > 0:
                                    await asyncio.sleep(delay)
                        await session.send_realtime_input(audio_stream_end=True)
                        self._log(f"sender: streamed {sent_s:.1f}s of audio, sent audio_stream_end")
                    except Exception as exc:  # noqa: BLE001 - log and let receiver observe the close
                        self._log(f"sender: stopped ({exc.__class__.__name__}: {exc})")
                    finally:
                        send_done.set()

                async def receiver() -> None:
                    try:
                        while True:
                            async for msg in session.receive():
                                self._handle_message(msg)
                            if send_done.is_set():
                                break
                    except Exception as exc:  # noqa: BLE001
                        code = getattr(exc, "code", None)
                        self._record(
                            "closed",
                            text=str(exc),
                            raw_type=exc.__class__.__name__,
                            code=code,
                        )
                        self._log(f"receiver: session closed ({exc.__class__.__name__}: {exc})")

                sender_task = asyncio.create_task(sender())
                receiver_task = asyncio.create_task(receiver())
                await asyncio.wait(
                    {sender_task, receiver_task},
                    timeout=max_wall_s + 15,
                    return_when=asyncio.ALL_COMPLETED,
                )
                for task in (sender_task, receiver_task):
                    if not task.done():
                        task.cancel()
        except Exception as exc:  # noqa: BLE001 - connect() itself failed
            self._record("error", text=str(exc), raw_type=exc.__class__.__name__)
            self._log(f"connect/session error: ({exc.__class__.__name__}: {exc})")
            raise
        finally:
            self._record("closed", raw_type="probe_finished")
            elapsed = time.monotonic() - self.t0
            cost_usd = (elapsed / 60.0) * Settings.load(
                env_path=str(ROOT / ".env"), config_path=str(ROOT / "config.yaml")
            ).prices.lt_per_min
            self._log(
                f"done. elapsed={elapsed:.1f}s counts={self.counts} "
                f"go_away={self.go_away} est_cost_usd={cost_usd:.4f}"
            )


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def build_client() -> genai.Client:
    settings = Settings.load(env_path=str(ROOT / ".env"), config_path=str(ROOT / "config.yaml"))
    return genai.Client(api_key=settings.gemini_api_key)


async def cmd_smoke(client: genai.Client) -> None:
    pcm = download_clip(EN_LONG_URL, EN_LONG_START_S, SMOKE_DURATION_S, SAMPLES_LONG / "en_smoke")
    probe = LiveTranslateProbe(
        client,
        target_lang="es",
        echo_target_language=True,
        jsonl_path=VERIFY_DATA_DIR / "smoke.jsonl",
        log_path=VERIFY_DATA_DIR / "smoke.log",
    )
    await probe.run(pcm, max_wall_s=SMOKE_DURATION_S + 15)
    print(f"\nSMOKE RESULT: counts={probe.counts} go_away={probe.go_away}")


async def cmd_echo(client: genai.Client) -> None:
    pcm = download_clip(ES_ECHO_URL, ES_ECHO_START_S, ES_ECHO_DURATION_S, SAMPLES_LONG / "es_echo")
    results: dict[str, dict[str, int]] = {}
    for echo in (True, False):
        probe = LiveTranslateProbe(
            client,
            target_lang="es",
            echo_target_language=echo,
            jsonl_path=VERIFY_DATA_DIR / f"echo_{echo}.jsonl",
            log_path=VERIFY_DATA_DIR / f"echo_{echo}.log",
        )
        await probe.run(pcm, max_wall_s=ES_ECHO_DURATION_S + 20)
        results[str(echo)] = dict(probe.counts)

    print("\nECHO TEST RESULT (es audio -> target=es):")
    for echo, counts in results.items():
        target_text_seen = counts.get("target_delta", 0) + counts.get("target_final", 0)
        print(f"  echo_target_language={echo}: counts={counts} target_text_events={target_text_seen}")

    summary_path = VERIFY_DATA_DIR / "echo_summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"summary written to {summary_path}")


async def cmd_full(client: genai.Client) -> None:
    pcm = download_clip(EN_LONG_URL, EN_LONG_START_S, EN_LONG_DURATION_S, SAMPLES_LONG / "en_long")
    jsonl_path = FIXTURES_DIR / "lt_en.jsonl"
    log_path = VERIFY_DATA_DIR / "verify_en.log"
    probe = LiveTranslateProbe(
        client,
        target_lang="es",
        echo_target_language=True,
        jsonl_path=jsonl_path,
        log_path=log_path,
    )
    await probe.run(pcm, max_wall_s=EN_LONG_DURATION_S + 60)
    print(f"\nFULL RUN DONE: counts={probe.counts} go_away={probe.go_away}")
    print(f"fixture: {jsonl_path}")
    print(f"log: {log_path}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["smoke", "echo", "full"])
    args = parser.parse_args()

    client = build_client()
    if args.mode == "smoke":
        asyncio.run(cmd_smoke(client))
    elif args.mode == "echo":
        asyncio.run(cmd_echo(client))
    else:
        asyncio.run(cmd_full(client))


if __name__ == "__main__":
    main()
