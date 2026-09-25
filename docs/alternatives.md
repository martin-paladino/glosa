# Alternatives evaluated

Glosa runs on two Gemini engines, chosen after benchmarking them against
each other and against the alternatives below. "Measured by us" means we
ran it and timed it ourselves (dated); "vendor/third-party" means the
number comes from the provider's own docs, pricing page or announcement,
not from our own test.

Two separate benchmarks feed this table:

- **Pre-event spike** (2026-09-23, disposable code, not in this repo):
  `transcripcion-v0/spike-latencia/REPORT.md` (engine latency/quality) and
  `transcripcion-v0/notas/jev-typesafe.md` + `modelos-gemini.md` (Jev, model
  choice). One ~3-4 min clip per language, real-tier-limited (see caveats
  below each number).
- **Vibeathon build** (2026-09-24, this repo): the glossary engine's own
  wiring, 6 runs of 60 s of Spanish audio, logged in
  `.superpowers/sdd/2026-09-24-glosa/progress.md`.

## The two engines Glosa ships with

| Option | What it is | Latency | Cost/h | Glossary | Streaming | Why (not) |
|---|---|---|---|---|---|---|
| **Gemini Live Translate** (`gemini-3.5-live-translate-preview`) — chosen as the **fast** engine | One Gemini Live API session per room does STT + translation together; Glosa keeps only the text (audio response discarded). `glosa/engines/live_translate.py`. | Translation p50 2.4 s / p90 4.3 s (EN→ES); source text p50 1.6 s. Measured by us, `spike-latencia/REPORT.md`, 2026-09-23; one clip, real-time paced, YouTube auto-captions (±0.3 s) as reference. | US$0.0368/min per room (one target language per session) ≈ US$2.2/h, confirmed against the API's own usage metadata, same 2026-09-23 spike. This is also `Prices.lt_per_min` in `glosa/config.py`. **No.** It ignores our glossary (see "Why" for an example). | Yes, one continuous session; ran stably 3 sessions in parallel with no cuts in a 4 min test. | **Chosen** for stability (no stalls in the spike, unlike server-VAD `transcribe-live`) and because one connection gives both the original and the translated text. Downsides we accept: it does not apply our glossary (e.g. it renders "cumplimiento normativo" as plain "cumplimiento" instead of the configured term, while the cascade gets it right), adds filler words ("Este,", "eh"), sometimes drops a word, and is a **preview model** that can change or GoAway mid-talk (Glosa's session-handoff relay, `relay.standby_at`/`force_at` in `config.example.yaml`, covers the ~10-min session lifetime). |
| **Gemini Transcribe Live + Flash-Lite** (`gemini-3.5-transcribe-live` for STT, `gemini-3.5-flash-lite` for translation, fallback `gemini-3.1-flash-lite`) — chosen as the **glossary** engine | STT with `customVocabulary` in `VERBATIM` mode (SMART mode is reported to ignore the vocabulary), then a text-only translation pass with the glossary in the system prompt. `glosa/engines/transcribe.py`, `glosa/text/translator.py`. | 6 runs × 60 s of Spanish audio, measured by us during this vibeathon (2026-09-24, `progress.md`): source (transcription) p50 0.86–0.92 s / p90 0.97–1.13 s from end of speech; translation p50 0.66–0.68 s / p90 0.75–1.48 s from the cut. Flash-Lite alone, in isolation, pre-event spike (2026-09-23): ~0.75 s p50 for a short phrase (design spec §12, free-tier key) and p50 747 ms / max 1030 ms over 12 short EN→ES calls with the glossary in the system prompt (`notas/modelos-gemini.md`, paid-tier key, same day) — `spike-latencia/REPORT.md`'s own free-tier run of the same model separately saw p50 0.8 s with occasional 3-21 s spikes and one 503. | ~US$0.012/min of audio with one translated language (transcribe US$0.009/min + Flash-Lite), measured by us during this vibeathon, `progress.md` (e.g. run "corrida live 6": US$0.0126). `Prices.transcribe_per_min` = 0.009 in `glosa/config.py`; Flash-Lite is billed per token (`flash_lite_in_per_m`/`flash_lite_out_per_m` = US$0.30 / US$2.50 per million tokens). | **Yes.** Respects the configured glossary; this was the point of building it. | Yes. The server-side VAD alone froze on monologues in the spike (finals every 12–18 s); Glosa's hybrid VAD (client-side energy VAD, `vad.pause_ms`/`min_speech_s`) fixes that — no freezes measured in Spanish in the spike. | **Chosen** because it is the only path that honors the glossary, and because splitting STT from translation lets each extra target language reuse the same transcription for roughly the cost of one more Flash-Lite pass (est. ~US$0.1/h/extra language per the pre-event spike's recommendation — **not implemented as a live multi-language feature in this build**: today each room/talk still captions one source + one target language live, see `glosa/room.py`'s `target_lang()`). Also produces a slower, higher-quality "corrected" pass (`gemini-3.8-flash`, p50 1.66 s pre-event spike) meant for SRT/VTT export — `glosa/text/corrector.py` exists, but the admin endpoint to trigger/download an export is not wired in this build (see `docs/operator-guide.md`). |

## Alternatives investigated and not chosen

All of these come from the pre-event spec's alternatives survey
(`transcripcion-v0/docs/superpowers/specs/2026-09-23-glosa-design.md` §12,
2026-09-23) unless noted — **vendor/third-party reported figures**, not
benchmarked by us on our own audio.

| Option | What it is | Latency | Cost/h | Glossary | Streaming | Why not |
|---|---|---|---|---|---|---|
| **Soniox `stt-rt-v5`** | Real-time streaming STT+translation, EN↔ES, one stream. | Not benchmarked by us (vendor markets it as low-latency streaming; no figure in our sources). | ~US$0.18/h (vendor-reported, spec §12). | Yes — `translation_terms` parameter (vendor-reported). | Yes. | Not evaluated hands-on before the deadline; Gemini Live Translate's Google AI Studio credits and our existing spike gave a faster path to a working demo. Worth a look for a v2: it is the only third-party option in this table that both streams and supports a glossary. |
| **OpenAI `gpt-realtime-translate`** | Real-time speech translation session. | Not benchmarked by us. | ~US$2.04/h (vendor-reported, spec §12) — comparable to Live Translate's ~US$2.2/h. | **No.** | Yes, but **one target language per session** (same structural limit as our chosen engines). | No glossary support, and no cost or quality advantage over what we'd already built and tested with Gemini. |
| **OpenAI Whisper** | Open-weights STT. | Not benchmarked by us. | Self-hosted (compute only; no per-minute API price in our sources). | No. | No usable streaming (batches silences into hallucinated text, per spec §12). | No new open release evaluated; the spec notes it invents text during silences and only translates **into** English, not out of it — wrong shape for an ES↔EN conference. |
| **Gemma 4 (with audio)** | Multimodal Gemini-family open model, audio input. | Not benchmarked by us. | Self-hosted. | Structural, not measured: 30 s clip cap. | **No** — 30 s max clips, no streaming API (spec §12). | Disqualified by the 30 s clip cap alone: a conference talk runs for tens of minutes. |
| **Gemini Omni** | Gemini's omni-modal model. | — | — | — | Generates video output (spec §12). | Wrong output modality for captions; not usable at all for this task. |
| **NVIDIA Parakeet, local (Apple M4)** | Local streaming ASR, no network call. | Source text p50 1.2 s / p90 1.8 s overall; with the streaming step widened to 2 s (to fit 2 rooms on one core instead of 1), p50 rises to 1.8 s. Batch throughput ~30x real-time. Measured by us, `spike-latencia/REPORT.md`, 2026-09-23. | US$0 marginal (local compute only). | No (ASR only — no built-in translation; would need a second local model, e.g. TranslateGemma, not benchmarked). | Yes, locally. Capacity: **1–2 rooms per M4** (1 s steps ≈ 70% of one core = 1 room; 2 s steps ≈ 37% = 2 rooms, at the cost of that higher 1.8 s delay), per the spike. | Kept as the **offline fallback** for a room that loses internet (source-language captions only, no live translation) — not the primary engine because of the 1–2 room ceiling per machine and because it needs a second model bolted on for translation. Out of scope for this vibeathon build (P2 in the spec). |
| **TypeSafe Jev, for "translate now vs. wait" segmentation** | A typed-question model (`Noul`/`Choice`/`Score`, not a text generator) asked "is this fragment a complete enough unit of sense to translate now?" | p50 0.37 s / p90 0.50 s over 947 calls via the direct API (measured by us, `jev-typesafe.md`, 2026-09-23). | US$0.042 per million input tokens, output free (vendor-reported, `jev-typesafe.md`, 2026-09-23). | n/a | n/a | **Discarded with data.** Our own P1 heuristic segmenter beat it: 3.41 s / 4.86 s (p50/p90) with fidelity 9.31, vs. Jev alone or Jev+heuristic, whose best case only shaved 0.07 s off the median while making fidelity and fluency worse (measured by us, `jev-typesafe.md`, 2026-09-23; 20 real partial-transcript fragments). The segmenter's `comma_min_words`/`max_words`/`max_wait_s` in `glosa/config.py` implement the winning P1 rule. |
| **TypeSafe Jev, as a translation-quality meter** | Same model, asked to score how faithful a translation is to its source. | p50 0.37 s / p90 0.50 s (same measurement as above). | Same pricing as above. | n/a | n/a | **Chosen** for this second use. AUC 0.975 distinguishing 20 good vs. 20 corrupted translations (mean "faithful" probability 0.79 vs. 0.21), measured by us, `jev-typesafe.md`, 2026-09-23. Weaker on omissions (0.29) and numbers/entities (0.31) than on wrong terms (0.09) or inverted meaning (0.14) — an operator should not treat a high Jev score as a guarantee against a dropped number. This is the `TYPESAFE_API_KEY`-gated quality meter in Glosa (`glosa/quality.py`); optional, everything else works without it. |

## Caveats on the pre-event spike numbers

The 2026-09-23 spike (`spike-latencia/REPORT.md`) itself flags its own
numbers as approximate: one clip per language, run on a **free tier** key
at a moment of high demand (503s on `gemini-flash`/`3.8-flash`/`3-flash-preview`,
a 404 on the now-removed `gemini-2.5-flash`), and Live Translate's own
translation alignment was scored by an LLM, not a human. The vibeathon's
own glossary-engine numbers (2026-09-24) were re-measured on the **paid
tier** and are the ones we trust most for that engine; Live Translate's
2.4 s/4.3 s figure was not independently re-measured during the vibeathon
itself, only reconfirmed as the price Glosa bills against
(`Prices.lt_per_min`, `glosa/config.py`).
