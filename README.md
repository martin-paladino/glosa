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
# {"imported": 5, "skipped": []}
```

The CSV columns are those of `agenda.example.csv`: `sala, inicio, fin, titulo, speakers, idioma, destinos, motor, abstract, tags, glosario`. Times are local to `timezone` unless they carry an offset; `speakers`, `destinos`, `tags` and `glosario` are `;`-separated; `idioma` is `es` or `en`; a blank `motor` means `glossary` for Spanish and `default_engine_en` for English; a glossary entry is `Term` (kept as is) or `term=translation`. A malformed row rejects the whole file with its row number. Importing again updates the talks that are still scheduled and never touches one that is live or done.

**Browse and edit:** `GET /api/admin/talks?room=<id>&day=YYYY-MM-DD` (default: today), `GET /api/admin/talks/<id>`, and `PUT /api/admin/talks/<id>` with any of `title, speakers, language, targets, engine, start, end, abstract, tags, glossary` (a live talk only takes `title`, `targets` and `glossary`).

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
