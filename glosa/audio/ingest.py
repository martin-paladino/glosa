"""ffmpeg-backed audio ingest: turns a room's source (file/url/youtube/emitter)
into a stream of 100 ms AudioChunks, supervising the ffmpeg subprocess and
restarting it (with growing backoff) when it dies unexpectedly.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from typing import Literal

from glosa.clock import Clock
from glosa.models import AudioChunk

CHUNK_BYTES = 3200  # 100 ms @ 16 kHz mono s16le
CHUNK_S = 0.1

# "Supervisa el proceso y lo reinicia con espera de 1, 2, 4, 8 y 16 s, con 5
# intentos como maximo. Despues emite un error terminal."
RESTART_BACKOFFS_S = [1, 2, 4, 8, 16]
MAX_RESTARTS = 5

SourceType = Literal["file", "url", "youtube", "emitter"]


def resolve_youtube(url: str) -> str:
    """Resolve a YouTube watch URL to a direct, ffmpeg-playable media URL.

    Shells out to `yt-dlp -g -f bestaudio`, which prints the direct stream
    URL(s) to stdout; we take the first non-empty line.
    """
    result = subprocess.run(
        ["yt-dlp", "-g", "-f", "bestaudio", url],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    raise RuntimeError(f"yt-dlp returned no stream URL for {url!r}")


def build_ffmpeg_cmd(source_type: SourceType, source_url: str, realtime: bool) -> list[str]:
    """Build the ffmpeg argv that decodes `source_url` to raw PCM on stdout.

    -hide_banner -loglevel error [-re] -i SRC -vn -ac 1 -ar 16000 -f s16le -
    """
    src = resolve_youtube(source_url) if source_type == "youtube" else source_url

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if realtime:
        cmd.append("-re")
    cmd += ["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    return cmd


class AudioIngest:
    """Supervises an ffmpeg subprocess and yields AudioChunks from its stdout.

    On an unexpected ffmpeg failure (non-zero exit, or failure to even start)
    the process is restarted with growing backoff (1, 2, 4, 8, 16 s), up to
    MAX_RESTARTS times; `restarts` counts how many restarts have happened and
    `last_error` holds the most recent failure's message. After the last
    retry also fails, chunks() ends (a "terminal error": last_error stays
    set, no more chunks are yielded). A clean ffmpeg exit (returncode 0, e.g.
    a finite input file finished decoding) ends chunks() normally, with no
    restart and last_error left as None.

    For source_type == "youtube", the URL is re-resolved (via
    resolve_youtube, through build_ffmpeg_cmd) on every attempt, including
    restarts, since a previously resolved direct URL may have expired.
    """

    def __init__(
        self,
        source_type: SourceType,
        source_url: str,
        realtime: bool,
        clock: Clock,
    ) -> None:
        self.source_type = source_type
        self.source_url = source_url
        self.realtime = realtime
        self.clock = clock

        self.restarts = 0
        self.last_error: str | None = None

        self._t = 0.0

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        attempt = 0
        while True:
            error = None
            try:
                # build_ffmpeg_cmd (and, for youtube, resolve_youtube) shells
                # out synchronously; run it off the event loop so one room
                # resolving a URL doesn't stall every other room's pipeline.
                # A resolution failure (e.g. yt-dlp couldn't resolve the
                # URL) is treated the same as an ffmpeg start/exit failure:
                # it flows into the same backoff/retry/terminal-error path
                # below, instead of crashing chunks() outright.
                cmd = await asyncio.to_thread(
                    build_ffmpeg_cmd, self.source_type, self.source_url, self.realtime
                )
            except Exception as exc:
                error = f"failed to resolve source: {exc}"
            else:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    error = f"failed to start {cmd[0]!r}: {exc}"
                else:
                    assert proc.stdout is not None
                    try:
                        try:
                            while True:
                                data = await proc.stdout.readexactly(CHUNK_BYTES)
                                yield AudioChunk(pcm=data, t=self._t)
                                self._t = round(self._t + CHUNK_S, 2)
                        except asyncio.IncompleteReadError:
                            pass  # EOF; any trailing partial (< CHUNK_BYTES) is dropped

                        returncode = await proc.wait()
                        if returncode == 0:
                            return  # clean end of stream
                        stderr = b""
                        if proc.stderr is not None:
                            stderr = await proc.stderr.read()
                        error = stderr.decode(errors="replace").strip() or f"{cmd[0]} exited {returncode}"
                    finally:
                        # If we were cancelled/abandoned mid-stream (caller
                        # stopped iterating), don't leave ffmpeg running.
                        if proc.returncode is None:
                            proc.kill()
                            await proc.wait()

            self.last_error = error
            if attempt >= MAX_RESTARTS:
                return  # terminal error: last_error stays set, no more chunks

            wait_s = RESTART_BACKOFFS_S[attempt]
            attempt += 1
            self.restarts = attempt
            await self.clock.sleep(wait_s)
