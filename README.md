# Glosa

Live captions and translation for conference talks, room by room, straight from an audio source into your phone.

## Prerequisites

Either **Docker** (with Compose), or, to run it directly: **uv**, **ffmpeg** and **make**.

## Quickstart

```bash
git clone https://github.com/martin-paladino/glosa.git && cd glosa
cp .env.example .env   # fill in GEMINI_API_KEY and ADMIN_PASSWORD, see Credentials below
make demo               # or: docker compose up
```

Open **http://localhost:8000**. Two rooms come up captioning the bundled sample clips (`samples/en_clip.opus`, `samples/es_clip.opus`) at real speed, in English and Spanish, each with a live translation into the other language. `make demo` / plain `docker compose up` run Gemini for real (costs about $0.10 for the two ~90s clips); `make demo-fake` / `GLOSA_CONFIG=config.demo-fake.yaml docker compose up` do the same with no API key and no cost, replaying a recorded session instead.

To run your own event instead of the demo:

```bash
cp config.example.yaml config.yaml   # describe your rooms, agenda, branding
make run                              # or, with Docker: see below
```

`config.yaml` is never touched by `make demo`/`make demo-fake` (they use the versioned `config.demo.yaml` / `config.demo-fake.yaml` instead), so you can try the demo and set up your real event side by side.

**Running your own event with Docker:** `config.yaml` is deliberately never baked into the image (see `.dockerignore`) or read from the host's environment (see *Secrets* below), so mount it explicitly and point `GLOSA_CONFIG` at it:

```bash
GLOSA_CONFIG=config.yaml docker compose run --rm -p 8000:8000 -v "$PWD/config.yaml:/app/config.yaml:ro" glosa
```

or add the same volume line to a local `docker-compose.override.yml` (Compose merges it automatically) so plain `GLOSA_CONFIG=config.yaml docker compose up` picks it up every time.

## Credentials (`.env`)

| Variable | Required | What it's for |
|---|---|---|
| `GEMINI_API_KEY` | yes | Drives live captioning/translation (Gemini Live). Get one at [Google AI Studio](https://aistudio.google.com/). Billed per minute of audio (see *How it scales* below); `make demo-fake` needs no key at all. |
| `ADMIN_PASSWORD` | yes | The single password for `/admin` (start/stop rooms). At least 8 characters — Glosa refuses to start otherwise. Use a long random one, e.g. `openssl rand -base64 18`; it's the only thing standing between the internet and your rooms' start/stop controls. |
| `TYPESAFE_API_KEY` | no | Enables the Jev quality meter. Leave blank to skip it — everything else works without it. |

Secrets live only in `.env` (gitignored) and are read from that file directly, never from the shell/container environment (so a stray exported variable, or `docker inspect`, can't leak them — see `docker-compose.yml`'s comment). Never put them in `config.yaml` or commit them.

## What's in the box

- **Audience view** (`/`, `/s/{room}`): live captions per room over SSE, phone-first, EN/ES interface, light/dark/high-contrast themes.
- **Admin** (`/admin`): log in with `ADMIN_PASSWORD`, see every room's state and current talk, start/stop each one. (A fuller production console — audio levels, quality, cost, an event log — lands in a later milestone; this is the minimum needed to run the MVP.)
- **Docker**: `docker-compose.yml` builds the same app, mounts `.env` and persists `data/` (the SQLite database: agenda, captions, cost) across restarts.

## Agenda & autopilot

Load the event's agenda once and the rooms follow it. Every endpoint below needs the admin session cookie (log in at `/admin`) and the `X-Glosa-Admin: 1` header.

**Import** with `POST /api/admin/agenda/import` (multipart form):

- `file`: a CSV or Nerdearla's sessions JSON, **or** `url`: the server fetches it (http/https only), e.g. `https://backstage.nerdearla.com/api/sessions/?event_id=<uuid>`;
- `format` (optional): `csv` or `nerdearla`, guessed from the name or the content;
- `room_map` (optional): JSON `{"agenda room name": "room id"}`. By default a talk goes to the room whose `id`, `name` or `agenda_names` (config.yaml) matches its room.

```bash
curl -b 'glosa_admin=<cookie>' -H 'X-Glosa-Admin: 1' -F file=@agenda.example.csv http://localhost:8000/api/admin/agenda/import
# {"imported": 5, "skipped": [], "removed": []}
```

The CSV columns are those of `agenda.example.csv`: `sala, inicio, fin, titulo, speakers, idioma, destinos, motor, abstract, tags, glosario`. Times are local to `timezone` unless they carry an offset; `speakers`, `destinos`, `tags` and `glosario` are `;`-separated; `idioma` is `es` or `en`; a blank `motor` means `glossary` for Spanish and `default_engine_en` for English; a glossary entry is `Term` (kept as is) or `term=translation`. A malformed row rejects the whole file with its row number. Importing again updates the talks that are still scheduled and never touches one that is live or done. It also drops, for each room and day the new file covers, the scheduled talks it no longer lists (cancelled, or re-titled or moved, which changes a CSV talk's id); they come back in `removed`.

**Browse and edit:** `GET /api/admin/talks?room=<id>&day=YYYY-MM-DD` (default: today), `GET /api/admin/talks/<id>`, `PUT /api/admin/talks/<id>` with any of `title, speakers, language, targets, engine, start, end, abstract, tags, glossary` (a live talk only takes `title`, `targets` and `glossary`; new targets apply the next time the talk starts), and `DELETE /api/admin/talks/<id>` (scheduled talks only).

**Autopilot.** Each room is in `auto` (the default) or `manual` mode, stored in the database so it survives a restart. In `auto`, a room with agenda talks today follows the clock: each talk opens a minute before its start and closes at its end (when the next one's minute of lead arrives first, the current one closes then), and between talks the room is idle. If the operator opened the next talk early and then hands the room back to `auto`, that talk keeps going through its slot. A room with no talks today keeps its free session. In `manual`, the autopilot leaves the room alone.

After a restart, an `auto` room reopens the talk the agenda says is on, and a `manual` room resumes the talk it was running when the server died (other manual rooms stay idle, with no free session). Known limitation: which talks already ended inside their slot is remembered only in memory, so after a restart a talk that was ended early (say, the speaker finished ahead of time) reopens if its slot is still on; switch the room to `manual` if that happens.

| Endpoint (`POST /api/admin/rooms/<id>/...`) | What it does |
|---|---|
| `mode` `{"mode": "auto"\|"manual"}` | Switch modes. Back to `auto`, the room follows the agenda again right away. |
| `start-talk` `{"talk_id": "..."}` | End the current talk and open this one now. The room goes `manual`. |
| `end-talk` | End the current talk; the room goes idle and `manual`. |
| `reconnect` | Open a new engine session for the running talk (the mode stays). |
| `start` / `stop` | Start (free session or current talk) or stop the room. The room goes `manual`. |

`GET /api/admin/rooms` lists every room with its mode, status, current talk and next talk.

## Room stations (mini PC per stage)

Configure a room with `source_type: emitter` in `config.yaml` (`source_url` can be any non-empty placeholder — the room's audio comes from a connected station, not a URL) and its mini PC opens `/station/<room>?key=<station_key>` instead of a SaaS tab: it captures the audio desk's feed from the browser, streams it to Glosa, and shows the room's own captions full screen for the stage screens (`docs/field-notes.md` has the story behind this). The URL is stable across a server restart — an unattended station keeps working — and is revoked by changing `ADMIN_PASSWORD`. A remote reload ("F5 remoto", no more RustDesk) is `POST /api/admin/rooms/<id>/station/reload` (same admin auth as the rest of `/api/admin/*`).

**HTTPS is required.** Browsers only allow microphone capture (`getUserMedia`) in a secure context (HTTPS or `localhost`); a mini PC opening `http://<server>:8000` on the venue network cannot capture audio, and the station page explains this on screen instead of failing silently. Three ways to get there:

1. **A reverse proxy with automatic HTTPS** (recommended for the venue): `deploy/Caddyfile` is a working example — Caddy gets a Let's Encrypt certificate for a real domain on its own, or issues a private one for a LAN-only hostname with `tls internal`. Point it at Glosa's `:8000` and open the mini PC at `https://<your-domain>/station/<room>?key=...`. `docker-compose.yml` has a commented-out `caddy` service that mounts the same Caddyfile.
2. **A tunnel** (quickest for a single stage or a rehearsal): any HTTPS tunnel to `localhost:8000` (ngrok, Cloudflare Tunnel, Tailscale Funnel...) works — the browser only cares that the *origin it loaded* is secure, not how it got there.
3. **Lab only, no real HTTPS:** start Chrome with `--unsafely-treat-insecure-origin-as-secure=http://<server-ip>:8000 --user-data-dir=/tmp/glosa-lab` to test over the plain-HTTP venue network without setting up a proxy first. Never do this for the real event — it turns off a real browser security check.

**Kiosk mode**, for an unattended mini PC:

```bash
google-chrome \
  --kiosk "https://<your-domain>/station/<room>?key=<station_key>" \
  --autoplay-policy=no-user-gesture-required \
  --user-data-dir=/home/glosa/chrome-station-<room>
```

- `--kiosk`: full screen, no browser chrome; Chrome exits instead of prompting on close.
- `--autoplay-policy=no-user-gesture-required`: lets the page start its `AudioContext` on load instead of waiting for a click — combined with the persistent mic permission below, the station starts captioning with nobody touching it.
- A **persistent `--user-data-dir`** (a real path, not the default ephemeral one) is what makes the microphone permission stick: grant it once — click "Allow" the first time the page asks — and Chrome remembers it for that origin in that profile across every later launch, including after `POST .../station/reload` or a full reboot.

## How it scales

- **One instance comfortably handles about 10-20 rooms captioned at once.** The bottleneck is the number of concurrent Gemini Live sessions and their network I/O (each room keeps 1-2 sessions open for the session-handoff overlap), not CPU: ffmpeg, voice detection and segmentation are cheap per room.
- **Beyond that, split rooms across instances** — e.g. one process per building or track, each with its own `config.yaml` subset of rooms and its own `db_path` (SQLite; no shared state between instances). Point each instance's admin panel and audience links at its own host/port.
- **Gemini's per-tier rate and concurrency limits cap how many rooms one API key can drive at once.** The free tier is only good for a quick trial, not an event: check your current tier's limits in Google AI Studio before the day and move to a paid tier with higher throughput ahead of time, especially if you're running several rooms for several hours. Budget by audio-minutes: Live Translate is about $0.037/min per room (`prices.lt_per_min` in `config.yaml`), so 2 rooms running 8 hours is on the order of $35 — watch `budget_usd` and the running cost as the event gets bigger.

## Development

```bash
make test   # fast suite, no API calls (pytest -m "not live")
```

Sample clips and their provenance: `samples/README.md`.

## License

Apache-2.0. See `LICENSE`.

**Trademarks:** the bundled Nerdearla logos are not covered by that license — see `glosa/web/static/branding/nerdearla/NOTICE.md`.
