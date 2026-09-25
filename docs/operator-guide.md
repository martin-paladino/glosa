# Operator guide

Who this is for: whoever sets up and watches Glosa during a live event. It
assumes you've read the README's Quickstart. Button/label names below are
quoted exactly as they appear in the code (`glosa/web/templates/*.html`,
`glosa/i18n.py`, `glosa/web/static/js/admin.js`) — where the panel doesn't
have a button for something yet, this guide says so and shows the API call
instead, rather than inventing a label that isn't in the UI.

**State of the admin panel in this build.** `/admin` today (`admin.html`,
`admin.js`) shows every room with its name, current talk (or "No talk
running"), a status word, and two buttons: **Start** and **Stop**. That's
it. The fuller **"Sala de control"** console — a live-updating **Atención**
(Attention) bar, per-room monitors, a side drawer with mode/reconnect/
station controls, a budget meter, an event log — is an **approved design**
(`docs/design/README.md`, `docs/design/admin.html`) that a separate task is
implementing; it is not merged into this build. Everything this guide
describes as "not yet in the panel" already works as a plain HTTP call to
`/api/admin/...` (same auth as everything else: the `/admin` session
cookie plus an `X-Glosa-Admin: 1` header — see README's *Agenda &
autopilot*) — use `curl` or a REST client until the buttons land.
<!-- TODO(controller): once the "Sala de control" console merges, replace the curl-based steps below with the real button names and re-check every claim in "During the event" against the shipped UI. -->

## Before the event

### 1. Credentials and budget

Copy `.env.example` to `.env` and fill in:

- `GEMINI_API_KEY` — required. Get one at Google AI Studio.
- `ADMIN_PASSWORD` — required, at least 8 characters (Glosa refuses to
  start otherwise); this is the only thing between the internet and your
  rooms' start/stop controls and the station keys. Generate one with
  `openssl rand -base64 18`.
- `TYPESAFE_API_KEY` — optional, only needed for the Jev quality meter
  (`docs/alternatives.md`); leave blank to skip it.

Set `budget_usd` in `config.yaml` to your event's spending ceiling (default
US$10.0 — this vibeathon's own prepaid project). See `docs/costs.md` for
per-hour costs by engine and what actually happens when that budget runs
out (short version: it isn't a hard stop from Glosa's own tracker yet —
it's the vendor's own credit exhaustion, detected and shown per room).

### 2. Move to a paid Gemini tier before the day, and why

**The free tier is not enough for a real event.** Our own pre-event test
(2026-09-23, `spike-latencia/REPORT.md`) hit `503` (high demand) on the
`flash`, `3.8-flash` and `3-flash-preview` models, and a `404` on
`gemini-2.5-flash` (no longer available to new users) — all on a
free-tier key. A
same-day test on a **paid** key (`notas/modelos-gemini.md`, 2026-09-23) sent
25 simultaneous requests to `gemini-3.5-flash-lite` and got 25 OKs, versus
8-of-25 `429`s on the free key. Check your paid tier's current rate/
concurrency limits in Google AI Studio ahead of time — they cap how many
rooms one API key can drive at once (README's *How it scales*).

### 3. HTTPS (Caddy or a tunnel)

Room stations need `getUserMedia` (microphone capture), which browsers
only allow in a secure context. Three options, all documented in the
README's *Room stations* section with copy-pasteable configs:

1. **`deploy/Caddyfile`** — a reverse proxy that gets you a real Let's
   Encrypt certificate (Option A, needs a real domain and open 80/443) or a
   LAN-only self-signed one via `tls internal` (Option B). It sets
   `flush_interval -1` so live captions aren't buffered mid-stream — do not
   drop that line if you write your own proxy config. `docker-compose.yml`
   has a commented-out `caddy` service that mounts this same file.
2. **A tunnel** (ngrok, Cloudflare Tunnel, Tailscale Funnel...) to
   `localhost:8000` — quickest for a single stage or a rehearsal.
3. **Lab only:** Chrome's `--unsafely-treat-insecure-origin-as-secure` flag,
   to test over the plain-HTTP venue network. Never use this for the real
   event.

### 4. `config.yaml`: rooms and `agenda_names`

Copy `config.example.yaml` to `config.yaml` (never overwritten by
`make demo`/`make demo-fake`, which use their own versioned configs). For
each room:

```yaml
rooms:
  - id: main
    name: Main Stage
    agenda_names: [gran-sala]   # names this room goes by in the external agenda
    source_type: youtube        # file | url | youtube | emitter
    source_url: "https://www.youtube.com/watch?v=..."
    language: es                # the room's free-session source language
    default_targets: [en]
```

`agenda_names` is how an agenda import maps a talk to this room when the
agenda's own room name doesn't match this room's `id` or `name` (e.g.
Nerdearla's sessions API uses room slugs like `gran-sala`). For a room
station (a mini PC on a stage), use `source_type: emitter` with any
non-empty placeholder `source_url` — see step 6.

### 5. Import Nerdearla's agenda, by URL or CSV

`POST /api/admin/agenda/import` (multipart form), after logging in at
`/admin`:

```bash
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' \
  -F url=https://backstage.nerdearla.com/api/sessions/?event_id=<uuid> \
  http://localhost:8000/api/admin/agenda/import
# or: -F file=@agenda.csv
```

`format` is optional (guessed as `csv` or `nerdearla` from the name/
content); `room_map` (optional JSON, `{"agenda room name": "room id"}`)
overrides the automatic `id`/`name`/`agenda_names` matching. CSV columns
(`agenda.example.csv`): `sala, inicio, fin, titulo, speakers, idioma,
destinos, motor, abstract, tags, glosario` — `speakers`/`destinos`/`tags`/
`glosario` are `;`-separated, `idioma` is `es` or `en`, a blank `motor`
defaults to `glossary` for Spanish and `default_engine_en` for English,
and a glossary entry is either a bare term (kept untranslated) or
`term=translation`. Import again any time to update still-scheduled talks;
a malformed row rejects the whole file with its row number, and talks
already live or done are never touched.

### 6. Review and edit talks and glossaries

There's no browse/edit form in the panel yet — use the same admin API:

```bash
# list today's talks for a room
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' \
  "http://localhost:8000/api/admin/talks?room=main"

# edit a title, targets or glossary (a LIVE talk only accepts these three)
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"glossary":[{"term":"Kubernetes"},{"term":"control plane","translation":"plano de control","keep_in_english":false}]}' \
  "http://localhost:8000/api/admin/talks/<talk_id>"
```

A glossary term is either kept as-is (`keep_in_english: true`, the
default — just `{"term": "Kubernetes"}`) or translated
(`keep_in_english: false` with a `translation`). This is the same
glossary the **glossary** engine enforces live and the **fast** engine
(Live Translate) does not (`docs/alternatives.md`).

### 7. Room stations: one per mini PC, with their link and kiosk mode

For each `source_type: emitter` room, the mini PC opens:

```
https://<your-domain>/station/<room-id>?key=<station_key>
```

`station_key` is derived from `ADMIN_PASSWORD` and the room id
(`glosa.web.station.station_key` — HMAC-SHA256, stable across restarts,
revoked by changing `ADMIN_PASSWORD`). A logged-in admin can also open
`/emitter/<room-id>` to get redirected there without typing the key by
hand. **QR code for this link:** not generated by Glosa yet in this build
— there is no `/qr` route or QR-image code in this codebase.
<!-- TODO(controller): confirm whether Task 14b's QR page has landed by the event, and add the real route/flow here instead of this note. -->

Start Chrome in kiosk mode on each mini PC (README's *Room stations*):

```bash
google-chrome \
  --kiosk "https://<your-domain>/station/<room>?key=<station_key>" \
  --autoplay-policy=no-user-gesture-required \
  --user-data-dir=/home/glosa/chrome-station-<room>
```

Use a **persistent** `--user-data-dir` (not the ephemeral default) — grant
the microphone permission once (click "Allow" the first time) and Chrome
remembers it for every later launch, including after a remote reload or a
reboot. The station page itself shows **"Iniciar estación" / "Start
station"** to begin capture (`glosa/i18n.py`: `station_start`), and its own
audio-state words are **"Audio OK"**, **"Conectando…" / "Connecting…"**,
**"Reconectando…" / "Reconnecting…"**, **"Sin señal" / "No signal"**
(same file, `station_audio_*` keys) — the operator doesn't need to touch
these, they're what the person plugging in the mic sees.

### 8. Test each room

- For a `file`/`url`/`youtube` room, `make demo` (or your own
  `config.yaml` with a real `source_url`) plays real audio at real speed —
  watch `/s/<room>` in a browser and confirm captions appear.
- For an `emitter` room station, open its kiosk URL, allow the microphone,
  and check `/s/<room>` the same way.
- Glosa also has a "Probar con audio" ("Test with audio") capability
  already implemented at the code level — `RoomWorker.play_file(path)`
  plays a file at real-time speed as the room's live source, keeping the
  running talk's engine session, so you can sanity-check a room without a
  live mic — but **there is no admin endpoint or button that calls it
  yet**; it isn't reachable over HTTP in this build.
  <!-- TODO(controller): confirm if/when "Probar con audio" gets an admin route, and replace this note with the real button name and endpoint. -->

### Checklist

- [ ] `.env`: `GEMINI_API_KEY` (paid tier), `ADMIN_PASSWORD` (≥8 chars),
      `TYPESAFE_API_KEY` if you want the quality meter
- [ ] `budget_usd` set in `config.yaml`; know where to check remaining
      Gemini credit (Google AI Studio)
- [ ] HTTPS reachable from the venue network (Caddy or a tunnel) — test a
      station URL from a phone/mini PC on that network, not just localhost
- [ ] Every room defined in `config.yaml`, with correct `agenda_names`
- [ ] Agenda imported; spot-check a few talks' languages/targets/glossary
- [ ] Every `emitter` room station: kiosk mode running, mic permission
      granted once, captions confirmed on `/s/<room>`
- [ ] Every `file`/`url`/`youtube` room: source reachable, captions
      confirmed on `/s/<room>`
- [ ] `data/` on a persisted volume/disk (Docker: the `./data:/app/data`
      bind mount already in `docker-compose.yml`)

## During the event

### Reading room status

`GET /api/admin/rooms` (same auth) returns every room's `mode`
(`auto`/`manual`), `status` and current/next talk. `status.state` is one
of four values, computed by `RoomHealth.evaluate()` (`glosa/metrics.py`)
in this precedence — `idle` (no talk running) beats `red` beats `yellow`
beats `green`:

| `state` | Meaning | `status.detail` you'll see |
|---|---|---|
| `green` | Healthy, talk running. | `"ok"` |
| `yellow` | Degraded, still running. | `"latency {p50}s exceeds 5.0s"`, `"average quality {q} below 0.5"`, `"level {db}dB below -50dB with active talk"` (audio present but very quiet), or `"degraded: recent reconnect in the last 60s"` |
| `red` | Needs attention now. | `"stalled: audio present but no engine output"` (voice going in with no output for 8+ s — `StallWatchdog`, `glosa/engines/watchdog.py` — the relay should be reconnecting that session on its own), `"source is down"` (**fuente caída**: no audio for a long time — see below), or `"payment blocked: budget exhausted"` (**crédito**: the Gemini project's own prepaid credit ran out — see `docs/costs.md`) |
| `idle` | No talk in progress. | `"no talk in progress"` |

**"Fallback de motor" (falling back to another model):** this exists, but
only inside the **glossary** engine's translation step, and it's silent —
not surfaced as a room alert. `glosa.text.translator.Translator` retries a
failed `gemini-3.5-flash-lite` call, and after enough consecutive
retryable failures switches to `gemini-3.1-flash-lite`
(`fallback_model`) for the rest of that call — nothing is logged to
`db.log_event` or reflected in `RoomStatus` when this happens.
<!-- TODO(controller): confirm whether engine-fallback gets a visible alert/log entry before the event; until then, an operator has no way to tell from the panel that a translation was served by the fallback model. -->

The panel's future **Atención** bar lists exactly the rooms that aren't
`green`/`idle`, ranked by how bad they are, and says "Todo en orden" ("All
clear") when there's nothing to act on — that's the approved design
(`docs/design/README.md` §6), not yet the running UI in this build. Until
it lands, poll `GET /api/admin/rooms` (or watch the room list reload after
clicking **Start**/**Stop**, which does refresh from the server) for the
same information.

**"Sin audio" (no audio) vs. "fuente caída" (source down):** a room turns
`red` with `"source is down"` only once the audio source has been silent
long enough that `AudioIngest` gives up retrying (~31 s without audio,
`glosa/room.py`'s module docstring) — a brief dropout that self-heals
doesn't turn the room red. Once it *is* red for this reason, the talk
keeps running (the engine session doesn't tear down) until you fix the
source:

- **Reopen the source:** `POST /api/admin/rooms/<id>/start` restarts the
  room's configured source (the current talk, or the free session) and
  takes it off autopilot into `manual` mode.
- **Reconnect** (`POST /api/admin/rooms/<id>/reconnect`) is a **different**
  action: it replaces only the running *engine session* for the *same,
  still-working* audio, without touching the source. The code's own name
  for this is **"Reconectar"** (`glosa/room.py`: *"The admin's
  'Reconectar': replace the running engine session now"*) — use it for a
  stalled/degraded engine connection, not for a genuinely dead audio feed.

Neither has a button in this build's `/admin`; both are plain `POST`s with
no body:

```bash
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' -X POST \
  http://localhost:8000/api/admin/rooms/<id>/reconnect
```

### Reload a station ("F5 remoto")

If a mini PC's browser tab freezes, `POST /api/admin/rooms/<id>/station/reload`
sends it a reload over its own WebSocket — no RustDesk, no walking over to
the stage (README's *Room stations*, "F5 remoto"). Not yet a button in
`/admin`.

### Auto vs. Manual, and when to switch

Each room is `auto` (default) or `manual`, persisted in the database.
`auto` follows the agenda: a talk opens **60 seconds** before its
scheduled start (`glosa/scheduler.py`'s `LEAD_S = 60.0`) and closes at its
end (or when the next one's own 60 s lead arrives first). Any manual
action — **Start**, **Stop**, `start-talk`, `end-talk` — takes the room to
`manual` (README's *Agenda & autopilot*; also `admin_api.py`'s
`start_room`/`stop_room`, which explicitly call `autopilot.set_mode(...,
"manual")`). Switch a room to `manual` whenever the schedule and reality
disagree (a talk running long, a speaker swap, an early finish) so the
autopilot doesn't yank the room back to what the agenda says. Switch it
back to `auto` (`POST /api/admin/rooms/<id>/mode`, `{"mode": "auto"}`) once
things are back on schedule — the agenda takes over again immediately, not
up to 5 s later.

**Known gap after a restart:** an `auto` room reopens whatever talk the
agenda says is on now; a `manual` room resumes what it was running when
the server died. Which talks already ended *early*, inside their own
slot, is only remembered in memory — so a talk you ended early can reopen
on its own after a restart if its slot is still current. If that happens,
switch that room to `manual`.

### What the public sees

Regardless of the room's internal `green`/`yellow`/`red`/`idle` state, the
audience view (`glosa/web/static/js/room.js`) only ever shows one of three
words, by design ("red never means something scarier to the public than
'reconnecting'" — `docs/design/README.md` §4):

| Internal state | Audience sees (ES / EN) |
|---|---|
| `green` or `yellow` | **En vivo** / **Live** |
| `red` | **Reconectando** / **Reconnecting** |
| `idle` | **Entre charlas** / **Between talks** |

(`glosa/i18n.py`: `live`, `reconnecting`, `between_talks`; the mapping
itself is `room.js`'s `{ green: "live", yellow: "live", red: "down", idle:
"idle" }` → `{ live: T.live, down: T.reconnecting, idle: T.between_talks
}`.) The public never sees a "degraded" or "payment blocked" message —
only you, in the operator-facing status, see the real reason.

## After the event

**Exports.** `glosa/exports.py` already implements `to_srt`/`to_vtt`/
`to_txt` from a talk's saved segments, and every segment is saved with the
timing it needs (`Settings.default_export_shift_s = 2.4`, the measured p50
translation delay, applied when rendering). There is **no admin endpoint
or download link for this yet** in this build — the higher-quality,
`gemini-3.8-flash` re-translation pass meant to produce a "corrected"
export (`glosa/text/corrector.py`'s `correct_segments`, already
implemented and tested) is also not yet hooked up to run automatically
when a talk ends and save its output as that talk's corrected version.
<!-- TODO(controller): confirm whether an exports endpoint/UI landed before the event and document the real path (e.g. "see the Exports section in the admin") here instead of this note. -->

**Costs.** See `docs/costs.md` for the full breakdown. Quick check: each
room's running spend is `status.cost_usd` from `GET /api/admin/rooms`
(a total since the server process started, not per-talk); the durable,
cross-restart total lives in the SQLite `costs` table but nothing exposes
it as a single number yet.

**Back up `data/`.** Everything durable — the agenda, every saved caption
segment, the event log, and the cost log — is one SQLite file,
`Settings.db_path` (default `data/glosa.db`; `data/demo.db` and
`data/demo-fake.db` for the demo configs). In Docker it's the
`./data:/app/data` bind mount in `docker-compose.yml`, already set to
survive a restart or rebuild — copy that file (or the whole `data/`
directory) somewhere else after the event, and periodically during a
multi-day one.
