# Task 15c: `fast` vs `glossary` engine bench

One real `RoomWorker` run per (clip, engine) combination, real-time pace, real Gemini APIs (`--live`). Source: `bench/bench.py`; raw per-message recordings: `bench/raw/*.jsonl` (+ `*.meta.json` for cost/duration); cached reference translations: `bench/reference/*.txt`.

Date: 2026-09-24 (original live run) / 2026-09-25 (this recompute). Models: fast engine `gemini-3.5-live-translate-preview`; glossary engine `gemini-3.5-transcribe-live` (VERBATIM + customVocabulary) -> `gemini-3.5-flash-lite` translation (talk glossary as context); judge and reference translation `gemini-3.8-flash` (thinking LOW). **Recomputed from bench/raw/*.jsonl -- no new API calls this round** (Task 15c fix round 1: the latency metric was wrong, see Method below); original live run's real spend: US$0.1601 (cap US$0.50).

**Glossary-engine re-run (2026-09-25, +US$0.043):** after Task 16q (translation context from the previous two segments, inflected glossary terms) the two glossary rows were re-run live (`--live --only <clip>:glossary`); the fast rows are still the first run. Compared with the first run: EN→ES unchanged (translation lag p50 1.34 s, judge 3/2, terms 100 %); ES→EN judge scores improved from 3/2 to 4/3 and terms from 94 % to 100 %, but translation lag p50 rose from 1.76 s to 3.22 s. One run each, so part of that is noise; a longer prompt (the added context) plausibly costs some of it.

## en_clip (English -> Spanish)

| clip | engine | source latency p50/p90 (s) | translation latency p50/p90 (s) | fidelity | fluency | % terms | US$/run | US$/h |
|---|---|---|---|---|---|---|---|---|
| en_clip | fast | 1.28 / 2.08 (n=19) | 1.40 / 3.84 (n=19) | 4 | 3 | 94% | 0.0595 | 2.19 |
| en_clip | glossary | 0.80 / 1.48 (n=19) | 1.34 / 3.02 (n=19) | 3 | 2 | 100% | 0.0203 | 0.75 |

## es_clip (Spanish -> English)

| clip | engine | source latency p50/p90 (s) | translation latency p50/p90 (s) | fidelity | fluency | % terms | US$/run | US$/h |
|---|---|---|---|---|---|---|---|---|
| es_clip | fast | 1.66 / 2.35 (n=19) | 2.57 / 3.90 (n=19) | 5 | 4 | 100% | 0.0583 | 2.16 |
| es_clip | glossary | 0.74 / 1.57 (n=19) | 3.22 / 5.17 (n=19) | 4 | 3 | 100% | 0.0203 | 0.75 |

## Method

- **Latency (progress-lag, Task 15c fix round 1)**: for each track, compare two cumulative curves -- S(t), words spoken by time t (json3 word timings) and C(t), words visible on that caption track at time t (replayed from the recorded `append`/`set`/`close` events; `set` *replaces* a segment's text rather than adding to it, and the running max keeps the curve monotone through any ASR revision). Each curve is normalised by its own final total (a translation is not the same length as its source), then lag(f) = t_C(f) - t_S(f) is measured at progress points f = 5%, 10%, ..., 95%; the table reports p50/p90 over those 19 points (`bench/json3.py`'s `progress_lag`). Source latency uses the source-language track; translation latency the target-language track. `—` means that track had no events at all. This replaces the original `match_next` metric ("earliest unclaimed caption-bus event at or after each reference utterance's end"), which measured event density, not latency, and produced implausible numbers (e.g. a 26s "source latency" for the glossary engine independently measured at ~0.9s) whenever a track emitted frequent interim events.
- **`fast`'s source track**: the original report claimed fast "never publishes a source-language track" while its own results table showed a source-latency number for it anyway -- a genuine inconsistency. The raw recordings resolve it: fast (Live Translate) DOES publish a source-language track (e.g. `bench/raw/en_clip_fast.jsonl` has 101 `append` + 21 `close` events on its 'en' track, alongside 99 `append` + 21 `close` on 'es'); both engines get a real source-latency number in the table above.
- **Sanity check (word-timing alignment, `en_clip`)**: the json3 reference's first 3 words land at 'So,'@0.00s, 'what'@0.56s, 'does'@0.76s; the first event on its source-language ('en') caption track arrives at 2.63s. Both are seconds since the clip's own start (t=0) -- a large, implausible gap here would mean the reference and the recording are misaligned, not that the system is that slow.
- **Quality**: one reference translation per clip from the json3 transcript (`gemini-3.8-flash`, thinking LOW), cached under `bench/reference/`; then the same model (thinking LOW, JSON output) scores each engine's full translated output against the source transcript and that reference, fidelity and fluency 1-5, with a one-line justification (`bench/judge.py`; the prompts are in that file, unchanged per run). This is a single run per (clip, engine): an earlier, uncommitted live attempt on `es_clip`/`glossary` had scored fidelity/fluency 4/4 on essentially the same output where the committed run scored 3/2 -- LLM-judge 1-5 scores on freeform paragraph translation are noisy at n=1; read fidelity/fluency as directional, not precise. Each run's one-line judge justification is not stored anywhere on disk (`bench/bench.py` only ever held it in memory before rendering this table) -- it is not recoverable now without re-running the judge, which this fix round does not do (no new API calls; recompute-from-raw only).
- **Glossary terms**: `bench/terms.yaml` lists each clip's technical terms with accepted source/target spellings; `bench/terms.py` counts case/accent-insensitive substring occurrences in the source transcript and hits in the engine's translated output. Reported as % of source occurrences with a hit.
- **Cost**: from each run's `bench/raw/*.meta.json` (`Database.total_cost()`, glosa/db.py, the same per-component cost accounting `RoomStatus.cost_usd` reads from, recorded at run time); $/h scales it by the audio actually processed (`audio_s`).

## Recommendation

Averaged over both clips: fast scored fidelity 4.5/5, fluency 3.5/5, 97% glossary terms, at $2.17/h. Glossary scored fidelity 3.5/5, fluency 2.5/5, 100% glossary terms, at $0.75/h. Translation progress-lag (avg p50/p90 across both clips): fast 1.99s / 3.87s, glossary 2.28s / 4.09s -- on this run, glossary's median lag is actually at or below fast's on both clips (its two-stage pipeline, transcribe then translate, does not show up as a clearly worse p50 here), but its p90 tail is consistently worse than fast's on both clips; read both numbers, not just the median. This is one run of ~93 s clips each, not a statistically robust sample -- read the deltas as a signal for the controller's call on `default_engine_en`, not as a verdict on their own.
