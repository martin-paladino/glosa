#!/usr/bin/env python3
"""bench/load_test.py: simulated load test of Glosa's caption fan-out (Task 15a).

Boots a REAL Glosa server (``python -m glosa.web.app``, the same entry point
operators use) in a subprocess, with a generated ``config.yaml`` of N `file`
rooms (``engine_mode: fake`` -- FakeEngine, no API calls) replaying
``samples/en_clip.opus``, and a throwaway ``.env`` in a temp directory. It
then opens M concurrent SSE clients (httpx, async) against
``/api/stream/{room}/{lang}``, spread evenly across every room's languages,
for D seconds, and measures:

  - loss: every client of a (room, lang) track must see the same, gap-free
    id sequence (CaptionBus.publish assigns one id per message, per track,
    with no gaps -- see glosa/captions/bus.py);
  - fan-out delay: wall-clock receipt time minus ``CaptionMsg.ts`` (the
    publish epoch), for messages published *after* the client subscribed
    (excludes the instant replay-buffer catch-up on connect, which is not
    fan-out latency) -- p50/p95/p99;
  - server CPU/RSS, sampled every second with ``ps`` (psutil is not a
    dependency here on purpose -- see the task brief) for the main process,
    and separately for its ffmpeg children (each `file` room decodes its
    source with a real ffmpeg subprocess even under engine_mode fake);
  - client errors/reconnects (Last-Event-ID resume, one retry per drop).

Usage::

    uv run python bench/load_test.py --rooms 50 --clients 500 --seconds 60

Plan-scale defaults (50 rooms / 500 clients / 60 s) match the ``make load``
target and Task 15's plan. Every run appends one row to
``bench/load-results.md`` (``--results-md`` to change). The script always
stops the server it started, even on error.

Pass criteria (Task 15's plan): no losses, server CPU < 80%, fan-out delay
p95 < 200 ms. Exit code is 0 iff all three hold.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EN_CLIP = REPO_ROOT / "samples" / "en_clip.opus"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
DEFAULT_RESULTS_MD = REPO_ROOT / "bench" / "load-results.md"

RESULTS_MD_HEADER = (
    "| Rooms | Clients | Duration (s) | Messages | Losses | Errors | Reconnects "
    "| p50 (ms) | p95 (ms) | p99 (ms) | CPU avg (%) | CPU peak (%) "
    "| Tree CPU peak (%) | RSS peak (MB) | Result | Notes |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)

# The plan's pass/fail thresholds (task-15-brief.md 15.1).
CPU_PEAK_LIMIT = 80.0
P95_DELAY_LIMIT_MS = 200.0


# --------------------------------------------------------------------------- setup


def free_port(host: str) -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def write_env(tmp: Path) -> Path:
    """A throwaway .env: engine_mode fake never uses the Gemini key, but
    Settings requires the field, and ADMIN_PASSWORD must be >= 8 chars."""
    path = tmp / ".env"
    path.write_text(
        "GEMINI_API_KEY=unused-load-test-key\n"
        f"ADMIN_PASSWORD=loadtest-{secrets.token_hex(6)}\n",
        encoding="utf-8",
    )
    return path


def write_config(tmp: Path, rooms: int) -> Path:
    """N `file` rooms replaying samples/en_clip.opus (absolute path: the
    server's cwd is the repo root, but this removes any doubt), engine_mode
    fake. en_clip.opus runs ~93s (see samples/README.md), longer than any
    run this script does by default, so no looping is needed to keep the
    source alive for the whole test."""
    data = {
        "event_name": "Glosa load test",
        "timezone": "UTC",
        "engine_mode": "fake",
        "db_path": str(tmp / "load.db"),
        "budget_usd": 1_000_000,
        "rooms": [
            {
                "id": f"room-{i:03d}",
                "name": f"Load room {i:03d}",
                "source_type": "file",
                "source_url": str(EN_CLIP),
                "language": "en",
                "default_targets": ["es"],
            }
            for i in range(rooms)
        ],
    }
    path = tmp / "config.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return path


def start_server(env_path: Path, config_path: Path, host: str, port: int, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["GLOSA_ENV_FILE"] = str(env_path)
    env["GLOSA_CONFIG"] = str(config_path)
    env["HOST"] = host
    env["PORT"] = str(port)
    log_fh = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        [str(VENV_PYTHON), "-m", "glosa.web.app"],
        cwd=REPO_ROOT,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )


def stop_server(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    """SIGTERM (uvicorn's own handler runs the lifespan shutdown: stops
    every room, kills its ffmpeg child), then SIGKILL if it doesn't exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


async def wait_ready(base_url: str, proc: subprocess.Popen, log_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early (code {proc.returncode}); see {log_path}")
            with contextlib.suppress(httpx.HTTPError):
                resp = await client.get(f"{base_url}/healthz")
                if resp.status_code == 200:
                    return
            await asyncio.sleep(0.2)
    raise TimeoutError(f"server did not become ready within {timeout}s; see {log_path}")


async def fetch_rooms(base_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(f"{base_url}/api/rooms")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- CPU/RSS sampling


@dataclass
class Sample:
    t: float
    main_cpu: float
    main_rss_kb: int
    children_cpu: float
    children_rss_kb: int
    n_children: int


async def take_ps_sample(root_pid: int) -> Sample | None:
    """One snapshot of the server process and its direct children (each
    `file` room's ffmpeg decoder), via `ps` -- psutil is deliberately not a
    dependency (see the task brief). LC_ALL=C/LANG=C: some locales make ps
    print %cpu with a comma decimal separator (observed on this machine's
    es_AR locale), which would otherwise break float() parsing.
    """
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-axo", "pid=,ppid=,pcpu=,rss=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await proc.communicate()
    except Exception:
        return None
    rows: dict[int, tuple[int, float, int]] = {}
    for line in out.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            pid, ppid, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            continue
        rows[pid] = (ppid, cpu, rss)
    if root_pid not in rows:
        return None
    _, main_cpu, main_rss = rows[root_pid]
    children_cpu = 0.0
    children_rss = 0
    n_children = 0
    for _pid, (ppid, cpu, rss) in rows.items():
        if ppid == root_pid:
            children_cpu += cpu
            children_rss += rss
            n_children += 1
    return Sample(
        t=time.monotonic(), main_cpu=main_cpu, main_rss_kb=main_rss,
        children_cpu=children_cpu, children_rss_kb=children_rss, n_children=n_children,
    )


async def sample_loop(root_pid: int, interval: float, stop: asyncio.Event) -> list[Sample]:
    samples: list[Sample] = []
    while not stop.is_set():
        sample = await take_ps_sample(root_pid)
        if sample is not None:
            samples.append(sample)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    return samples


# --------------------------------------------------------------------------- clients


@dataclass
class ClientResult:
    room: str
    lang: str
    idx: int
    ids: list[int] = field(default_factory=list)
    delays_ms: list[float] = field(default_factory=list)
    errors: int = 0
    reconnects: int = 0


def _parse_sse_line(line: str, ev_id: str | None, data_parts: list[str]) -> tuple[str | None, list[str], dict | None]:
    """Feeds one SSE line into the current frame; returns (ev_id, data_parts,
    completed_payload_or_None). A blank line ends a frame."""
    if line.startswith(":"):
        return ev_id, data_parts, None
    if line.startswith("id:"):
        return line[3:].strip(), data_parts, None
    if line.startswith("data:"):
        data_parts.append(line[5:].strip())
        return ev_id, data_parts, None
    if line == "":
        if ev_id is not None and data_parts:
            try:
                payload = json.loads("".join(data_parts))
            except json.JSONDecodeError:
                payload = None
            return None, [], payload
        return None, [], None
    return ev_id, data_parts, None


async def run_client(
    http_client: httpx.AsyncClient, base_url: str, slug: str, lang: str, idx: int, deadline: float
) -> ClientResult:
    result = ClientResult(room=slug, lang=lang, idx=idx)
    last_event_id: int | None = None
    while time.monotonic() < deadline:
        try:
            headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
            async with http_client.stream(
                "GET", f"{base_url}/api/stream/{slug}/{lang}", headers=headers
            ) as resp:
                if resp.status_code != 200:
                    result.errors += 1
                    return result
                subscribed_at = time.time()
                ev_id: str | None = None
                data_parts: list[str] = []

                async def pump() -> None:
                    nonlocal ev_id, data_parts, last_event_id
                    async for line in resp.aiter_lines():
                        ev_id, data_parts, payload = _parse_sse_line(line, ev_id, data_parts)
                        if payload is None:
                            continue
                        mid = payload.get("id")
                        if not isinstance(mid, int):
                            continue
                        result.ids.append(mid)
                        last_event_id = mid
                        ts = payload.get("ts")
                        if isinstance(ts, (int, float)) and ts >= subscribed_at:
                            result.delays_ms.append((time.time() - ts) * 1000.0)

                task = asyncio.create_task(pump())
                remaining = max(deadline - time.monotonic(), 0.0)
                done, pending = await asyncio.wait({task}, timeout=remaining)
                if task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    return result  # normal end: ran the full duration
                exc = task.exception()
                if exc is not None:
                    raise exc
                # The stream ended before the deadline (server-side close):
                # reconnect with Last-Event-ID, same as a real EventSource.
        except Exception:
            result.errors += 1
            result.reconnects += 1
            await asyncio.sleep(0.2)
    return result


# --------------------------------------------------------------------------- analysis


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


@dataclass
class TrackReport:
    track: tuple[str, str]
    n_clients: int
    min_len: int
    max_len: int
    gap_clients: int
    mismatch_clients: int


def analyze_tracks(results: list[ClientResult]) -> tuple[int, int, list[TrackReport]]:
    """Total messages received, total loss events, and a per-track
    breakdown. A "loss event" is a client whose own id sequence has a gap
    (skipped id) or that disagrees with the track's longest-observed
    sequence on their common prefix -- see the module docstring."""
    by_track: dict[tuple[str, str], list[ClientResult]] = {}
    for r in results:
        by_track.setdefault((r.room, r.lang), []).append(r)

    total_messages = 0
    total_loss_events = 0
    reports: list[TrackReport] = []
    for track, clients in by_track.items():
        gap_clients = 0
        for c in clients:
            total_messages += len(c.ids)
            if any(b - a != 1 for a, b in zip(c.ids, c.ids[1:])):
                gap_clients += 1
        longest = max(clients, key=lambda c: len(c.ids))
        mismatch_clients = 0
        for c in clients:
            n = min(len(c.ids), len(longest.ids))
            if c.ids[:n] != longest.ids[:n]:
                mismatch_clients += 1
        loss_events = gap_clients + mismatch_clients
        total_loss_events += loss_events
        reports.append(
            TrackReport(
                track=track, n_clients=len(clients),
                min_len=min((len(c.ids) for c in clients), default=0),
                max_len=len(longest.ids),
                gap_clients=gap_clients, mismatch_clients=mismatch_clients,
            )
        )
    return total_messages, total_loss_events, reports


@dataclass
class Report:
    rooms: int
    clients: int
    seconds: float
    wall_elapsed: float
    total_messages: int
    total_loss_events: int
    total_errors: int
    total_reconnects: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    cpu_avg: float | None
    cpu_peak: float | None
    tree_cpu_peak: float | None
    rss_peak_mb: float | None
    tree_rss_peak_mb: float | None
    n_samples: int
    peak_at_s: float | None  # seconds into the run when cpu_peak was sampled
    startup_cpu_avg: float | None  # avg main_cpu over the first few samples (connection burst)
    steady_cpu_avg: float | None  # avg main_cpu once past the startup burst
    track_reports: list[TrackReport]
    note: str

    @property
    def passed(self) -> bool:
        if self.total_loss_events != 0:
            return False
        if self.cpu_peak is None or self.cpu_peak >= CPU_PEAK_LIMIT:
            return False
        if self.p95_ms is None or self.p95_ms >= P95_DELAY_LIMIT_MS:
            return False
        return True


def build_report(
    rooms: int, clients: int, seconds: float, wall_elapsed: float,
    results: list[ClientResult], samples: list[Sample], note: str,
) -> Report:
    total_messages, total_loss_events, track_reports = analyze_tracks(results)
    total_errors = sum(r.errors for r in results)
    total_reconnects = sum(r.reconnects for r in results)
    delays = sorted(d for r in results for d in r.delays_ms)
    cpu_avg = statistics.fmean(s.main_cpu for s in samples) if samples else None
    cpu_peak = max((s.main_cpu for s in samples), default=None)
    tree_cpu_peak = max((s.main_cpu + s.children_cpu for s in samples), default=None)
    rss_peak_mb = max((s.main_rss_kb / 1024 for s in samples), default=None)
    tree_rss_peak_mb = max(((s.main_rss_kb + s.children_rss_kb) / 1024 for s in samples), default=None)

    # Was cpu_peak a startup transient (opening every client's SSE connection
    # at once) or a sustained steady-state cost? Split the first few samples
    # (the connection burst) from the rest.
    peak_at_s = None
    startup_cpu_avg = None
    steady_cpu_avg = None
    if samples:
        run_start = samples[0].t
        peak_sample = max(samples, key=lambda s: s.main_cpu)
        peak_at_s = peak_sample.t - run_start
        startup_n = min(5, len(samples))
        startup_cpu_avg = statistics.fmean(s.main_cpu for s in samples[:startup_n])
        steady = samples[startup_n:]
        if steady:
            steady_cpu_avg = statistics.fmean(s.main_cpu for s in steady)

    return Report(
        rooms=rooms, clients=clients, seconds=seconds, wall_elapsed=wall_elapsed,
        total_messages=total_messages, total_loss_events=total_loss_events,
        total_errors=total_errors, total_reconnects=total_reconnects,
        p50_ms=percentile(delays, 0.50), p95_ms=percentile(delays, 0.95), p99_ms=percentile(delays, 0.99),
        cpu_avg=cpu_avg, cpu_peak=cpu_peak, tree_cpu_peak=tree_cpu_peak,
        rss_peak_mb=rss_peak_mb, tree_rss_peak_mb=tree_rss_peak_mb,
        n_samples=len(samples), track_reports=track_reports, note=note,
        peak_at_s=peak_at_s, startup_cpu_avg=startup_cpu_avg, steady_cpu_avg=steady_cpu_avg,
    )


# --------------------------------------------------------------------------- output


def _fmt(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def print_report(report: Report) -> None:
    print()
    print(f"=== load test: {report.rooms} rooms / {report.clients} clients / {report.seconds:.0f}s ===")
    print(f"wall elapsed: {report.wall_elapsed:.1f}s (target {report.seconds:.0f}s)")
    print(f"messages received: {report.total_messages}")
    print(f"loss events: {report.total_loss_events}")
    print(f"client errors: {report.total_errors}  reconnects: {report.total_reconnects}")
    print(f"fan-out delay ms: p50={_fmt(report.p50_ms)} p95={_fmt(report.p95_ms)} p99={_fmt(report.p99_ms)}")
    print(
        f"server CPU %: avg={_fmt(report.cpu_avg)} peak={_fmt(report.cpu_peak)} "
        f"(tree incl. ffmpeg peak={_fmt(report.tree_cpu_peak)}) over {report.n_samples} samples"
    )
    print(f"server RSS MB: peak={_fmt(report.rss_peak_mb)} (tree incl. ffmpeg peak={_fmt(report.tree_rss_peak_mb)})")
    print(
        f"CPU shape: peak at t={_fmt(report.peak_at_s)}s into the run; "
        f"first-{min(5, report.n_samples)}s avg={_fmt(report.startup_cpu_avg)} "
        f"vs steady-state avg={_fmt(report.steady_cpu_avg)} "
        "(large gap => the peak is the connection-open burst, not sustained fan-out cost)"
    )
    if report.total_loss_events:
        print("-- tracks with losses --")
        for t in report.track_reports:
            if t.gap_clients or t.mismatch_clients:
                print(
                    f"  {t.track}: clients={t.n_clients} len=[{t.min_len},{t.max_len}] "
                    f"gap_clients={t.gap_clients} mismatch_clients={t.mismatch_clients}"
                )
    verdict = "PASS" if report.passed else "FAIL"
    print(
        f"--- {verdict}: losses==0 ({report.total_loss_events == 0}), "
        f"CPU peak < {CPU_PEAK_LIMIT:.0f}% ({_fmt(report.cpu_peak)}), "
        f"p95 < {P95_DELAY_LIMIT_MS:.0f}ms ({_fmt(report.p95_ms)}) ---"
    )


def append_results_md(report: Report, path: Path) -> None:
    if not path.exists() or RESULTS_MD_HEADER not in path.read_text(encoding="utf-8"):
        # First write: seed with a short intro + the table header. Any prose
        # already in the file (this script only appends rows) is preserved.
        if not path.exists():
            path.write_text(
                "# Glosa load test results (Task 15a)\n\n"
                "Runs of `bench/load_test.py` (`make load` for the plan-scale one). "
                "See the module docstring for methodology.\n\n" + RESULTS_MD_HEADER,
                encoding="utf-8",
            )
        elif RESULTS_MD_HEADER not in path.read_text(encoding="utf-8"):
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + RESULTS_MD_HEADER)
    verdict = "PASS" if report.passed else "FAIL"
    row = (
        f"| {report.rooms} | {report.clients} | {report.seconds:.0f} | {report.total_messages} "
        f"| {report.total_loss_events} | {report.total_errors} | {report.total_reconnects} "
        f"| {_fmt(report.p50_ms)} | {_fmt(report.p95_ms)} | {_fmt(report.p99_ms)} "
        f"| {_fmt(report.cpu_avg)} | {_fmt(report.cpu_peak)} | {_fmt(report.tree_cpu_peak)} "
        f"| {_fmt(report.rss_peak_mb, 0)} | {verdict} | {report.note} |\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(row)


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rooms", type=int, default=50)
    p.add_argument("--clients", type=int, default=500)
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--sample-interval", type=float, default=1.0)
    p.add_argument("--ready-timeout", type=float, default=30.0)
    p.add_argument("--results-md", type=Path, default=DEFAULT_RESULTS_MD)
    p.add_argument("--note", default="", help="extra text for the results row's Notes column")
    p.add_argument("--keep-tmp", action="store_true", help="keep the temp config/.env/db/log dir for debugging")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not EN_CLIP.is_file():
        print(f"error: sample clip not found: {EN_CLIP}", file=sys.stderr)
        return 2
    if not VENV_PYTHON.is_file():
        print(f"error: venv python not found: {VENV_PYTHON} (run `uv sync --all-extras --dev` first)", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="glosa-load-"))
    print(f"temp dir: {tmp}")
    proc: subprocess.Popen | None = None
    try:
        env_path = write_env(tmp)
        config_path = write_config(tmp, args.rooms)
        log_path = tmp / "server.log"
        port = free_port(args.host)
        base_url = f"http://{args.host}:{port}"
        print(f"starting server on {base_url} ({args.rooms} rooms)...")
        proc = start_server(env_path, config_path, args.host, port, log_path)

        await wait_ready(base_url, proc, log_path, args.ready_timeout)
        rooms = await fetch_rooms(base_url)
        if len(rooms) != args.rooms:
            raise RuntimeError(f"expected {args.rooms} rooms, server reports {len(rooms)}")
        tracks = [(r["slug"], lang) for r in rooms for lang in r["langs"]]
        assignments = [tracks[i % len(tracks)] for i in range(args.clients)]
        print(f"server ready (pid {proc.pid}); {len(tracks)} tracks, {args.clients} clients")

        stop_flag = asyncio.Event()
        sampler = asyncio.create_task(sample_loop(proc.pid, args.sample_interval, stop_flag))

        limits = httpx.Limits(max_connections=args.clients + 50, max_keepalive_connections=20)
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=30.0)
        start = time.monotonic()
        deadline = start + args.seconds
        async with httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=False) as http_client:
            results = await asyncio.gather(
                *(
                    run_client(http_client, base_url, slug, lang, i, deadline)
                    for i, (slug, lang) in enumerate(assignments)
                )
            )
        wall_elapsed = time.monotonic() - start
        stop_flag.set()
        samples = await sampler

        report = build_report(args.rooms, args.clients, args.seconds, wall_elapsed, results, samples, args.note)
        print_report(report)
        args.results_md.parent.mkdir(parents=True, exist_ok=True)
        append_results_md(report, args.results_md)
        print(f"results appended to {args.results_md}")
        return 0 if report.passed else 1
    finally:
        if proc is not None:
            print("stopping server...")
            stop_server(proc)
        if args.keep_tmp:
            print(f"kept temp dir: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
