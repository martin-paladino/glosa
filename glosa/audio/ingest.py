"""ffmpeg-backed audio ingest: turns a room's source (file/url/youtube/emitter)
into a stream of 100 ms AudioChunks, supervising the ffmpeg subprocess and
restarting it (with growing backoff) when it dies unexpectedly.

For ``source_type == "emitter"`` (Task 14a: the room station), there is no
ffmpeg: ``glosa/web/station.py``'s WebSocket handler re-packs whatever the
station sends into 3200-byte PCM frames and pushes them onto a per-room
bounded queue on ``StationHub`` (``app.state.station_hub``, one instance
shared by every room); ``EmitterIngest.chunks()`` reads from that queue
instead of a subprocess, using the same ``Ingest`` interface
(``glosa/room.py``) as ``AudioIngest``.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from glosa.clock import Clock
from glosa.models import AudioChunk

CHUNK_BYTES = 3200  # 100 ms @ 16 kHz mono s16le
CHUNK_S = 0.1

# "Supervisa el proceso y lo reinicia con espera de 1, 2, 4, 8 y 16 s, con 5
# intentos como maximo. Despues emite un error terminal."
RESTART_BACKOFFS_S = [1, 2, 4, 8, 16]
MAX_RESTARTS = 5

SourceType = Literal["file", "url", "youtube", "emitter"]

# StationHub: how many 100 ms chunks the per-room queue holds (5 s) before it
# starts dropping the oldest one, and how long without audio (Ruling 38)
# before a room reports "station disconnected".
STATION_QUEUE_CHUNKS = 50
STATION_TIMEOUT_S = 5.0


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


class _StationSocket(Protocol):
    """The slice of ``starlette.websockets.WebSocket`` StationHub needs.
    Typed narrowly so glosa.audio (this module) doesn't import FastAPI/
    Starlette; glosa/web/station.py passes the real WebSocket in."""

    async def close(self, code: int = 1000) -> None: ...
    async def send_json(self, data: Any) -> None: ...


@dataclass
class StationInfo:
    """A room's station, as the admin sees it (RoomWorker.status()'s raw
    ``detail``, Ruling 29 -- never the public API)."""

    connected: bool
    device: str | None
    level_db: float | None
    last_audio_age_s: float | None


class _StationState:
    __slots__ = ("ws", "generation", "device", "level_db", "last_audio_at")

    def __init__(self) -> None:
        self.ws: _StationSocket | None = None
        self.generation = 0
        self.device: str | None = None
        self.level_db: float | None = None
        self.last_audio_at: float | None = None  # clock.now(), set on push_audio


class StationHub:
    """One per process (``app.state.station_hub``), shared by every
    ``emitter`` room: owns each room's bounded, drop-oldest audio queue and
    its single active station WebSocket (Ruling 38).

    ``connect``/``disconnect``/``push_audio``/``set_hello``/``set_level`` are
    called by glosa/web/station.py's WebSocket handler; ``queue`` and
    ``is_stale`` by EmitterIngest; ``reload`` by the admin API; ``info`` by
    RoomWorker.status() and, later, the admin panel (Task 12).
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self._state: dict[str, _StationState] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}

    def queue(self, room_id: str) -> asyncio.Queue[bytes]:
        queue = self._queues.get(room_id)
        if queue is None:
            queue = self._queues[room_id] = asyncio.Queue(maxsize=STATION_QUEUE_CHUNKS)
        return queue

    def _state_of(self, room_id: str) -> _StationState:
        state = self._state.get(room_id)
        if state is None:
            state = self._state[room_id] = _StationState()
        return state

    def _connect_lock(self, room_id: str) -> asyncio.Lock:
        # No `await` between the dict lookup and the (possible) insert, so
        # this is safe without its own lock: asyncio is single-threaded and
        # cooperative, and nothing here yields control mid-way.
        lock = self._connect_locks.get(room_id)
        if lock is None:
            lock = self._connect_locks[room_id] = asyncio.Lock()
        return lock

    async def connect(self, room_id: str, ws: _StationSocket) -> int:
        """Register ``ws`` as ``room_id``'s one active station, closing (4409)
        whatever was connected before (a reload, a second tab, a flaky
        network: the newest connection always wins). Returns a generation
        number: pass it to ``disconnect`` so a superseded connection's own
        cleanup can't clear the *new* one's state.

        Fix round 1, review #3: the whole body runs under a per-room lock.
        Without it, two connects arriving close together could both read
        the same (stale) ``state.ws`` before either had written its own
        socket in -- the loser's socket would then never be closed (4409)
        and would keep pushing audio into the queue forever, alongside the
        winner's."""
        async with self._connect_lock(room_id):
            state = self._state_of(room_id)
            old = state.ws
            if old is not None:
                with contextlib.suppress(Exception):
                    await old.close(code=4409)
            state.ws = ws
            state.generation += 1
            state.device = None
            state.level_db = None
            return state.generation

    def disconnect(self, room_id: str, generation: int) -> None:
        state = self._state.get(room_id)
        if state is not None and state.generation == generation:
            state.ws = None

    def push_audio(self, room_id: str, chunk: bytes) -> None:
        """Queue one already-repacked ``CHUNK_BYTES`` PCM frame, dropping the
        oldest queued one first if the queue is full (never blocks)."""
        self._state_of(room_id).last_audio_at = self._clock.now()
        queue = self.queue(room_id)
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait(chunk)

    def set_hello(self, room_id: str, device: str | None) -> None:
        self._state_of(room_id).device = device or None

    def set_level(self, room_id: str, db: float) -> None:
        self._state_of(room_id).level_db = db

    async def reload(self, room_id: str) -> bool:
        """Remote reload ("F5 remoto"): tell the connected station to
        reload. False (no-op) if none is connected."""
        state = self._state.get(room_id)
        if state is None or state.ws is None:
            return False
        await state.ws.send_json({"type": "reload"})
        return True

    def info(self, room_id: str) -> StationInfo:
        state = self._state.get(room_id)
        if state is None:
            return StationInfo(connected=False, device=None, level_db=None, last_audio_age_s=None)
        age = None if state.last_audio_at is None else max(0.0, self._clock.now() - state.last_audio_at)
        return StationInfo(connected=state.ws is not None, device=state.device, level_db=state.level_db, last_audio_age_s=age)

    def is_stale(self, room_id: str, timeout: float = STATION_TIMEOUT_S) -> bool:
        """No audio in the last ``timeout`` s -- including "never sent any",
        which counts as stale too: an emitter room with nobody connected
        yet is down, not merely quiet."""
        state = self._state.get(room_id)
        if state is None or state.last_audio_at is None:
            return True
        return (self._clock.now() - state.last_audio_at) >= timeout


class EmitterIngest:
    """Ingest for ``source_type == "emitter"``: reads already-repacked
    ``CHUNK_BYTES`` PCM frames from ``hub``'s queue for ``room_id`` instead
    of running ffmpeg. Same ``Ingest`` interface as AudioIngest
    (``restarts``, ``last_error``, ``chunks()``), plus ``stale()`` (not part
    of the ``Ingest`` protocol; RoomWorker checks for it with ``getattr``).

    Unlike AudioIngest, ``chunks()`` never ends on a quiet station: it just
    waits for the next chunk, so a reconnecting station resumes the same
    talk with no operator action (Ruling 38). RoomWorker instead polls
    ``stale()`` on its ticker and sets/clears the room's "source is down"
    state itself -- the same mechanism a dying AudioIngest uses, without
    tearing down and restarting the audio task.
    """

    def __init__(self, hub: StationHub, room_id: str, clock: Clock) -> None:
        self._hub = hub
        self._room_id = room_id
        self._t = 0.0

        self.restarts = 0
        self.last_error: str | None = None

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        queue = self._hub.queue(self._room_id)
        while True:
            pcm = await queue.get()
            yield AudioChunk(pcm=pcm, t=self._t)
            self._t = round(self._t + CHUNK_S, 2)

    def stale(self) -> str | None:
        return "station disconnected" if self._hub.is_stale(self._room_id) else None
