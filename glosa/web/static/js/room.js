/* Glosa: audience room view (/s/{slug}). Plain JS, no build, no dependencies.

   Captions arrive over SSE from /api/stream/{slug}/{lang} as CaptionMsg JSON:
     {"id", "type": "append", "seg", "text"}   text is added to the end of phrase `seg`
     {"type": "close", "seg"}                   phrase `seg` is finished
     {"type": "talk", "data": {"talk_id", "title", "speakers", "language"}}
     {"type": "status", "data": {"state": "green|yellow|red|idle", ...}}
   On connect the server replays recent history; EventSource reconnects on its
   own and resumes with Last-Event-ID.

   Rules from docs/design/README.md §5: text already on screen is never
   rewritten (only appended); the open phrase is dimmer and "settles" by colour
   only; about three closed phrases move from the live block to the history as
   one paragraph; scrolling up pauses auto-scroll and shows "Back to live".

   Bilingual mode shows two "facing pages" (original on the left, translation
   on the right), each fed by its own stream: segments of different languages
   are not aligned, so each page flows on its own. Below 40rem the original
   shrinks to a small live line above the translation (CSS only).
*/
(() => {
  "use strict";

  const configEl = document.getElementById("glosa-room");
  if (!configEl) return;
  const cfg = JSON.parse(configEl.textContent);
  const T = cfg.i18n;

  const SCALES = [0.8, 0.9, 1, 1.15, 1.3, 1.5, 1.75];
  const THEMES = ["system", "light", "dark", "contrast"];
  const FOLLOW_SLACK_PX = 48;      // scrolling up more than this pauses auto-scroll
  const PARAGRAPH_PHRASES = 3;     // closed phrases per history paragraph
  const PARAGRAPH_CHARS = 160;     // ...or fewer if they are long,
  const PARAGRAPH_MAX_CHARS = 360; // ...cut at a sentence end unless it gets this long
  const SENTENCE_END = /[.?!…:;]["'”’»)\]]*\s*$/;
  const MAX_PARAGRAPHS = 400;      // per page; older ones are dropped while following
  const BACKLOG_GAP_MS = 250;      // the replayed history arrives as one burst
  const BACKLOG_MAX_MS = 2000;
  const RETRY_MS = 5000;           // when the server refuses the stream outright

  // Visual class names, all in one place. room.js only ever writes them (for the
  // CSS); it finds elements through data-* hooks and keeps its own state, so a
  // re-skin can restyle or rename these without touching the logic below.
  const CLASS = {
    page: "page",
    history: "history",
    line: "line",
    lineHead: "line line--head",
    lineLive: "line line--live",
    time: "line__time",
    text: "line__text",
    label: "line__label",
    phrase: "phrase",
    phraseOpen: "phrase--open",
    facing: "transcript--facing",
    stageNote: "stage-note",
    status: (kind) => `status status--${kind}`,
  };

  const root = document.documentElement;
  const $ = (selector) => document.querySelector(selector);
  const stage = $("[data-stage]");
  const transcript = $("[data-transcript]");
  const select = $("[data-lang-select]");
  const statusEl = $("[data-status]");
  const toLive = $('[data-action="live"]');
  const themeButton = $('[data-action="theme"]');
  const fullscreenButton = $('[data-action="fullscreen"]');

  // ---- small helpers ---------------------------------------------------------

  const store = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try {
        if (value === null) localStorage.removeItem(key);
        else localStorage.setItem(key, String(value));
      } catch { /* private mode: preferences just don't persist */ }
    },
  };

  const format = (template, vars) =>
    template.replace(/\{(\w+)\}/g, (_, key) => (key in vars ? vars[key] : ""));

  const nameOf = (code) => cfg.langNames[code] || String(code).toUpperCase();
  const endonymOf = (code) => cfg.endonyms[code] || String(code).toUpperCase();

  function joinNames(names) {
    if (names.length <= 1) return names.join("");
    return `${names.slice(0, -1).join(", ")} ${T.and} ${names[names.length - 1]}`;
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    node.append(...children);
    return node;
  }

  function clockStamp() {
    const d = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  // ---- state -----------------------------------------------------------------

  function initialLang() {
    if (cfg.forcedLang) return cfg.forcedLang;
    const saved = store.get("glosa.lang");
    return saved && cfg.langs.includes(saved) ? saved : cfg.defaultLang;
  }

  const state = {
    lang: initialLang(),
    bilingual: store.get("glosa.bilingual") === "1" && cfg.langs.length > 1,
    source: cfg.source,
    talkId: cfg.talkId,
    roomState: cfg.talkId ? "live" : "idle",   // live | down | idle
    following: true,
  };

  /** Languages on screen: [lang], or [original, translation] in bilingual mode. */
  function columns() {
    if (!state.bilingual || cfg.langs.length < 2) return [state.lang];
    const original = cfg.langs.includes(state.source) ? state.source : cfg.langs[0];
    const translation = state.lang !== original
      ? state.lang
      : cfg.langs.find((code) => code !== original);
    return [original, translation];
  }

  // ---- a page of captions: history + live block, for one language --------------

  class Page {
    constructor(lang, role) {
      this.lang = lang;
      this.el = el("section", { class: CLASS.page, "data-page": role || "single", lang });
      if (role) {
        const label = el("span", { class: CLASS.label }, el("b", {}, endonymOf(lang)), ` (${T[role]})`);
        this.el.append(el("div", { class: CLASS.lineHead, "aria-hidden": "true" }, el("span"), label));
      }
      this.history = el("ol", { class: CLASS.history, role: "log", "aria-label": T.said_so_far });
      this.liveTime = el("time", { class: CLASS.time });
      this.liveText = el("p", { class: CLASS.text });
      this.live = el("div", { class: CLASS.lineLive, "data-live": "", "aria-live": "off" },
        this.liveTime, this.liveText);
      this.el.append(this.history, this.live);
      this.reset();
    }

    reset() {
      this.phrases = new Map();     // seg -> <span>, only phrases still in the live block
      this.trailing = new Map();    // seg -> whitespace held back from the end of the phrase
      this.open = null;
      this.closedCount = 0;
      this.closedChars = 0;
      this.sentenceEnded = false;   // the last closed phrase ends a sentence
      this.liveStamp = null;
      this.lastStamp = null;        // time shown on the latest history paragraph
    }

    // Segments can be cut mid-sentence (max length), so paragraphs end at a
    // sentence end when possible: a history paragraph never stops at "de".
    paragraphDone() {
      if (!this.closedCount) return false;
      if (this.closedChars >= PARAGRAPH_MAX_CHARS) return true;
      const enough = this.closedCount >= PARAGRAPH_PHRASES || this.closedChars >= PARAGRAPH_CHARS;
      return enough && this.sentenceEnded;
    }

    hasContent() {
      return this.history.childElementCount > 0 || this.liveText.childNodes.length > 0;
    }

    clear() {
      this.history.replaceChildren();
      this.liveText.replaceChildren();
      this.liveTime.textContent = "";
      this.liveTime.removeAttribute("datetime");
      this.reset();
      autoTop = 0;   // the content just shrank: until the next jump, no clamp is the reader's
    }

    append(seg, text, stamp) {
      let span = this.phrases.get(seg);
      if (!span) {
        if (this.open) this.settle(this.open);
        if (this.paragraphDone()) this.flush();
        text = text.replace(/^\s+/, "");
        if (!text) return;
        span = el("span", { class: `${CLASS.phrase} ${CLASS.phraseOpen}`, "data-seg": seg, "data-open": "" });
        if (this.liveText.childNodes.length) this.liveText.append(" ");
        else this.stampLive(stamp);
        this.liveText.append(span);
        this.phrases.set(seg, span);
        this.open = span;
      }
      // Trailing spaces wait for the next word, so the cursor never wraps alone
      // onto a new line. Each chunk is a new text node: nothing is rewritten.
      const full = (this.trailing.get(seg) || "") + text;
      const visible = full.replace(/\s+$/, "");
      this.trailing.set(seg, full.slice(visible.length));
      if (visible) span.append(visible);
    }

    close(seg) {
      const span = this.phrases.get(seg);
      if (span && span === this.open) this.settle(span);   // only the last phrase is ever open
    }

    settle(span) {
      span.classList.remove(CLASS.phraseOpen);
      span.removeAttribute("data-open");
      if (this.open === span) this.open = null;
      this.closedCount += 1;
      this.closedChars += span.textContent.length;
      this.sentenceEnded = SENTENCE_END.test(span.textContent);
    }

    stampLive(stamp) {
      this.liveStamp = stamp;
      this.liveTime.textContent = stamp || "";
      if (stamp) this.liveTime.setAttribute("datetime", stamp);
      else this.liveTime.removeAttribute("datetime");
    }

    /** Move the closed phrases of the live block into a new history paragraph. */
    flush() {
      if (!this.liveText.childNodes.length) return;
      const item = el("li", { class: CLASS.line });
      if (this.liveStamp && this.liveStamp !== this.lastStamp) {
        item.append(el("time", { class: CLASS.time, datetime: this.liveStamp }, this.liveStamp));
        this.lastStamp = this.liveStamp;
      }
      const text = el("p", { class: CLASS.text });
      text.append(...this.liveText.childNodes);   // moved, not re-created
      item.append(text);
      this.history.append(item);
      this.phrases.clear();
      this.trailing.clear();
      this.open = null;
      this.closedCount = 0;
      this.closedChars = 0;
      this.stampLive(null);
      if (state.following) {
        while (this.history.childElementCount > MAX_PARAGRAPHS) this.history.firstElementChild.remove();
      }
    }
  }

  // ---- one SSE stream feeding one page ------------------------------------------

  class Feed {
    constructor(page) {
      this.page = page;
      this.talkId = undefined;     // last talk announced on this stream
      this.connected = true;       // optimistic: no "Reconnecting" flash on load
      this.backlog = true;         // the first burst is replayed history: no clock times
      this.firstOpen = true;
      this.lastAt = 0;
      this.retry = null;
      this.open();
    }

    open() {
      const source = new EventSource(cfg.streamBase + encodeURIComponent(this.page.lang));
      this.source = source;
      source.onopen = () => {
        if (this.firstOpen) {
          this.firstOpen = false;
          this.openedAt = performance.now();
        }
        this.setConnected(true);
      };
      source.onmessage = (event) => this.receive(event);
      source.onerror = () => {
        this.setConnected(false);
        // EventSource retries by itself (with Last-Event-ID) unless the server
        // refused the stream. A new EventSource starts without it and gets the
        // whole history again, so the page is rebuilt from that replay.
        if (source.readyState === EventSource.CLOSED && !this.retry) {
          this.retry = setTimeout(() => {
            this.retry = null;
            this.page.clear();
            this.talkId = undefined;
            this.backlog = true;
            this.firstOpen = true;
            this.lastAt = 0;
            this.open();
          }, RETRY_MS);
        }
      };
    }

    close() {
      clearTimeout(this.retry);
      this.retry = null;
      if (this.source) this.source.close();
    }

    setConnected(value) {
      if (this.connected === value) return;
      this.connected = value;
      renderStatus();
    }

    stamp() {
      const now = performance.now();
      if (this.backlog) {
        const sinceOpen = now - (this.openedAt ?? now);
        const gap = this.lastAt ? now - this.lastAt : 0;
        if (sinceOpen > BACKLOG_MAX_MS || gap > BACKLOG_GAP_MS) this.backlog = false;
      }
      this.lastAt = now;
      return this.backlog ? null : clockStamp();
    }

    receive(event) {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      const stamp = this.stamp();
      switch (msg.type) {
        case "append":
          if (typeof msg.text === "string" && msg.seg !== null && msg.seg !== undefined) {
            this.page.append(msg.seg, msg.text, stamp);
            hideStageNote();
          }
          break;
        case "close":
          this.page.close(msg.seg);
          break;
        case "talk":
          this.onTalk(msg.data || {});
          break;
        case "status":
          onStatus(msg.data || {});
          break;
        default:
          return;
      }
      scheduleScroll();
    }

    onTalk(data) {
      const id = data.talk_id || null;
      // Captions before a talk's announcement belong to an earlier talk.
      if (id && id !== this.talkId) this.page.clear();
      if (id) this.talkId = id;
      onTalk(data, !this.backlog);
    }
  }

  // ---- room: header, status, stage -------------------------------------------------

  let feeds = [];

  function connect() {
    feeds.forEach((feed) => feed.close());
    transcript.querySelectorAll("[data-page]").forEach((page) => page.remove());

    const langs = columns();
    const facing = langs.length > 1;
    transcript.classList.toggle(CLASS.facing, facing);
    transcript.dataset.layout = facing ? "facing" : "single";
    if (facing) transcript.removeAttribute("lang");
    else transcript.setAttribute("lang", langs[0]);

    const pages = langs.map((lang, i) =>
      new Page(lang, facing ? (i === 0 ? "original" : "translation") : null));
    transcript.append(...pages.map((page) => page.el));
    feeds = pages.map((page) => new Feed(page));

    select.value = facing ? "bilingual" : state.lang;
    stage.setAttribute("aria-label", facing
      ? format(T.captions_in_two, { a: nameOf(langs[0]), b: nameOf(langs[1]) })
      : format(T.captions_in, { lang: nameOf(langs[0]) }));
    renderDirection();
    setFollowing(true);
    renderStatus();
  }

  function renderStatus() {
    let kind = state.roomState;
    if (feeds.some((feed) => !feed.connected)) kind = "down";
    const words = { live: T.live, down: T.reconnecting, idle: T.between_talks };
    statusEl.className = CLASS.status(kind);
    statusEl.dataset.state = kind;
    if (statusEl.textContent !== words[kind]) statusEl.textContent = words[kind];
  }

  function onStatus(data) {
    const map = { green: "live", yellow: "live", red: "down", idle: "idle" };
    if (map[data.state]) {
      state.roomState = map[data.state];
      renderStatus();
    }
  }

  // `live` is false while a stream replays its history: old announcements then
  // only update the header, they never trigger a reconnect (that would replay
  // them again, forever).
  function onTalk(data, live) {
    const title = $("[data-talk-title]");
    const meta = $("[data-talk-meta]");
    const id = data.talk_id || null;
    if (!id) {
      state.talkId = null;
      state.roomState = "idle";
      title.hidden = true;
      meta.hidden = true;
      renderStatus();
      return;
    }
    const sourceChanged = (data.language || null) !== state.source;
    state.talkId = id;
    state.source = data.language || null;
    state.roomState = "live";
    title.textContent = data.title || "";
    if (data.language) title.setAttribute("lang", data.language);
    $("[data-talk-speakers]").textContent = joinNames(Array.isArray(data.speakers) ? data.speakers : []);
    title.hidden = false;
    meta.hidden = false;
    if (!feeds.some((feed) => feed.page.hasContent())) showWaitingNote();
    renderStatus();
    if (sourceChanged) {
      labelOptions();
      if (state.bilingual && live) connect();   // the original's column changed language
      else renderDirection();
    }
  }

  function renderDirection() {
    const badge = $("[data-direction]");
    const langs = columns();
    const target = langs[langs.length - 1];
    const source = state.source || target;
    const codes = source === target ? [target] : [source, target];
    const parts = [];
    codes.forEach((code, i) => {
      if (i > 0) parts.push(arrowIcon());
      parts.push(el("abbr", { title: nameOf(code) }, code.toUpperCase()));
    });
    badge.replaceChildren(...parts);
    badge.setAttribute("aria-label", codes.length === 1
      ? format(T.original_language, { lang: nameOf(codes[0]) })
      : format(T.direction, { src: nameOf(codes[0]), dst: nameOf(codes[1]) }));
  }

  function arrowIcon() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", "M2 8h11M9 4l4 4-4 4");
    for (const [key, value] of Object.entries({
      fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round",
    })) path.setAttribute(key, value);
    svg.append(path);
    return svg;
  }

  function labelOptions() {
    for (const option of select.options) {
      if (option.value === "bilingual") continue;
      option.textContent = endonymOf(option.value) + (option.value === state.source ? ` (${T.original})` : "");
    }
  }

  // The note on an empty stage ("captions show up here..." or the next talk).
  function hideStageNote() {
    const note = transcript.querySelector("[data-stage-note]");
    if (note) note.remove();
  }

  function showWaitingNote() {
    const note = transcript.querySelector("[data-stage-note]");
    if (note && note.dataset.stageNote === "waiting") return;
    const waiting = el("p", { class: CLASS.stageNote, "data-stage-note": "waiting" }, T.waiting_captions);
    if (note) note.replaceWith(waiting);
    else transcript.prepend(waiting);
  }

  // ---- following the live text --------------------------------------------------------

  let scrollQueued = false;
  let autoTop = 0;   // where the code last put the stage, after clamping

  function toBottom() {
    stage.scrollTop = stage.scrollHeight;
    autoTop = stage.scrollTop;
  }

  function scheduleScroll() {
    if (scrollQueued || !state.following) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      if (state.following) toBottom();
    });
  }

  // The scroll event for our own jump arrives a frame later, and a replay burst
  // may have grown the content by then. Only a move *up* from where the code
  // put the stage (or from the new bottom, if the content shrank or the stage
  // grew) is the reader's.
  stage.addEventListener("scroll", () => {
    const bottom = stage.scrollHeight - stage.clientHeight;
    if (state.following) {
      if (stage.scrollTop < Math.min(autoTop, bottom) - FOLLOW_SLACK_PX) setFollowing(false);
    } else if (bottom - stage.scrollTop <= FOLLOW_SLACK_PX) {
      setFollowing(true);
    }
  }, { passive: true });

  toLive.addEventListener("click", () => {
    setFollowing(true);
    toBottom();
  });

  function setFollowing(value) {
    state.following = value;
    toLive.hidden = value;
    if (value) scheduleScroll();
  }

  // Rotating the phone or resizing the text must not lose the live line.
  new ResizeObserver(() => scheduleScroll()).observe(stage);

  // ---- reading controls ----------------------------------------------------------------

  select.addEventListener("change", () => {
    if (select.value === "bilingual") {
      state.bilingual = true;
      store.set("glosa.bilingual", "1");
    } else {
      state.lang = select.value;
      state.bilingual = false;
      store.set("glosa.lang", state.lang);
      store.set("glosa.bilingual", null);
      // A ?lang= in the address wins over the saved choice: keep it in step.
      const url = new URL(location.href);
      if (url.searchParams.has("lang")) {
        url.searchParams.set("lang", state.lang);
        history.replaceState(null, "", url);
      }
    }
    connect();
  });

  function currentScale() {
    const value = parseFloat(root.style.getPropertyValue("--caption-scale"));
    return SCALES.includes(value) ? value : 1;
  }

  function stepScale(step) {
    const i = Math.max(0, Math.min(SCALES.length - 1, SCALES.indexOf(currentScale()) + step));
    root.style.setProperty("--caption-scale", String(SCALES[i]));
    store.set("glosa.scale", SCALES[i] === 1 ? null : SCALES[i]);
    scheduleScroll();
  }

  function currentTheme() {
    return THEMES.includes(root.dataset.theme) ? root.dataset.theme : "system";
  }

  function labelTheme() {
    const label = format(T.theme, { name: T[`theme_${currentTheme()}`] });
    themeButton.setAttribute("aria-label", label);
    themeButton.title = label;
  }

  function cycleTheme() {
    const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    store.set("glosa.theme", next === "system" ? null : next);
    labelTheme();
  }

  const canFullscreen = Boolean(document.fullscreenEnabled);
  if (!canFullscreen) fullscreenButton.hidden = true;

  function toggleFullscreen() {
    if (!canFullscreen) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else root.requestFullscreen().catch(() => {});
  }

  document.addEventListener("fullscreenchange", () => {
    fullscreenButton.setAttribute("aria-pressed", String(Boolean(document.fullscreenElement)));
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "size") stepScale(Number(button.dataset.step));
    else if (action === "theme") cycleTheme();
    else if (action === "fullscreen") toggleFullscreen();
  });

  // Desktop shortcuts: F full screen, + and − text size.
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target.closest && target.closest("input, select, textarea, [contenteditable]")) return;
    if (event.key === "f" || event.key === "F") toggleFullscreen();
    else if (event.key === "+" || event.key === "=") stepScale(1);
    else if (event.key === "-" || event.key === "_" || event.key === "−") stepScale(-1);
    else return;
    event.preventDefault();
  });

  // Keep the phone screen awake while reading, where the browser allows it.
  async function keepAwake() {
    try {
      if ("wakeLock" in navigator && document.visibilityState === "visible") {
        await navigator.wakeLock.request("screen");
      }
    } catch { /* not allowed here: the screen may dim as usual */ }
  }
  document.addEventListener("visibilitychange", keepAwake);
  keepAwake();

  // ---- start -------------------------------------------------------------------------------

  labelOptions();
  labelTheme();
  connect();
})();
