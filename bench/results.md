# Task 15c: `fast` vs `glossary` engine bench

One real `RoomWorker` run per (clip, engine) combination, real-time pace, real Gemini APIs (`--live`). Source: `bench/bench.py`; raw per-message recordings: `bench/raw/*.jsonl` (+ `*.meta.json` for cost/duration); cached reference translations: `bench/reference/*.txt`.

Date: 2026-09-24. Models: fast engine `gemini-3.5-live-translate-preview`; glossary engine `gemini-3.5-transcribe-live` (VERBATIM + customVocabulary) -> `gemini-3.5-flash-lite` translation (talk glossary as context); judge and reference translation `gemini-3.8-flash` (thinking LOW). Real spend this run: US$0.1601 (cap US$0.50).

## en_clip (English -> Spanish)

| clip | engine | source latency p50/p90 (s) | translation latency p50/p90 (s) | fidelity | fluency | % terms | US$/run | US$/h |
|---|---|---|---|---|---|---|---|---|
| en_clip | fast | 26.42 / 52.14 (n=21) | 0.58 / 1.26 (n=55) | 4 | 3 | 94% | 0.0595 | 2.19 |
| en_clip | glossary | 25.99 / 79.63 (n=9) | 1.42 / 3.18 (n=55) | 3 | 2 | 100% | 0.0190 | 0.70 |

## es_clip (Spanish -> English)

| clip | engine | source latency p50/p90 (s) | translation latency p50/p90 (s) | fidelity | fluency | % terms | US$/run | US$/h |
|---|---|---|---|---|---|---|---|---|
| es_clip | fast | 29.97 / 67.72 (n=13) | 0.46 / 0.98 (n=47) | 5 | 4 | 100% | 0.0583 | 2.16 |
| es_clip | glossary | 31.64 / 71.21 (n=10) | 1.22 / 3.70 (n=47) | 3 | 2 | 94% | 0.0199 | 0.74 |

## Method

- **Source latency** (glossary only -- fast never publishes a source-language track, see the module docstring in `bench/bench.py`): seconds from each reference utterance's estimated end (`bench/json3.py`'s `utterances()`, from the json3 word timings) to the earliest unclaimed `close` event on the source-language caption track at or after it (`match_next`); p50/p90 of those gaps.
- **Translation latency**: the same `match_next` computation, against the target-language track's `append`/`close` events (either puts translated text on screen).
- **Quality**: one reference translation per clip from the json3 transcript (`gemini-3.8-flash`, thinking LOW), cached under `bench/reference/`; then the same model (thinking LOW, JSON output) scores each engine's full translated output against the source transcript and that reference, fidelity and fluency 1-5, with a one-line justification (`bench/judge.py`; the prompts are in that file, unchanged per run).
- **Glossary terms**: `bench/terms.yaml` lists each clip's technical terms with accepted source/target spellings; `bench/terms.py` counts case/accent-insensitive substring occurrences in the source transcript and hits in the engine's translated output. Reported as % of source occurrences with a hit.
- **Cost**: `Database.total_cost()` (glosa/db.py), the same per-component cost accounting `RoomStatus.cost_usd` reads from, summed over the run; $/h scales it by the audio actually processed (`RoomWorker.audio_s`).

## Recommendation

Averaged over both clips: fast scored fidelity 4.5/5, fluency 3.5/5, 97% glossary terms, at $2.17/h. Glossary scored fidelity 3.0/5, fluency 2.0/5, 97% glossary terms, at $0.72/h. This is one run of ~93 s clips each, not a statistically robust sample -- read the deltas as a signal for the controller's call on `default_engine_en`, not as a verdict on their own. The glossary engine's own translation latency additionally pays for its two-stage pipeline (transcribe then translate) versus fast's single Live Translate hop; see the per-clip tables above for whether that shows up as materially higher p50/p90 here.
