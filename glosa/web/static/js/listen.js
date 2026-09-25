/* Glosa: "Escuchar el audio" (Task 14b, Ruling 5). Admin-only, and only
   while a room plays a test file: GET /api/admin/listen/{room} says whether
   one is playing and how far in (offset_s), and once the MP3 is ready,
   GET /api/admin/listen/{room}/audio streams it (glosa/web/admin_listen.py).
   Pressing the button seeks an <audio> to that offset and plays: the audio
   then runs a few seconds ahead of the captions on screen, same as in the
   room itself (real-time playback of the same file the pipeline reads).

   Two call sites share this module, each mounting a `[data-listen]`
   container of its own:
     - room.html, the public room page: rendered (markup and this script
       both) only when the viewer had a valid admin session AND the room
       was already in test mode at that page load (glosa/web/pages.py's
       `listen` flag -- without a session, neither exists at all). This
       module's own polling is what makes the button disappear once the
       test ends: a page load is the only signal the server otherwise gives
       this view.
     - admin.js's room drawer, next to "Probar con audio": admin.js creates
       and destroys a player each time the drawer opens or switches rooms
       (glosa/web/static/js/admin.js).

   Does not touch room.js's own scroll/follow/theme logic, or room.js's SSE
   caption stream -- this is a second, unrelated poll against a different
   endpoint.
*/
(() => {
  "use strict";

  const POLL_MS = 4000; // how often we check availability / offset
  const RESYNC_MS = 20000; // re-seek check cadence (spec: "cada 20 s")
  const DRIFT_S = 1; // re-seek once playback drifts this far from offset_s

  class ListenPlayer {
    constructor(container, roomId) {
      this.container = container;
      this.roomId = roomId;
      this.button = container.querySelector("[data-listen-button]");
      this.audio = container.querySelector("[data-listen-audio]");
      this.status = container.querySelector("[data-listen-status]");
      this.preparingText = container.dataset.listenPreparing || "";
      this.active = false; // the admin pressed play; keep seeking/playing once ready
      this.resyncAt = 0;
      this.destroyed = false;
      if (this.button) this.button.addEventListener("click", () => this.start());
      this.poll();
      this.timer = setInterval(() => this.poll(), POLL_MS);
    }

    destroy() {
      this.destroyed = true;
      clearInterval(this.timer);
      if (this.audio) {
        this.audio.pause();
        this.audio.removeAttribute("src");
      }
    }

    start() {
      this.active = true;
      this.poll();
    }

    setStatus(text) {
      if (!this.status) return;
      this.status.hidden = !text;
      this.status.textContent = text || "";
    }

    async poll() {
      let info;
      try {
        const response = await fetch(`/api/admin/listen/${encodeURIComponent(this.roomId)}`, {
          credentials: "same-origin",
        });
        if (this.destroyed) return;
        if (!response.ok) {
          this.offer(false);
          return;
        }
        info = await response.json();
      } catch {
        return; // a transient network hiccup: the next tick tries again
      }
      if (this.destroyed) return;
      const offering = info.offset_s !== null && info.offset_s !== undefined;
      this.offer(offering);
      if (!offering || !this.active) return;
      if (!info.available) {
        this.setStatus(this.preparingText);
        return;
      }
      this.setStatus(null);
      if (!this.audio) return;
      const now = Date.now();
      if (this.audio.getAttribute("src") !== info.url) {
        this.audio.hidden = false;
        this.audio.src = info.url;
        this.audio.currentTime = info.offset_s;
        this.audio.play().catch(() => {});
        this.resyncAt = now + RESYNC_MS;
      } else if (now >= this.resyncAt) {
        this.resyncAt = now + RESYNC_MS;
        if (Math.abs(this.audio.currentTime - info.offset_s) > DRIFT_S) this.audio.currentTime = info.offset_s;
      }
    }

    /** Whether the room is offering a test file to listen to at all -- the
        whole widget appears and disappears with this, regardless of
        whether the admin has actually pressed play yet. */
    offer(visible) {
      this.container.hidden = !visible;
      if (visible) return;
      this.active = false;
      this.setStatus(null);
      if (this.audio) {
        this.audio.pause();
        this.audio.hidden = true;
        this.audio.removeAttribute("src");
      }
    }
  }

  function boot() {
    // Auto-boot for room.html's static container. admin.js's own copy
    // lives inside a <template> (glosa/web/templates/admin.html) until a
    // drawer clones it, so it never matches this at load time -- admin.js
    // constructs a ListenPlayer itself, once per drawer open.
    const container = document.querySelector("[data-listen]");
    if (!container) return;
    const roomId = container.dataset.listenRoom;
    if (roomId) new ListenPlayer(container, roomId);
  }

  window.GlosaListen = { ListenPlayer };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
