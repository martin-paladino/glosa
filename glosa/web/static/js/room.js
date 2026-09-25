/* Glosa: audience room view (/s/{slug}). Plain JS, no build, no dependencies.

   Captions arrive over SSE from /api/stream/{slug}/{lang} as CaptionMsg JSON:
     {"id", "type": "append", "seg", "text"}   text is added to the end of phrase `seg`
     {"id", "type": "set", "seg", "text"}      the whole text of the open phrase `seg` so far
                                               (it replaces what the phrase shows; "" removes it)
     {"type": "close", "seg"}                   phrase `seg` is finished
     {"type": "talk", "data": {"talk_id", "title", "speakers", "language"}}
     {"type": "status", "data": {"state": "green|yellow|red|idle", ...}}
   On connect the server replays recent history; EventSource reconnects on its
   own and resumes with Last-Event-ID.

   Rules from docs/design/README.md §5: text already on screen is never
   rewritten (only appended); the open phrase is dimmer and "settles" by colour
   only; about three closed phrases move from the live block to the history as
   one paragraph; scrolling up pauses auto-scroll and shows "Back to live".
   The one exception to "never rewritten": the open phrase of a "set" stream
   (the glossary engine's transcription rewrites it while the speaker talks).

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
  const SUMMARY_POLL_MS = 60000;   // "¿Qué me perdí?": how often the summary is refreshed in the background
  const PIP_LINES = 3;             // "¿the last 2-3 caption lines" shown in the pop-out window

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
  const pipButton = $('[data-action="pip"]');
  const summaryToggle = $("[data-summary-toggle]");
  const summaryPanel = $("[data-summary-panel]");
  const summaryScrim = $("[data-summary-scrim]");
  const summaryBullets = $("[data-summary-bullets]");
  const summaryAgo = $("[data-summary-ago]");

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

    /** `text` is the whole open phrase `seg` so far: it replaces what the
        phrase shows (a new seg starts one, as append does). An empty text
        removes the phrase: it was not speech after all. */
    set(seg, text, stamp) {
      const span = this.phrases.get(seg);
      if (!span) {
        this.append(seg, text, stamp);
        return;
      }
      const full = text.replace(/^\s+/, "");
      const visible = full.replace(/\s+$/, "");
      this.trailing.set(seg, full.slice(visible.length));
      if (visible) {
        span.replaceChildren(visible);
        return;
      }
      const before = span.previousSibling;   // the space that separates it from the phrase before
      if (before && before.nodeType === 3 && !before.textContent.trim()) before.remove();
      span.remove();
      this.phrases.delete(seg);
      this.trailing.delete(seg);
      if (this.open === span) this.open = null;
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
        case "set":
          if (typeof msg.text === "string" && msg.seg !== null && msg.seg !== undefined) {
            this.page.set(msg.seg, msg.text, stamp);
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
      renderPip();
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
    renderPip();
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
    const talkChanged = id !== state.talkId;
    if (!id) {
      state.talkId = null;
      state.roomState = "idle";
      title.hidden = true;
      meta.hidden = true;
      renderStatus();
      if (talkChanged) pollSummary();   // the room went idle: no summary for it any more
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
    if (talkChanged) pollSummary();   // a new talk starts with no summary of its own yet
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

  // ---- "¿Qué me perdí?" (Task 17): GET /api/summary/{slug}/{lang}, polled -----------------
  // in the background every SUMMARY_POLL_MS (and once right away, and again on a caption
  // language change) so the button knows whether to show at all; a click refreshes it once
  // more (freshest) and opens the panel. Esc and the close button/scrim both dismiss it.

  const summary = { data: null, open: false };

  function summaryOpenFor(lang) {
    return cfg.summaryBase ? cfg.summaryBase + encodeURIComponent(lang) : null;
  }

  async function pollSummary() {
    const url = summaryOpenFor(state.lang);
    if (!url) return;
    let data = null;
    try {
      const res = await fetch(url);
      if (res.ok) data = await res.json();
    } catch {
      data = null;
    }
    summary.data = data;
    summaryToggle.hidden = !data;
    summaryToggle.setAttribute("aria-disabled", data ? "false" : "true");
    if (!data && summary.open) closeSummaryPanel();
    else if (summary.open) renderSummaryPanel();
  }

  function renderSummaryPanel() {
    const data = summary.data;
    summaryBullets.replaceChildren(...(data ? data.bullets : []).map((text) => el("li", {}, text)));
    if (data) {
      const minutes = Math.max(0, Math.floor((Date.now() / 1000 - data.generated_at) / 60));
      summaryAgo.textContent = format(T.summary_ago, { n: minutes });
    } else {
      summaryAgo.textContent = "";
    }
  }

  function openSummaryPanel() {
    if (!summary.data) return;
    summary.open = true;
    summaryToggle.setAttribute("aria-expanded", "true");
    summaryPanel.classList.add("summary-panel--open");
    summaryScrim.classList.add("summary-scrim--open");
    renderSummaryPanel();
    pollSummary();   // freshest bullets right as the reader opens it
  }

  function closeSummaryPanel() {
    summary.open = false;
    summaryToggle.setAttribute("aria-expanded", "false");
    summaryPanel.classList.remove("summary-panel--open");
    summaryScrim.classList.remove("summary-scrim--open");
  }

  function toggleSummaryPanel() {
    if (summary.open) closeSummaryPanel();
    else openSummaryPanel();
  }

  summaryToggle.setAttribute("aria-disabled", "true");
  pollSummary();
  setInterval(pollSummary, SUMMARY_POLL_MS);

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
    pollSummary();   // a language change may show a different (or no) button/panel
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
    syncPipRoot();
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
    syncPipRoot();
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

  // ---- Picture-in-Picture captions (Task 20) -------------------------------------------
  // Chrome/Edge only (window.documentPictureInPicture.requestWindow): a small
  // floating window that shows the last few caption lines on top of other
  // windows. It gets its own document, so the page's stylesheets are cloned
  // into it (and the theme/text-size the reader picked, mirrored onto its
  // root) rather than moving anything out of the main page -- closing it
  // needs no restore, since the main page was never touched.
  let pipWindow = null;
  let pipMirror = null;

  function pipSupported() {
    return typeof window !== "undefined" && window !== null && "documentPictureInPicture" in window;
  }

  if (pipButton) pipButton.hidden = !pipSupported();

  function pipSourcePage() {
    const feed = feeds.find((f) => f.page.lang === state.lang);
    return (feed || feeds[0] || null)?.page || null;
  }

  function captionLines(page, n) {
    if (!page) return [];
    const paras = Array.from(page.history.children)
      .map((li) => li.textContent.trim())
      .filter(Boolean);
    const live = page.liveText.textContent.trim();
    const lines = live ? [...paras, live] : paras;
    return lines.slice(-n);
  }

  function renderPip() {
    if (!pipWindow || !pipMirror) return;
    const lines = captionLines(pipSourcePage(), PIP_LINES);
    pipMirror.replaceChildren(...lines.map((text) => el("p", { class: "pip-line" }, text)));
  }

  function copyStylesInto(doc) {
    document.querySelectorAll('link[rel="stylesheet"], style').forEach((node) => {
      doc.head.append(node.cloneNode(true));
    });
  }

  function syncPipRoot() {
    if (!pipWindow) return;
    const pipRoot = pipWindow.document.documentElement;
    if (root.dataset.theme) pipRoot.dataset.theme = root.dataset.theme;
    else delete pipRoot.dataset.theme;
    pipRoot.style.setProperty("--caption-scale", root.style.getPropertyValue("--caption-scale") || "1");
  }

  function closePip() {
    pipWindow = null;
    pipMirror = null;
    if (pipButton) pipButton.setAttribute("aria-pressed", "false");
  }

  async function openPip() {
    if (!pipSupported() || pipWindow) return;
    let win;
    try {
      win = await window.documentPictureInPicture.requestWindow({ width: 420, height: 220 });
    } catch {
      return;   // the browser or the reader refused the pop-out
    }
    pipWindow = win;
    copyStylesInto(win.document);
    win.document.body.className = "pip-body";
    pipMirror = win.document.createElement("div");
    pipMirror.className = "pip-captions";
    win.document.body.append(pipMirror);
    syncPipRoot();
    renderPip();
    if (pipButton) pipButton.setAttribute("aria-pressed", "true");
    win.addEventListener("pagehide", closePip, { once: true });
  }

  function togglePip() {
    if (!pipSupported()) return;
    if (pipWindow) pipWindow.close();   // triggers "pagehide" -> closePip()
    else openPip();
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "size") stepScale(Number(button.dataset.step));
    else if (action === "theme") cycleTheme();
    else if (action === "fullscreen") toggleFullscreen();
    else if (action === "pip") togglePip();
    else if (action === "summary") toggleSummaryPanel();
    else if (action === "summary-close") closeSummaryPanel();
  });

  // Desktop shortcuts: F full screen, + and − text size.
  document.addEventListener("keydown", (event) => {
    if (summary.open && event.key === "Escape") {
      closeSummaryPanel();
      return;
    }
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
