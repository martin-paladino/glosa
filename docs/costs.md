# Costs

All prices below are Gemini's list prices as configured in `Prices`
(`glosa/config.py`, mirrored in `config.example.yaml`) plus what we actually
measured spending during this vibeathon. None of this is a quote or a
contract price from Google — it is what the API billed us, and the public
per-minute rate we billed against.

## Cost per room-hour, by engine

A talk's `targets` list can hold more than one language (`PUT
/api/admin/talks/<id>`'s `targets` field, or the agenda CSV's `destinos`
column). Live Translate itself still produces only one target per session
(`glosa/room.py`'s `target_lang()`: "Live Translate takes one target per
session"), so the **fast** engine covers the first target live and hands
any extra targets to the same text-translation lane (Flash-Lite) the
**glossary** engine uses for all of its targets (`glosa/room.py`'s
`_build_engine`, `glosa/room_text.py`). The table below is the cost of the
first (or only) target language; each extra target adds one more
Flash-Lite translation pass over the same transcribed text — see "Extra
target languages" below.

| Engine | US$/min of audio | US$/hour, one room | Source |
|---|---|---|---|
| **fast** (Gemini Live Translate) | 0.0368 | **≈ US$2.21** | `Prices.lt_per_min` (`glosa/config.py`); confirmed against the Live API's own usage metadata in the pre-event spike (2026-09-23, `spike-latencia/REPORT.md`: "$2.2/h por idioma"). |
| **glossary** (Transcribe Live + Flash-Lite) | ≈0.012 (transcribe 0.009 + Flash-Lite ≈0.003) | **≈ US$0.72** | Measured live during this vibeathon (2026-09-24): 6 runs of 60 s of Spanish audio, e.g. run "corrida live 6" cost US$0.0126/min (`.superpowers/sdd/2026-09-24-glosa/progress.md`). `Prices.transcribe_per_min` = 0.009 in `glosa/config.py`; Flash-Lite itself bills per token (`flash_lite_in_per_m`=0.30, `flash_lite_out_per_m`=2.50 USD/million tokens), so the per-minute total scales a little with how much is said, not just with audio duration. |

The glossary engine is roughly **3x cheaper per room-hour** than Live
Translate for one target language, on top of being the only one of the two
that respects the configured glossary (see `docs/alternatives.md`).

**Extra target languages:** a shipped feature — every target beyond the
first is translated from the already-transcribed source text by a
`TranslationLane` (Flash-Lite, the same code path the glossary engine uses
for its own translation), not by opening another Live Translate/
Transcribe-Live session. There is no separate `Prices` entry for it: its
per-minute cost is Flash-Lite's token price (`flash_lite_in_per_m`/
`flash_lite_out_per_m`), the same rate the glossary engine's translation
step bills at (≈US$0.003/min in the "glossary" row above). This lines up
with the pre-event spike's own estimate of roughly **US$0.1/h per extra
language** (`spike-latencia/REPORT.md`, 2026-09-23) — that estimate has
not been separately re-measured for multiple simultaneous targets in this
build, but it is the same mechanism the spike proposed, now shipped.

## Estimating a Nerdearla-scale event

Illustrative arithmetic only — we have not run an event at this scale.
Using the plan's own example: **3 days x 5 rooms x 8 h = 120 room-hours**.

| Scenario | Engine mix | Cost |
|---|---|---|
| All talks in English, Live Translate EN→ES | 120 room-hours x fast (US$2.21/h) | **≈ US$265** |
| All talks in Spanish, glossary engine ES→EN | 120 room-hours x glossary (US$0.72/h) | **≈ US$86** |
| Mixed (illustrative 50/50 split by room-hour) | 60 room-hours x fast + 60 room-hours x glossary | **≈ US$176** |

Engine is chosen per talk (`engine: fast\|glossary` in the agenda CSV, or
the `engine` field of `PUT /api/admin/talks/<id>`; `default_engine_en` in
`config.yaml` picks the default for an English talk that doesn't specify
one, Spanish talks default to `glossary`), so a real agenda's actual mix —
and actual cost — depends on how many talks are in each language and how
long they run, not on this even split.

For comparison, this vibeathon's own spend so far (development + live
testing, not a full event) was **≈ US$1.4 of a US$10 budget**
(`progress.md`, 2026-09-24) — far below any of the scenarios above, because
it was minutes of testing, not days of continuous captioning.

## Comparison with a commercial live-captioning SaaS

Today's setup relies on a commercial live-captioning/translation SaaS
(`docs/field-notes.md`). A typical plan's **public list price** (the
vendor's pricing page, surveyed 2026-09-23 — not a negotiated or
Nerdearla-specific rate) is:

- **US$359/month = 900 minutes of translation** → US$359 /
  15 h = **≈ US$24/h per translated language** (list price).
- **≈ US$4.8/h for captions only** (no translation) — list price, same
  source.

At the same 120-room-hour scale, 120 h x US$24/h (list) ≈ **US$2,880** if
every room-hour needed a translated language at that list rate —
roughly **11x** our all-English/fast-engine scenario above, and **~33x**
our all-glossary-engine scenario. This is a list-price-to-list-price
comparison; such plans typically bundle a monthly minute allowance
rather than pure metered per-minute billing, so an organization already
paying for a plan might have unused minutes that cost nothing marginally
— the per-hour figure is only the *list* rate implied by the plan size.

## What's not the model: server cost

Running Glosa itself needs a small amount of compute, separate from the
Gemini/TypeSafe API spend above:

- The FastAPI/uvicorn process (SQLite storage, one `ffmpeg` subprocess per
  `file`/`url`/`youtube` room) was load-tested at 50 rooms / 500 concurrent
  audience connections for 60 s: **62.5% peak CPU on one core** for the
  caption fan-out itself (average 17.9%), plus roughly **115 more
  percentage points of CPU and ~150 MB RSS from the `ffmpeg` fleet** at 50
  rooms (`bench/load-results.md`, Task 15a, 2026-09-24). A deployment using
  mostly `emitter` room stations (mini PCs push audio in; no local
  `ffmpeg`) would show a much smaller "tree" number.
- That fits on a small cloud VM — we did not price a specific instance
  type or provider, so: budget **a few US dollars a day** for a small VM
  (a couple of vCPUs, a few GB of RAM) at any major cloud provider's
  on-demand list price, and check your provider's current small-instance
  pricing before the event rather than relying on this figure.

## The budget cap (`budget_usd`)

`config.yaml`'s `budget_usd` (default US$10.0 in `config.example.yaml`;
this vibeathon ran on the same US$10 prepaid project) is the event's
expected spending ceiling. Two different things enforce/represent it
today, and they are not the same mechanism:

1. **Real and wired: the vendor's own credit running out.** When Gemini's
   prepaid project actually runs out of credit, the Live API returns an
   error Glosa recognizes as a payment stop (`code == 402`, or a
   "RESOURCE_EXHAUSTED"-with-billing-wording match — see
   `glosa/engines/live_translate.py` and `glosa/engines/transcribe.py`).
   That room's `SessionRelay` sets `payment_blocked = True`
   (`glosa/engines/relay.py`), which `RoomHealth.evaluate()`
   (`glosa/metrics.py`) turns into the room state **`red`**, detail
   `"payment blocked: budget exhausted"` — and the relay **keeps retrying
   every 30 s** in case the project is topped up, rather than giving up.
2. **Wired to the admin console: an 80%-of-budget warning.**
   `glosa/metrics.py`'s `CostTracker.alert()` returns `"80%"` once spend
   reaches 80% of `budget_usd`, and `"exhausted"` at or past 100% — this is
   unit-tested (`tests/test_metrics.py`) and now built into the admin
   panel's live feed: `GET /api/admin/stream` constructs a `CostTracker`
   from `Settings.budget_usd` every tick (`glosa/web/admin_stream.py`) and
   sends its `alert` to the browser. The panel (`admin.html`/`admin.js`)
   shows a **spend meter** ("Gasto"/"Spend") in the masthead that fills as
   the event spends its budget and turns into a warning or "over" state at
   80%/100%; the same alert also adds a row to the **Atención** bar (see
   below) once it fires, and a room whose engine is actually payment-
   blocked (case 1, above) is reported as `"exhausted"` regardless of the
   tracked total. Two different totals coexist: `GET /api/admin/rooms`'s
   `status.cost_usd` is each `RoomWorker`'s own in-memory counter (resets
   on a restart), while the panel's spend meter and the 80%/exhausted
   alert are computed from `db.cost_by_room()` — the durable, all-time sum
   of the SQLite `costs` table (`glosa/db.py`), summed across every room
   (`glosa/web/admin_stream.py`) — so the meter keeps counting correctly
   across a restart even though the per-room API figure does not. See
   `docs/operator-guide.md`.

Operationally: watch Google AI Studio's own usage/billing page as the
source of truth for remaining credit, and treat a room going red with
"payment blocked: budget exhausted" as the sign to either top up the
project's credit or accept that room stays down (it will auto-recover once
credit is added, no restart needed).
