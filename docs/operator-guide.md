# Operator guide

Who this is for: whoever sets up and watches Glosa during a live event. It
assumes you've read the README's Quickstart. Button/label names below are
quoted exactly as they appear in the code (`glosa/web/templates/*.html`,
`glosa/i18n.py`, `glosa/web/static/js/admin.js`) — where the panel doesn't
have a button for something yet, this guide says so and shows the API call
instead, rather than inventing a label that isn't in the UI.

**State of the admin panel in this build.** `/admin` (`admin.html`,
`admin.js`) is the full **"Sala de control"** console described in
`docs/design/README.md`/`docs/design/admin.html`: a live-updating
**Atención** (Attention) bar that lists only the rooms that need action
and says "Todo en orden" ("All clear") when none do, a monitor per room
(status light, current talk, time on air), a spend meter against
`budget_usd` in the masthead, an event log (alerts-first or all events),
today's agenda with the next automatic change, and a side drawer per room
(click it or press 1–9, Esc closes) with mode (**Auto**/**Manual**),
**Iniciar charla**/**Start talk**, **Terminar charla**/**End talk**,
**Reconectar**/**Reconnect**, **Probar con audio**/**Test with audio**,
**Escuchar el audio**/**Listen to the audio**, an **Exportaciones**/
**Exports** list, and — for a room station — its connection state and
**Recargar estación**/**Reload station**. Button/label names below are
quoted exactly as they appear in `glosa/i18n.py`. Everything the panel
does also has a plain `/api/admin/...` HTTP call behind it (same auth:
the `/admin` session cookie plus an `X-Glosa-Admin: 1` header — see
README's *Agenda & autopilot*); this guide gives the `curl` form too where
it is useful for scripting or troubleshooting, not because the panel lacks
the button.

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
station (a mini PC on a stage), use `source_type: emitter` and omit
`source_url` entirely — the station brings its own audio — see step 6.

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

Click a talk in the agenda schedule to open its edit drawer: title,
speakers, language, targets, engine, start/end, abstract, tags and
glossary, plus a **Sugerir glosario**/**Suggest glossary** button (via the
admin) that proposes terms from the abstract/tags. **Guardar cambios**/
**Save changes** writes the edit; **Borrar charla**/**Delete talk**
removes a still-scheduled talk (with a confirm step). The same admin API
works for scripting:

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
hand. **QR code for the station link:** the room's drawer has a
**Mostrar el QR**/**Show the QR code** disclosure next to the copyable
station link that renders it as a QR image, server-side, from the same
admin-only URL (behind a TLS proxy, built from `X-Forwarded-Proto`/
`X-Forwarded-Host`, so it is the public `https://` address — Ruling 52).
This is separate from `/qr/{room}`, the printable/projectable page for the
room's *audience* link (README's *What's in the box*); the station QR is
only ever shown inside the authenticated admin drawer, never on a public
page, since it carries the capture key.

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
- Any room can also be sanity-checked without a live mic or source: open
  its drawer and click **Probar con audio**/**Test with audio**, then
  either sample button (**Muestra en inglés**/**English sample**,
  **Muestra en español**/**Spanish sample** — the repo's own
  `samples/{en,es}_clip.opus`) or upload a file (max 50 MB). This calls
  `POST /api/admin/rooms/<id>/test-audio` (multipart form, `sample: en|es`
  or `file: <upload>`), which plays the clip at real-time speed as the
  room's audio via `RoomWorker.play_file()`, as a test session: it is
  refused (409, with the reason in the drawer) while an agenda talk is
  open in the room or, in auto mode, due within the autopilot's 60-s lead,
  so a test never ends or pollutes a talk; a running free session switches
  back to its own source when the clip ends, an idle room gets a session
  of its own, and the autopilot leaves it alone until the clip ends (a
  talk that comes due still takes over). While a room plays
  a test file, an admin can also click **Escuchar el audio**/**Listen to
  the audio** to hear that same audio synchronized with the live captions
  (`GET /api/admin/listen/<id>`; Ruling 51: admin-only, and only while the
  room is in test mode — the public never sees this button).

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
| `red` | Needs attention now. | `"stalled: audio present but no engine output"` (voice going in with no output for 8+ s — `StallWatchdog`, `glosa/engines/watchdog.py` — the relay should be reconnecting that session on its own), `"source is down"` (**fuente caída**: no audio for a long time — see below), or `"payment blocked: budget exhausted"` (**crédito**: the Gemini project's own prepaid credit ran out — see `docs/costs.md`) / `"payment blocked: spending cap reached"` (**tope de gasto**: the project hit its monthly spending cap — raise it at ai.studio/spend) |
| `idle` | No talk in progress. | `"no talk in progress"` |

**Two different things are both called "fallback"; only one is visible
today:**

- **Room-level: a `fast` talk falls back to `glossary` (Ruling 48).** When
  Live Translate keeps failing (`FlapDetector`: 3 incidents within 2 min)
  or halts outright, `RoomWorker._fall_back()` swaps the running talk to
  the glossary engine hot, without reopening the audio source, and *is*
  logged: a `"warning"`/`"fallback"` event with the message `"fallback:
  glossary engine"` (`glosa/room.py`) goes to `db.log_event`, so it shows
  up in the admin's event **log** (visible with the "alerts" filter) —
  there is no separate Atención row for it, since the room itself stays
  `green` once the swap succeeds.
- **Text-model level: Flash-Lite retries onto its fallback model, inside
  the glossary engine's translation step, and this one is still silent.**
  `glosa.text.translator.Translator` retries a failed
  `gemini-3.5-flash-lite` call, and after enough consecutive retryable
  failures switches to `gemini-3.1-flash-lite` (`fallback_model`) for the
  rest of that call — nothing is logged to `db.log_event` or reflected in
  `RoomStatus` when this happens; an operator has no way to tell from the
  panel that a given translation was served by the fallback model.

The **Atención** bar lists every room that's `red` or `yellow` (never
`green`/`idle`), ranked by severity; each room row carries a one-click
**Reconectar**/**Reconnect** button (same label either way;
`glosa/web/admin_stream.py`'s `classify()` picks the actual endpoint
behind it — `restart` for "source is down", `reconnect` for a stalled/
halted engine or high latency — the admin's `runAction()` posts to
whichever one the issue calls for). Once the budget's 80%/exhausted alert
fires (above), a separate, room-less budget row is added too, with no
action button of its own. The bar says "Todo en orden. La sala está
dentro de los márgenes."/"All clear. The room is within its limits." when
there is nothing to act on. Every room's own monitor card carries the
same one-click button when it has an active issue. `GET /api/admin/rooms`
(polling, or scripting) gives the same information as the panel.

**"Sin audio" (no audio) vs. "fuente caída" (source down):** a room turns
`red` with `"source is down"` only once the audio source has been silent
long enough that `AudioIngest` gives up retrying (~31 s without audio,
`glosa/room.py`'s module docstring) — a brief dropout that self-heals
doesn't turn the room red. Once it *is* red for this reason, the talk
keeps running (the engine session doesn't tear down) until you fix the
source:

- **Reopen the source:** `POST /api/admin/rooms/<id>/restart` opens the
  room's configured source again for the talk it's running, **without**
  taking the room off autopilot (the mode stays) — this is the Atención
  bar's/monitor card's **Reconectar**/**Reconnect** action when the issue
  is "source is down". `POST .../start` is a different, blunter action:
  it (re)starts the room's current talk or free session **and** takes it
  off autopilot into `manual` mode — use it from the drawer's own
  **Start**/**Stop** buttons, not as the first response to "source is
  down".
- **Reconnect** (`POST /api/admin/rooms/<id>/reconnect`) is a **different**
  action again: it replaces only the running *engine session* for the
  *same, still-working* audio, without touching the source. The code's
  own name for this is **"Reconectar"** (`glosa/room.py`: *"The admin's
  'Reconectar': replace the running engine session now"*) — it's the same
  Atención/monitor button for a stalled/halted/degraded engine, and it's
  also the drawer's own **Reconectar**/**Reconnect** button (`data-d-
  reconnect` in `admin.html`) for any room, any time.

All three are one click in the panel: the drawer's **Reconectar**/
**Reconnect** button, or the Atención bar's/monitor card's action button
(which picks `restart` or `reconnect` depending on the issue, see above).
They are also plain `POST`s with no body, for scripting:

```bash
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' -X POST \
  http://localhost:8000/api/admin/rooms/<id>/reconnect
```

### Reload a station ("F5 remoto")

If a mini PC's browser tab freezes, the drawer's **Recargar estación**/
**Reload station** button (`POST /api/admin/rooms/<id>/station/reload`)
sends it a reload over its own WebSocket — no remote desktop, no walking over to
the stage (README's *Room stations*, "F5 remoto").

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

**Exports.** `glosa/exports.py` implements `to_srt`/`to_vtt`/`to_txt` from
a talk's saved segments, and every segment is saved with the timing it
needs (`Settings.default_export_shift_s = 2.4`, the measured p50
translation delay, subtracted from the caption-arrival times when rendering). Each room's drawer has an
**Exportaciones**/**Exports** list (one entry per finished talk in that
room, `GET /api/admin/exports`) with a **En vivo**/**Live** download link
per target language plus a **Corregida**/**Corrected** one, whose status
reads **Generándose…**/**Building…**, **Lista**/**Ready** or
**Falló**/**Failed**. The corrected version is the higher-quality
`gemini-3.8-flash` re-translation pass (`glosa/text/corrector.py`'s
`build_corrected`) and it now runs automatically: `create_app`'s
`on_talk_end` hook (`glosa/web/app.py`) kicks it off for every target
language as soon as a talk ends (including a talk the server finds still
"live" at boot, closed as stale). The download links themselves are
public routes, `GET /exports/{talk_id}/{lang}.{srt,vtt,txt}` (`?version=
corrected` for the corrected one) — served without login when
`Settings.exports_public` is `true` (the `config.example.yaml` default),
admin-only otherwise.

**Costs.** See `docs/costs.md` for the full breakdown. Quick check: the
panel's **Gasto**/**Spend** meter (masthead) is the durable, all-event
total (`db.cost_by_room()`, summed across rooms — survives a restart);
`GET /api/admin/rooms`'s `status.cost_usd` per room is a separate,
in-memory counter that resets when the server process restarts.

**Back up `data/`.** Everything durable — the agenda, every saved caption
segment, the event log, and the cost log — is one SQLite file,
`Settings.db_path` (default `data/glosa.db`; `data/demo.db` and
`data/demo-fake.db` for the demo configs). In Docker it's the
`./data:/app/data` bind mount in `docker-compose.yml`, already set to
survive a restart or rebuild — copy that file (or the whole `data/`
directory) somewhere else after the event, and periodically during a
multi-day one.
