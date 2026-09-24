# Glosa

Live captions and translation for conference talks, room by room, straight from an audio source into your phone.

## Quickstart

```bash
git clone https://github.com/martin-paladino/glosa.git && cd glosa
cp .env.example .env   # fill in GEMINI_API_KEY and ADMIN_PASSWORD, see Credentials below
make demo               # or: docker compose up
```

Open **http://localhost:8000**. Two rooms come up captioning the bundled sample clips (`samples/en_clip.opus`, `samples/es_clip.opus`) at real speed, in English and Spanish, each with a live translation into the other language. `make demo` runs Gemini for real (costs about $0.10 for the two ~90s clips); `make demo-fake` does the same with no API key and no cost, replaying a recorded session instead.

To run your own event instead of the demo:

```bash
cp config.example.yaml config.yaml   # describe your rooms, agenda, branding
make run                              # or: docker compose up (uses the same config.yaml)
```

`config.yaml` is never touched by `make demo`/`make demo-fake` (they use the versioned `config.demo.yaml` / `config.demo-fake.yaml` instead), so you can try the demo and set up your real event side by side.

## Credentials (`.env`)

| Variable | Required | What it's for |
|---|---|---|
| `GEMINI_API_KEY` | yes | Drives live captioning/translation (Gemini Live). Get one at [Google AI Studio](https://aistudio.google.com/). Billed per minute of audio (see *How it scales* below); `make demo-fake` needs no key at all. |
| `ADMIN_PASSWORD` | yes | The single password for `/admin` (start/stop rooms). Any string; the app won't start without one. |
| `TYPESAFE_API_KEY` | no | Enables the Jev quality meter. Leave blank to skip it — everything else works without it. |

Secrets live only in `.env` (gitignored). Never put them in `config.yaml` or commit them.

## What's in the box

- **Audience view** (`/`, `/s/{room}`): live captions per room over SSE, phone-first, EN/ES interface, light/dark/high-contrast themes.
- **Admin** (`/admin`): log in with `ADMIN_PASSWORD`, see every room's state and current talk, start/stop each one. (A fuller production console — audio levels, quality, cost, an event log — lands in a later milestone; this is the minimum needed to run the MVP.)
- **Docker**: `docker-compose.yml` builds the same app, mounts `.env` and persists `data/` (the SQLite database: agenda, captions, cost) across restarts.

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
