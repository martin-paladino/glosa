# Glosa load test results (Task 15a)

Simulated load test of the caption fan-out (Task 15's plan, 15.1): a real
Glosa server (`python -m glosa.web.app`, `engine_mode: fake` -- FakeEngine,
zero API spend) with N `file` rooms replaying `samples/en_clip.opus`, hit by
M concurrent SSE clients (`httpx`, async) spread evenly across every room's
languages, for D seconds. Runs of `bench/load_test.py` (`make load` for the
plan-scale one); see its module docstring for the full methodology (loss
definition, fan-out delay definition, CPU/RSS sampling). Machine: Apple M4,
10 cores, 16 GB, macOS.

Plan's pass criteria: **no losses**, server **CPU peak < 80%**, fan-out
delay **p95 < 200 ms**.

| Rooms | Clients | Duration (s) | Messages | Losses | Errors | Reconnects | p50 (ms) | p95 (ms) | p99 (ms) | CPU avg (%) | CPU peak (%) | Tree CPU peak (%) | RSS peak (MB) | Result | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 50 | 20 | 850 | 0 | 0 | 0 | 0.9 | 3.1 | 4.1 | 5.3 | 59.6 | 79.7 | 91 | PASS | debug run, to shake out bugs before the plan-scale run |
| 50 | 500 | 60 | 37250 | 0 | 0 | 0 | 0.8 | 1.9 | 4.8 | 17.9 | 62.5 | 178.4 | 201 | PASS | plan-scale run (Task 15's plan: 50 rooms / 500 clients / 60s) |
| 100 | 1000 | 60 | 74500 | 0 | 0 | 0 | 0.6 | 1.6 | 6.3 | 23.6 | 95.4 | 320.4 | 321 | FAIL | headroom run: 2x plan scale to find the ceiling |
| 100 | 1000 | 60 | 74500 | 0 | 0 | 0 | 0.6 | 1.6 | 6.2 | 23.9 | 93.9 | 324.3 | 326 | FAIL | repeat, with CPU startup-vs-steady breakdown |

## Findings

- **Plan scale (50 rooms / 500 clients / 60 s) passes with comfortable
  headroom.** Zero lost or out-of-order messages on any of the 100 tracks;
  fan-out delay p95 1.9 ms and p99 4.8 ms (the 200 ms budget is ~100x
  looser than what was observed); server CPU peaked at 62.5% of one core
  (average 17.9%), well under the 80% limit. `httpx` itself reported zero
  connection errors or forced reconnects across the run.
- **2x scale (100 rooms / 1000 clients / 60 s) finds the ceiling, and it is
  not the fan-out.** Fan-out delay stays just as good (p95 1.6 ms) and
  there are still zero losses -- the pub/sub path (`glosa/captions/bus.py`,
  one `asyncio.Queue` per subscriber, no polling) scales cleanly to 1000
  concurrent subscribers. What fails the plan's CPU criterion is a **startup
  transient**: opening 1000 SSE connections against the same process within
  about a second spikes the main process to 93-95% of one core for a couple
  of samples (`peak at t=0.0s`, two independent runs agree: 95.4% and 93.9%
  peak vs. 22-24% steady-state average once the connections are up -- see
  the script's "CPU shape" line). Steady-state CPU at 1000 clients
  (~23%) is barely above the 500-client steady state (~18%), consistent
  with the fan-out being cheap per message and the real cost being
  per-connection setup, not per-message delivery.
- **No losses at any scale tested.** `CaptionBus` assigns one id per
  publish, per (room, lang) track, and replays its buffer (2000 messages,
  far more than any track publishes in a 60 s run) to every new subscriber
  before switching to live delivery -- every client of a track saw the
  exact same, gap-free id sequence in every run.

## Bottleneck

The one criterion that fails at 2x scale is CPU, and it is a **one-time
connection-establishment burst**, not sustained fan-out cost: `asyncio.wait`
samples show the peak at the very first 1-second sample of the run, and
average CPU across the rest of the run stays under 25%. `bench/load_test.py`
opens all of a run's clients essentially at once (`asyncio.gather`), which
is a harsher test than a real audience arriving over the seconds or minutes
before a talk starts. Reported as a concern in the task report, not fixed
here per the brief (`bench/`/`tests/load/` only): the practical takeaway for
"how it scales" is that Glosa comfortably serves the planned 50 rooms / 500
concurrent viewers with capacity to spare, and that a much larger instantaneous
connection burst (not a realistic arrival pattern for a conference audience)
is where the single process starts to show strain -- worth a note in the
scaling section rather than a blocker.

## What's measured

"Server CPU/RSS" is the main `python -m glosa.web.app` process (the asyncio
event loop doing the actual SSE fan-out) -- the thing this task is about.
Each `file` room also runs a real `ffmpeg` subprocess decoding
`en_clip.opus` in real time (even under `engine_mode: fake` -- FakeEngine
replays its own recorded timeline independently of the audio, but
`RoomWorker` still starts the configured source; see
`glosa/audio/ingest.py`). "Tree CPU/RSS" sums the main process with every
such ffmpeg child, and is reported for context (what a single VM running
Glosa would actually need), but is **not** what the plan's 80% criterion
gates -- that ffmpeg cost is a property of running N real audio sources, not
of the caption fan-out this task measures. At 50 rooms the ffmpeg fleet
alone accounts for roughly 115 percentage points of CPU (tree peak 178% -
main peak 63%) and ~150 MB of RSS; both scale ~linearly with room count, so
a deployment with fewer file/url/youtube rooms (e.g. mostly `emitter`
stations, which have no ffmpeg) would show a much smaller gap between "main"
and "tree".

## Reproducing

```
uv run python bench/load_test.py --rooms 50 --clients 500 --seconds 60   # make load
uv run python bench/load_test.py --rooms 100 --clients 1000 --seconds 60
```
