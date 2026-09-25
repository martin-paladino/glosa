# Test audio samples

Short excerpts from public Nerdearla YouTube talks, bundled so Glosa can be
tried out immediately by importing a file instead of using a live
microphone. Produced as pre-work for T7.3.

Each clip has a matching caption reference under `reference/` (see
[Caption references](#caption-references) below) to compare live
transcription/translation output against.

## Clips

### `en_clip.opus` — English

- **Talk:** "LLMs on Autopilot: Designing Reliable Multi-Agent Systems for the Real World"
- **Speaker:** Annie Talvasto
- **Source:** https://www.youtube.com/watch?v=wyGMy5ic7PE
- **Excerpt used:** 00:05:53.560 – 00:07:26.500 (92.94 s)
- **Why this range:** falls inside the requested 05:00–09:00 window, is
  continuous uninterrupted speech (no applause/slide-change silence), and
  covers infrastructure/SRE-agent vocabulary (agents, SRE, provisioning,
  compliance, cost optimization, structural information, operational
  knowledge). The boundaries were chosen at sentence edges found in the
  auto-captions ("So, what does that then actually mean?" ... "... doesn't
  exist as a concept really.") so the clip starts/ends on a clean sentence.

### `es_clip.opus` — Spanish

- **Talk:** "Game Over en la factura: cómo transformamos el caos del costo cloud en un juego que todos quieren ganar"
- **Speaker:** Juan José Ciarlante
- **Source:** https://www.youtube.com/watch?v=VJCrOw2uxbk
- **Excerpt used:** 00:05:45.120 – 00:07:17.350 (92.23 s)
- **Why this range:** falls inside the requested 05:00–08:00 window, is
  continuous speech, and is dense with cloud/FinOps technical vocabulary
  (namespaces, labels, workloads, Grafana, Loki, AWS, GCP, Azure, modelo de
  datos, atribución de costos). Boundaries snapped to sentence edges in the
  auto-captions ("Por cierto, cuando ustedes reciben la factura de cloud..."
  ... "... No, eso es un desafío.").

## Attribution

Excerpt from Nerdearla's public YouTube channel, included for testing as
suggested by the Nerdearla Vibeathon organizers; all rights belong to the
speakers/Nerdearla.

## How the clips were produced

System `yt-dlp` (2026.03) returns HTTP 403 for this content; a newer
`yt-dlp` was run ad hoc via `uvx yt-dlp` (resolved to `2026.08.19`) instead,
with no changes to the project's own dependencies.

1. Downloaded best available audio-only stream for each video to a
   scratch directory outside the repo:

   ```bash
   uvx yt-dlp -f "bestaudio" -o "en_audio.%(ext)s" "https://www.youtube.com/watch?v=wyGMy5ic7PE"
   uvx yt-dlp -f "bestaudio" -o "es_audio.%(ext)s" "https://www.youtube.com/watch?v=VJCrOw2uxbk"
   ```

2. Downloaded the video's auto-generated captions in `json3` format to the
   same scratch directory (used both to pick the excerpt and to build the
   reference files below):

   ```bash
   uvx yt-dlp --skip-download --write-auto-sub --write-sub --sub-format json3 \
     --sub-langs "en,en-US,en-GB,en-orig" -o "en_full" "https://www.youtube.com/watch?v=wyGMy5ic7PE"
   uvx yt-dlp --skip-download --write-auto-sub --write-sub --sub-format json3 \
     --sub-langs "es,es-419,es-ES,es-orig" -o "es_full" "https://www.youtube.com/watch?v=VJCrOw2uxbk"
   ```

3. Inspected the captions in the target time windows to find a stretch of
   continuous, technical-vocabulary speech with clean sentence boundaries
   (see excerpt ranges above).

4. Cut and encoded the chosen ranges to mono Ogg/Opus at 16 kHz, ~24 kbps
   with `ffmpeg` (8.1), targeting the `< 2 MB` per-file budget:

   ```bash
   ffmpeg -y -ss 353.560 -to 446.500 -i en_audio.webm \
     -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k -application voip en_clip.opus

   ffmpeg -y -ss 345.120 -to 437.350 -i es_audio.webm \
     -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k -application voip es_clip.opus
   ```

5. Verified the result with `ffprobe`/`ls -l` (see below), copied the two
   `.opus` files into `samples/`, and deleted the scratch directory
   (original webm downloads and full-length caption files never entered
   the repo).

### Verification

| file | duration | channels | sample rate (container) | bitrate | size |
|---|---|---|---|---|---|
| `en_clip.opus` | 92.95 s | 1 (mono) | 48000 Hz (Ogg/Opus container rate; encoded from 16 kHz source per RFC 6716) | ~24.2 kb/s | 281,178 bytes (~275 KiB) |
| `es_clip.opus` | 92.24 s | 1 (mono) | 48000 Hz (Ogg/Opus container rate; encoded from 16 kHz source per RFC 6716) | ~24.1 kb/s | 278,399 bytes (~272 KiB) |

Both are well under the 2 MB budget. Note: Ogg/Opus streams always report a
48000 Hz "container" sample rate per the Opus spec regardless of the actual
encoded bandwidth — this is expected, not a leftover of a wrong `-ar` flag;
`ffmpeg`'s own encoder log confirms the stream was produced from a 16000 Hz,
mono, 24 kb/s encode (`Stream #0:0: Audio: opus, 16000 Hz, mono, flt, 24
kb/s`).

## Caption references

`reference/en_clip.en.json3` and `reference/es_clip.es.json3` are the
YouTube auto-generated captions for the exact same time ranges as the two
clips above, trimmed and re-based so `events[].tStartMs` starts at 0 at the
clip's start. They are meant as a latency/quality reference for a later
transcription/translation benchmark (not shipped as subtitles).

**Re-basing method:** YouTube's `json3` caption format uses "rolling" cue
events, where a single event's `segs` (word chunks) can straddle an
arbitrary time boundary — an event can start before a chosen cut point but
still contain words spoken after it (each word's real position is
`event.tStartMs + seg.tOffsetMs`). Filtering whole events by their start
time would therefore either drop wanted words at the clip start or leak
unwanted trailing words at the clip end. Instead, filtering was done at the
individual segment level:

1. For every event, compute each segment's absolute start time
   (`event.tStartMs + (seg.tOffsetMs or 0)`).
2. Keep only segments whose absolute start falls inside
   `[clip_start_ms, clip_end_ms)`.
3. Drop events left with no kept segments, and drop pure blank/newline
   roll-up events.
4. Rebuild each surviving event from only its kept segments: the new
   `tStartMs` is the first kept segment's absolute time minus
   `clip_start_ms`; each kept segment's `tOffsetMs` is recomputed relative
   to that new event start (the first segment has no `tOffsetMs`, matching
   the original format); `dDurationMs` is `min(original event end,
   clip_end_ms) - first kept segment's absolute time`.

The clip cut points themselves (the `-ss`/`-to` values above) were chosen
so that no word's caption boundary falls inside the exact cut instant —
each clip starts and ends on a caption/sentence boundary with a small
silence gap before the next spoken word, so re-based captions line up with
what is actually audible in the trimmed audio (no partially-audible
leading/trailing word). The `json3` structure (`events[].tStartMs`,
`events[].segs[].tOffsetMs`/`utf8`) is otherwise unmodified.

## Demo translations (`fixtures/tr_es_en.json`)

`make demo-fake`'s Spanish room (`demo-es` in `config.demo-fake.yaml`)
replays `fixtures/tr_es.jsonl` (a recorded transcribe-live/glossary-engine
session) through `FakeTranslator` (`glosa/text/translator.py`), which never
calls the API. Without a lookup
it would just tag every segment ("[en] <source text>"); `tr_es_en.json` is
a real, once-off English translation of every segment that recording
produces (`{source segment text: translation}`), loaded automatically by
`FakeTranslator` (via `load_demo_translations()`) so judges running `make
demo-fake` see real captions in the room's `en` track.

Regenerate it (only needed if `fixtures/tr_es.jsonl` changes) with:

```bash
uv run python scripts/build_demo_translations.py --env-path /path/to/glosa/.env
```

It drives an offline `RoomWorker` (`DrivenClock`/`FakeEngine`, no ffmpeg, no
wall-clock wait) through the whole recording to collect the exact segment
texts the glossary engine's `TranslationLane` hands to the translator, then
translates each one, in order, with the real `Translator`
(`gemini-3.5-flash-lite`, the same system-instruction prompt and rolling
context production uses) and writes the result to `fixtures/tr_es_en.json`.
`--env-path` defaults to this checkout's own `.env`; pass another
checkout's (e.g. the main repo's) if this one doesn't have one. The script
reads the key with `dotenv_values()` and never prints or logs it, and
refuses (`--budget-usd`, default $0.02) before any API call that would push
total spend over the cap. Whole-run cost so far: $0.0016 for 25 segments.

Any segment `TranslationLane` produces that is not a key in this file (a
different recording, a re-segmented line, etc.) just falls back to
`FakeTranslator`'s placeholder — this file only needs to cover
`tr_es.jsonl` as it exists today.

## Temporary files

All intermediate downloads (`*.webm` full-length audio, full-length
`*_full.*.json3` captions) were produced in a `mktemp`-style scratch
directory outside this repository and deleted after the two `.opus` clips
and two trimmed `reference/*.json3` files were copied in. No other files
in this worktree were modified.
