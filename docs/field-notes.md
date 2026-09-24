# Field notes: how Nerdearla runs live captions today

Source: the official `#nerdearla-vibeathon` channel on the sysarmy Discord, 2026-09-24. Participants asked the Nerdearla staff how they currently caption each stage, and the staff answered. What follows is a summary with a few short quotes (originally in Spanish). It records what we learned and how Glosa responds to each point.

## What the organizers told us

| Topic | Current setup | Pain point |
|---|---|---|
| **Audio source** | The mics come out of the stage's audio interface through a **3.5 mm jack into a mini PC**, and the input is captured **in a web browser**. | None mentioned. It is simple and works. |
| **Engine** | A commercial SaaS listens to that input and transcribes and translates until someone presses stop. | Paid per session; nothing is automatic. |
| **Operation** | No dedicated operator per room. They leave **RustDesk** running so someone can remote in "if it needs an F5 because it froze". | Freezes that need a manual reload, and remote access to every mini PC. |
| **In-room audience** | The same browser that captures the audio shows the captions on **screens in front of the stage**. | Nothing specific. |
| **Phones** | Attendees can also **scan a QR code** and follow on their phone. | Nothing specific. |
| **Online audience (stream)** | Captions are not in the stream. The staff would like to "plug it into vMix and burn the subtitles into the stream too, because we don't have live translation" for virtual attendees. | Remote viewers get no translation. The staff later said this is **an idea, not a requirement**. |

Other answers from the channel:

- The Devpost texts (elevator pitch and so on) may be written in Spanish.
- Everything is submitted through the Devpost platform. The start time was confirmed as 15:00 UTC (12:00 in Argentina).
- Several participants reported that the claim window for the Google Developer Program credits had expired. Glosa does not depend on those credits.

## What Glosa does about it

> Status on 2026-09-24 19:50 UTC: the room station (1), remote reload (2) and the overlay (5) are being built. The autopilot (3) and the audience QR flow (4) are in progress or done. This note is updated when they land.

1. **Room station.** Instead of a SaaS tab, the mini PC opens `/station/{room}?key=…`.
   - Capture:
     - it picks the audio input (the jack from the audio interface);
     - it streams 16 kHz PCM to the server over a WebSocket;
     - it reconnects on its own and buffers up to 5 s;
     - it re-acquires the device if it is unplugged;
     - it keeps the screen awake.
   - Stage display: the same page shows the captions full screen, in any language, for the stage screens. This keeps today's physical setup.
   - Stable URL: the station keeps its URL across server restarts, so an unattended mini PC keeps working.
2. **No RustDesk needed.**
   - The production panel shows each station's state: connected or not, audio level, device, and seconds since the last audio.
   - A **remote reload** button sends an "F5" to the station.
   - Rooms reconnect their model sessions on their own and raise alarms when something goes wrong.
3. **No start/stop per talk.** The agenda-driven autopilot opens and closes each talk and switches the engine, languages and glossary. Operators take over only when reality diverges from the schedule.
4. **Phones.** Attendees use the same QR flow as today: pick a room and a language, with no app and no login.
5. **Online audience.** An **OBS/vMix overlay page** with a transparent background works as a vMix browser input. It burns translated captions into the stream, which is the idea the staff mentioned.

## Deployment gotcha found while designing the station

Browsers only allow microphone capture (`getUserMedia`) in a **secure context**: HTTPS or `localhost`. A mini PC that opens `http://<server>:8000` on the venue network cannot capture audio. The README therefore covers:

- serving Glosa over HTTPS (a Caddy reverse proxy or a tunnel);
- the lab-only Chrome flag `--unsafely-treat-insecure-origin-as-secure`;
- the Chrome kiosk flags an unattended mini PC needs.

The station page detects an insecure context and explains the fix on screen instead of failing silently.
