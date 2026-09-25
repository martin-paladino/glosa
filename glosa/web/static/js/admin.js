/* Glosa: the production panel (/admin), "Sala de control" v2. Plain JS, no build.

   One EventSource, GET /api/admin/stream (glosa/web/admin_stream.py):
     state   every second: every room (raw RoomStatus, mode, talk, next talk,
             issue and its texts), the budget, the Atención rows, the next
             automatic change. The server localizes the room texts.
     log     one event of the log (id = Last-Event-ID).
     cc      the last lines of a room's original-language captions.
     notice  an admin event (talk edited, agenda imported...): reload the agenda.
     bye     the session ended: ask to log in again.
   The page is rendered from the same snapshot, so it is complete before the
   stream connects. Controls call /api/admin/* with the X-Glosa-Admin header
   (the CSRF check); a 401 anywhere means the session expired.

   Management by exception (docs/design/NOTES.md): a healthy room shows its
   name, light, time and captions; a problem adds one footer line with the
   suggested action; everything else lives in the side drawer, a modal
   dialog (click a monitor or press 1–9; Esc closes; the rest of the page is
   inert while it is open), which also edits the agenda (click a
   talk) and imports it. No native dialogs: confirmations are inline.

   Elements are found by data-* hooks; the visual classes are glosa.css's.
   The login page (admin_login.html) uses only the login part and the clock.
*/
(() => {
  "use strict";

  const configEl = document.getElementById("glosa-admin");
  if (!configEl) return;
  const cfg = JSON.parse(configEl.textContent);
  const T = cfg.i18n;
  const CSRF_HEADER = "X-Glosa-Admin";
  const LOCALE = cfg.ui === "es" ? "es-AR" : "en-US";
  const SPAN_MIN = 270;            // the agenda's window, as in the mockup (4 h 30 min)
  const LOOK_BACK_MIN = 105;       // ...with "now" about 40 % in
  const STALE_MS = 5000;           // no state frame for this long: the panel is out of date
  const RETRY_MS = 5000;           // when the stream is refused outright
  const MAX_LOG = 500;
  const TOAST_MS = 7000;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function say(key, values = {}) {
    return (T[key] ?? key).replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));
  }

  function plural(key, n) {
    const [one, many] = (T[key] ?? key).split("|");
    return (n === 1 || many === undefined ? one : many).replace("{n}", String(n));
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of children) if (child !== null && child !== undefined && child !== false) node.append(child);
    return node;
  }

  // ---- time: the server's clock, in the event's timezone ------------------------------

  let skew = 0;                    // server clock minus this browser's, from each state frame
  const now = () => Date.now() + skew;
  const pad = (n) => String(Math.floor(n)).padStart(2, "0");

  const partsIn = (() => {
    const options = { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
                      second: "2-digit", hourCycle: "h23" };
    let format;
    try {
      format = new Intl.DateTimeFormat("en-GB", { ...options, timeZone: cfg.tz });
    } catch {
      format = new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" });
    }
    return (ms) => {
      const parts = {};
      for (const part of format.formatToParts(new Date(ms))) parts[part.type] = part.value;
      return parts;
    };
  })();

  const hms = (ms) => { const p = partsIn(ms); return `${p.hour}:${p.minute}:${p.second}`; };
  const minuteOfDay = (ms) => { const p = partsIn(ms); return +p.hour * 60 + +p.minute + +p.second / 60; };
  const elapsed = (seconds) => `${pad(seconds / 3600)}:${pad((seconds / 60) % 60)}:${pad(seconds % 60)}`;
  // The server sends times in the event's timezone ("2030-09-24T15:29:00-03:00").
  const isoHm = (iso) => (iso ? iso.slice(11, 16) : "");
  const isoMin = (iso) => +iso.slice(11, 13) * 60 + +iso.slice(14, 16);
  const isoDay = (iso) => (iso ? iso.slice(0, 10) : "");

  function ago(iso) {
    const s = Math.max(0, Math.round((now() - Date.parse(iso)) / 1000));
    return s < 60 ? say("ago_s", { s }) : say("ago_m", { m: Math.floor(s / 60), ss: pad(s % 60) });
  }

  function until(iso) {
    const s = Math.max(0, Math.round((Date.parse(iso) - now()) / 1000));
    if (s < 3600) return say("in_mmss", { mm: pad(s / 60), ss: pad(s % 60) });
    return say("in_hmm", { h: Math.floor(s / 3600), mm: pad((s % 3600) / 60) });
  }

  const numbers = (digits) => new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const N0 = numbers(0), N1 = numbers(1), N2 = numbers(2);
  const money = (usd) => `US$ ${N2.format(usd)}`;
  const decibels = (db) => `${db < -0.5 ? "−" : ""}${N0.format(Math.abs(db))}`;
  const limit = (v) => (v < 0 ? "−" : "") + (Number.isInteger(v) ? N0 : N1).format(Math.abs(v));

  // ---- the ticking parts: clocks, timecodes, countdowns, ages ------------------------------

  const onTick = [];

  function tick() {
    const t = now();
    for (const clock of $$("[data-admin-clock]")) clock.textContent = hms(t);
    for (const node of $$("[data-start]")) {
      const iso = node.dataset.start;
      node.textContent = iso ? (node.dataset.prefix || "") + elapsed(Math.max(0, (t - Date.parse(iso)) / 1000)) : "";
    }
    for (const node of $$("[data-until]")) node.textContent = node.dataset.until ? until(node.dataset.until) : "";
    for (const node of $$("[data-since]")) node.textContent = node.dataset.since ? ago(node.dataset.since) : "";
    for (const fn of onTick) fn(t);
    setTimeout(tick, 1000 - (Date.now() % 1000) + 5);
  }

  // ---- notices -------------------------------------------------------------------------

  const toastEl = $("[data-toast]");
  let toastTimer = 0;

  function toast(message, { sticky = false, link = null } = {}) {
    if (!toastEl) return;
    const text = $("[data-toast-text]", toastEl);
    text.replaceChildren(message);
    if (link) text.append(" ", el("a", { href: link.href, text: link.text }), ".");
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    if (!sticky) toastTimer = setTimeout(() => { toastEl.hidden = true; }, TOAST_MS);
  }

  function showNotice(node, message, link = null) {
    if (!node) return;
    node.replaceChildren(message);
    if (link) node.append(" ", el("a", { href: link.href, text: link.text }), ".");
    node.hidden = false;
  }

  function busy(button, on, label = null) {
    if (!button) return;
    if (on) {
      if (label) {
        button.dataset.label = button.textContent;
        button.textContent = label;
      }
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      return;
    }
    button.removeAttribute("aria-busy");
    button.disabled = false;
    if (button.dataset.label !== undefined) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  }

  // ---- login ---------------------------------------------------------------------------

  if (cfg.page === "login") {
    const form = $("[data-admin-login-form]");
    const error = $("[data-admin-login-error]");
    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.hidden = true;
      const button = form.querySelector('button[type="submit"]');
      busy(button, true);
      let response;
      try {
        response = await fetch(form.action, { method: "POST", body: new FormData(form), redirect: "manual" });
      } catch {
        busy(button, false);
        showNotice(error, T.server_unreachable);
        return;
      }
      // Our own 303 to /admin comes back opaque under redirect: "manual"; a
      // wrong password is a readable 401.
      if (response.type === "opaqueredirect" || response.ok) {
        window.location.assign("/admin" + window.location.search);
        return;
      }
      busy(button, false);
      showNotice(error, T.wrong_password);
      form.elements.namedItem("password")?.select();
    });
    tick();
    return;
  }

  // ---- the panel -------------------------------------------------------------------------

  const admin = $("[data-admin]");
  if (!admin) return;
  const drawerEl = $("[data-drawer]");
  const scrim = $("[data-scrim]");
  const monitors = new Map($$("[data-monitor]").map((node) => [node.dataset.monitor, node]));
  const rooms = new Map();         // id -> the room of the last state frame
  const tails = new Map();         // id -> its last cc frame
  const logs = [];                 // oldest first
  const logIds = new Set();
  let state = cfg.state;
  let agenda = [];
  let logFilter = "alerts";
  let drawer = null;               // {kind: "room"|"talk"|"import", id, opener}
  let expired = false;

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(method, path, body) {
    const init = { method, headers: { [CSRF_HEADER]: "1" }, credentials: "same-origin" };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(`/api/admin${path}`, init);
    } catch {
      throw new ApiError(T.server_unreachable, 0);
    }
    if (response.status === 401) {
      sessionExpired();
      throw new ApiError(T.session_expired, 401);
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      /* not JSON */
    }
    if (!response.ok) throw new ApiError(errorText(payload, response.status), response.status);
    return payload;
  }

  function errorText(payload, status) {
    const detail = payload && payload.detail;
    let text = "";
    if (typeof detail === "string") text = detail;
    else if (Array.isArray(detail)) {
      text = detail.map((d) => {
        const where = (d.loc || []).filter((part) => part !== "body").join(".");
        return where ? `${where}: ${d.msg}` : d.msg;
      }).join("; ");
    } else if (detail && typeof detail === "object") {
      text = detail.row !== undefined ? `${detail.row}: ${detail.reason}` : JSON.stringify(detail);
    }
    return text ? say("error_prefix", { detail: text }) : say("error_status", { status });
  }

  function sessionExpired() {
    if (expired) return;
    expired = true;
    if (source) source.close();
    closeDrawer();                                  // its focus trap would keep the toast out of reach
    admin.classList.add("admin--stale");
    toast(T.session_expired, { sticky: true, link: { href: `/admin/login?lang=${cfg.ui}`, text: T.log_in_again } });
    $("[data-toast-text] a", toastEl)?.focus();
  }

  // ---- state frames -------------------------------------------------------------------

  let lastFrame = Date.now();
  let agendaKey = "";

  function onState(snapshot) {
    lastFrame = Date.now();
    setStale(false);
    skew = Date.parse(snapshot.now) - Date.now();
    state = snapshot;
    for (const room of snapshot.rooms) {
      rooms.set(room.id, room);
      renderMonitor(room);
      const led = $(`[data-track-row="${CSS.escape(room.id)}"] [data-t-led]`);
      if (led) led.className = `led led--${room.state}`;
    }
    renderMasthead(snapshot);
    renderAttention(snapshot);
    renderNextChange(snapshot);
    const key = snapshot.rooms.map((r) => [r.id, r.mode, r.talk?.id, r.next?.id].join(",")).join("|")
      + `|${snapshot.next_change?.talk_id}`;
    if (key !== agendaKey) {
      agendaKey = key;
      renderTimeline();
    }
    if (drawer?.kind === "room") updateRoomDrawer();
  }

  function renderMasthead(snapshot) {
    const tally = $("[data-tally]");
    const tallyKey = JSON.stringify(snapshot.tally);
    if (tally && tally.dataset.key !== tallyKey) {
      tally.dataset.key = tallyKey;
      tally.replaceChildren(...snapshot.tally.map((item) => el("span", { class: "tally__item", "data-tally-state": item.state },
        el("i", { class: `led led--${item.state}` }), el("strong", { text: String(item.count) }), item.word)));
    }
    const budget = snapshot.budget;
    const box = $("[data-budget]");
    box.classList.toggle("budget--warn", budget.alert === "80%");
    box.classList.toggle("budget--over", budget.alert === "exhausted");
    box.style.setProperty("--spent", String(Math.min(budget.ratio, 1)));
    $("[data-budget-value]").textContent = budget.text.value;
    $("[data-budget-of]").textContent = budget.text.of;
    const bar = $("[data-budget-bar]");
    bar.setAttribute("aria-valuenow", budget.spent.toFixed(2));
    bar.setAttribute("aria-valuetext", budget.text.valuetext);
    bar.title = budget.text.warns_at;
    $("[data-panels]").textContent = snapshot.panels_text;
  }

  // ---- monitors ----------------------------------------------------------------------------

  function isToday(iso) {
    return Boolean(iso) && isoDay(iso) === isoDay(state.now);
  }

  function renderMonitor(room) {
    const node = monitors.get(room.id);
    if (!node) return;
    node.className = `monitor monitor--${room.state}`;
    $("[data-m-led]", node).className = `led led--${room.state}`;
    const word = $("[data-m-state]", node);
    word.textContent = room.text.state_word;
    word.hidden = room.state === "live" || !room.text.state_word;
    $("[data-m-tc]", node).dataset.start = room.talk && room.state !== "idle" ? room.talk.actual_start || "" : "";
    const cc = $("[data-m-cc]", node);
    cc.hidden = room.state === "idle";
    const frozen = room.state === "down";
    if (cc.classList.contains("monitor__cc--frozen") !== frozen || !node.dataset.rendered) {
      cc.classList.toggle("monitor__cc--frozen", frozen);
      node.dataset.rendered = "1";
      rooms.set(room.id, room);
      renderCc(room.id);
    }
    cc.lang = room.lang;
    renderFrozen(room);
    const wait = $("[data-m-wait]", node);
    wait.hidden = room.state !== "idle";
    if (room.state === "idle") {
      const next = room.next && isToday(room.next.start) ? room.next : null;
      $("[data-m-count]", node).dataset.until = next ? next.start : "";
      const title = $("[data-m-next-title]", node);
      title.textContent = next ? next.title : "";
      if (next) title.lang = next.language;
      const speakers = next && next.speakers.length ? `${next.speakers.join(", ")}. ` : "";
      $("[data-m-next-meta]", node).textContent = speakers + room.text.wait;
    }
    const footer = $("[data-m-issue]", node);
    footer.hidden = !room.issue;
    if (room.issue) {
      $("[data-m-issue-text]", node).replaceChildren(el("b", { text: room.text.title }), ` ${room.text.note}`);
      suggest($("button", footer), room, "btn btn--compact");
    }
    node.setAttribute("aria-label", room.name + (room.text.state_word ? `, ${room.text.state_word}` : ""));
  }

  // The suggested action's key: coloured like the state that asks for it.
  function suggest(button, room, base) {
    if (!button || button.getAttribute("aria-busy") === "true") return;
    const issue = room.issue;
    button.hidden = !issue || !issue.action;
    if (button.hidden) return;
    button.dataset.action = issue.action;
    button.dataset.roomId = room.id;
    button.className = `${base} btn--${issue.severity}`;
    button.textContent = room.text.action_label;
  }

  function renderFrozen(room) {
    const node = monitors.get(room.id);
    if (!node) return;
    const label = $("[data-m-frozen]", node);
    const tail = tails.get(room.id);
    const show = room.state === "down" && tail && tail.ts;
    label.hidden = !show;
    if (show) label.textContent = say("last_caption", { time: hms(tail.ts * 1000) });
  }

  const FROZEN_CHARS = 140;         // a room that is down shows its last subtitle, not four lines

  function lastWords(text, max) {
    if (text.length <= max) return text;
    const cut = text.slice(-max);
    const space = cut.indexOf(" ");
    return `…${space >= 0 ? cut.slice(space + 1) : cut}`;
  }

  function renderCc(roomId) {
    const node = monitors.get(roomId);
    const tail = tails.get(roomId);
    if (!node || !tail) return;
    const room = rooms.get(roomId);
    if (room?.state === "down") {
      $("[data-m-closed]", node).textContent = lastWords(`${tail.closed} ${tail.open}`.trim(), FROZEN_CHARS);
      $("[data-m-open]", node).textContent = "";
    } else {
      $("[data-m-closed]", node).textContent = tail.closed;
      $("[data-m-open]", node).textContent = tail.open;
    }
    if (room) renderFrozen(room);
  }

  function onCc(frame) {
    tails.set(frame.room_id, frame);
    renderCc(frame.room_id);
    if (drawer?.kind === "room" && drawer.id === frame.room_id) renderDrawerCc();
  }

  // ---- Atención ------------------------------------------------------------------------------

  let attnOrder = null;

  function renderAttention(snapshot) {
    const box = $("[data-attn]");
    box.classList.toggle("attn--calm", snapshot.attention.length === 0);
    $("[data-attn-count]").textContent = snapshot.pending;
    $("[data-attn-ok]").textContent = snapshot.all_clear;
    const list = $("[data-attn-list]");
    const order = snapshot.attention.map((row) => `${row.key}:${row.severity}:${row.action}`).join("|");
    if (order !== attnOrder) {
      attnOrder = order;
      list.replaceChildren(...snapshot.attention.map(attentionRow));
      return;
    }
    // Same rows: update them in place, so a key the operator is on keeps its focus.
    for (const row of snapshot.attention) {
      const item = list.querySelector(`[data-attn-row="${CSS.escape(row.key)}"]`);
      if (!item) continue;
      $(".attn__what", item).replaceChildren(...whatOf(row));
      $("[data-since]", item).dataset.since = row.since || "";
    }
  }

  function whatOf(row) {
    return row.em ? [row.what, " ", el("em", { text: row.em })] : [row.what];
  }

  function attentionRow(row) {
    const action = row.action
      ? el("button", { class: `btn btn--compact btn--${row.severity}`, type: "button", "data-action": row.action,
                       "data-room-id": row.room_id, text: row.action_label })
      : el("span");
    return el("li", { class: `attn__row attn__row--${row.severity}`, "data-attn-row": row.key },
      el("i", { class: `led led--${row.severity}` }), el("b", { class: "attn__room", text: row.room }),
      el("span", { class: "attn__what" }, ...whatOf(row)),
      el("time", { class: "attn__age tc", "data-since": row.since || "" }), action);
  }

  async function runAction(button) {
    const { action, roomId } = button.dataset;
    if (action === "open") {
      openRoom(roomId, button);
      return;
    }
    const path = action === "restart" ? "restart" : "reconnect";
    busy(button, true, T.reconnecting);
    try {
      await api("POST", `/rooms/${encodeURIComponent(roomId)}/${path}`);
    } catch (error) {
      if (error.status !== 401) toast(error.message);
    } finally {
      busy(button, false);
    }
  }

  // ---- the log ------------------------------------------------------------------------------

  let logQueued = false;

  function onLog(event) {
    if (logIds.has(event.id)) return;
    logIds.add(event.id);
    logs.push(event);
    lastLogId = Math.max(lastLogId ?? 0, event.id);
    if (logs.length > MAX_LOG) for (const old of logs.splice(0, logs.length - MAX_LOG)) logIds.delete(old.id);
    if (logQueued) return;
    logQueued = true;
    requestAnimationFrame(() => {
      logQueued = false;
      renderLog();
      renderRoomHistory();
    });
  }

  // A flapping source logs a restart every few seconds: each run of them in a
  // room is one row with a count. `events` newest first.
  function coalesce(events) {
    const rows = [];
    const newest = new Map();          // room -> its newest row so far
    for (const event of events) {
      const room = event.room_id ?? "";
      const row = newest.get(room);
      if (event.type === "source_restart" && row?.event.type === "source_restart") {
        row.count += 1;
        continue;
      }
      const created = { event, count: 1 };
      rows.push(created);
      newest.set(room, created);
    }
    return rows;
  }

  function logItem({ event, count }, { compact = false } = {}) {
    const led = event.level === "error" ? "down" : event.level === "warning" ? "degraded" : "info";
    const attrs = { class: event.alert ? "log__item" : "log__item log__item--info" };
    if (!compact && event.room_id && monitors.has(event.room_id)) {
      attrs.tabindex = "0";
      attrs["data-log-room"] = event.room_id;
    }
    return el("li", attrs,
      el("i", { class: `led led--${led}` }),
      el("time", { class: "log__time tc", datetime: event.ts, text: hms(Date.parse(event.ts)) }),
      el("p", { class: "log__text" }, !compact && event.room ? el("b", { text: event.room }) : null, event.text,
        count > 1 ? el("em", { text: ` ${say("repeated", { n: count })}` }) : null));
  }

  function renderLog() {
    const alerts = logs.filter((event) => event.alert);
    const alertRows = coalesce(alerts.slice().reverse());
    const allRows = coalesce(logs.slice().reverse());
    const shown = (logFilter === "all" ? allRows : alertRows).slice(0, 150);
    const list = $("[data-log]");
    list.classList.toggle("log--all", logFilter === "all");
    list.replaceChildren(...shown.map((row) => logItem(row)));
    for (const button of $$("[data-log-filter]")) {
      const all = button.dataset.logFilter === "all";
      button.textContent = say(all ? "all_n" : "alerts_n", { n: all ? allRows.length : alertRows.length });
      button.setAttribute("aria-pressed", String(button.dataset.logFilter === logFilter));
    }
    const info = logs.length - alerts.length;
    const more = $("[data-log-more]");
    more.hidden = logFilter === "all" || info === 0;
    more.textContent = plural("show_info", info);
    $("[data-log-empty]").hidden = shown.length > 0;
  }

  // ---- the agenda ----------------------------------------------------------------------------

  let windowStart = null;
  let agendaTimer = 0;
  let renderedMinute = -1;

  async function loadAgenda() {
    try {
      agenda = await api("GET", "/talks");
    } catch (error) {
      if (error.status !== 401) toast(error.message);
      return;
    }
    renderTimeline();
  }

  function reloadAgenda() {
    clearTimeout(agendaTimer);
    agendaTimer = setTimeout(loadAgenda, 300);
  }

  function windowFor(minute) {
    return Math.max(0, Math.min(24 * 60 - SPAN_MIN, Math.floor((minute - LOOK_BACK_MIN) / 30) * 30));
  }

  function renderTimeline() {
    const timeline = $("[data-timeline]");
    if (!timeline) return;
    const minute = minuteOfDay(now());
    renderedMinute = Math.floor(minute);
    windowStart = windowFor(minute);
    timeline.style.setProperty("--span", String(SPAN_MIN));
    const axis = [];
    for (let m = 0; m <= SPAN_MIN; m += 30) {
      const at = windowStart + m;
      axis.push(el("span", { style: `--m:${m}`, text: `${pad(at / 60)}:${pad(at % 60)}` }));
    }
    $("[data-axis]", timeline).replaceChildren(...axis);
    const today = isoDay(state.now);
    // Redrawing replaces the talk keys: the one that had the focus gets it back.
    const focused = document.activeElement?.closest?.("[data-talk-id]")?.dataset.talkId;
    let shown = 0;
    for (const track of $$("[data-track]", timeline)) {
      const room = rooms.get(track.dataset.track);
      const talks = agenda.filter((talk) => talk.room_id === track.dataset.track && isoDay(talk.start) === today);
      shown += talks.length;
      track.replaceChildren(...talks.map((talk) => talkBlock(talk, room, minute, today)).filter(Boolean));
    }
    $("[data-agenda-empty]").hidden = shown > 0;
    if (focused) timeline.querySelector(`[data-talk-id="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
    placeNow(minute);
  }

  function talkBlock(talk, room, minute, today) {
    const start = isoMin(talk.start) - windowStart;
    const endMin = isoDay(talk.end) > today ? 24 * 60 : isoMin(talk.end);
    const end = endMin - windowStart;
    if (end <= 0 || start >= SPAN_MIN) return null;
    const from = Math.max(start, 0);
    const to = Math.min(end, SPAN_MIN);
    const running = Boolean(room?.talk && room.talk.id === talk.id);
    const next = Boolean(room?.next && room.next.id === talk.id);
    const past = !running && (talk.status === "done" || endMin <= minute);
    let classes = "timeline__talk";
    if (running) classes += " timeline__talk--now";
    else if (past) classes += " timeline__talk--past";
    else if (next) classes += " timeline__talk--next";
    if (room?.mode === "manual" && !past && !running) classes += " timeline__talk--manual";
    const parts = [];
    if (next) parts.push(el("span", { class: "tc", text: isoHm(talk.start) }));
    parts.push(el("span", { text: talk.title, lang: talk.language }));
    if (next && state.next_change && state.next_change.talk_id === talk.id) {
      parts.push(el("em", { class: "tc", "data-until": state.next_change.at, text: until(state.next_change.at) }));
    }
    const who = talk.speakers.length ? `. ${talk.speakers.join(", ")}` : "";
    return el("button", {
      type: "button", class: classes, "data-talk-id": talk.id,
      style: `--s:${from.toFixed(1)};--d:${(to - from).toFixed(1)}`,
      title: `${isoHm(talk.start)}–${isoHm(talk.end)}. ${talk.title}${who}`,
    }, ...parts);
  }

  function placeNow(minute) {
    const timeline = $("[data-timeline]");
    if (!timeline || windowStart === null) return;
    timeline.style.setProperty("--t", (minute - windowStart).toFixed(2));
    const flag = $("[data-now-label]", timeline);
    flag.textContent = say("now_at", { time: `${pad(minute / 60)}:${pad(minute % 60)}` });
    // The "Ahora" flag sits on the axis: hide the hour it covers, not half of it.
    const covered = flag.getBoundingClientRect();
    for (const label of $$("[data-axis] > span", timeline)) {
      label.style.visibility = "";
      const box = label.getBoundingClientRect();
      const overlaps = box.right > covered.left - 4 && box.left < covered.right + 4;
      label.style.visibility = overlaps ? "hidden" : "";
    }
  }

  onTick.push((t) => {
    const minute = minuteOfDay(t);
    if (windowStart !== null && (windowFor(minute) !== windowStart || Math.floor(minute) !== renderedMinute)) {
      renderTimeline();                           // a new minute: what is past may have changed
    } else {
      placeNow(minute);
    }
  });

  function renderNextChange(snapshot) {
    const line = $("[data-next-change]");
    const change = snapshot.next_change;
    const key = change ? `${change.text}|${change.at}` : snapshot.next_change_text;
    if (line.dataset.key === key) return;
    line.dataset.key = key;
    if (change) {
      line.replaceChildren(`${T.next_change} `, el("b", { text: change.text }), " ",
        el("span", { class: "tc", "data-until": change.at, text: until(change.at) }));
    } else {
      line.replaceChildren(snapshot.next_change_text);
    }
  }

  // ---- the drawer ----------------------------------------------------------------------------

  // The drawer is a modal dialog: while it is open the rest of the page is
  // inert (no focus, no clicks, hidden from assistive tech), so Tab stays in it.
  const BACKGROUND = [".masthead", "[data-attn]", "[data-wall]", "[data-schedule]"];

  function setInert(on) {
    for (const selector of BACKGROUND) {
      const node = $(selector, admin);
      if (node) node.inert = on;
    }
  }

  function markOpen(roomId) {
    for (const [id, node] of monitors) {
      $(".monitor__open", node)?.setAttribute("aria-expanded", String(id === roomId));
    }
  }

  // Where the focus goes back to, even if that element is replaced meanwhile
  // (the log and the agenda redraw themselves; a deleted talk is gone).
  function openerOf(node) {
    if (!(node instanceof Element)) return { node: null };
    return {
      node,
      talkId: node.closest("[data-talk-id]")?.dataset.talkId,
      roomId: node.closest("[data-monitor]")?.dataset.monitor ?? node.dataset.logRoom ?? node.dataset.roomId,
    };
  }

  function focusBack(opener) {
    let target = opener.node && opener.node.isConnected ? opener.node : null;
    if (!target && opener.talkId) target = document.querySelector(`[data-talk-id="${CSS.escape(opener.talkId)}"]`);
    if (!target && opener.roomId) target = $(".monitor__open", monitors.get(opener.roomId) || admin);
    target = target || $("[data-open-import]");
    target?.focus({ preventScroll: true });
  }

  // Tab and Shift+Tab wrap around inside the open drawer (the rest is inert,
  // but without this the focus would leave the page for the browser's UI).
  function trapTab(event) {
    const keys = $$("a[href], button, input, select, textarea, summary, [tabindex]", drawerEl)
      .filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length > 0);
    if (!keys.length) return;
    const first = keys[0];
    const last = keys[keys.length - 1];
    const inside = drawerEl.contains(document.activeElement);
    if (event.shiftKey && (!inside || document.activeElement === first)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (!inside || document.activeElement === last)) {
      event.preventDefault();
      first.focus();
    }
  }

  function openDrawer(kind, id, opener, fill) {
    const template = document.querySelector(`template[data-tpl="${kind}"]`);
    drawerEl.replaceChildren(template.content.cloneNode(true));
    const previous = drawer?.opener;
    drawer = { kind, id, opener: previous || openerOf(opener || document.activeElement) };
    drawerEl.classList.add("drawer--open");
    scrim.classList.add("scrim--open");
    setInert(true);
    fill();
    markOpen(kind === "room" ? id : null);
    $("[data-close]", drawerEl)?.focus({ preventScroll: true });
  }

  function closeDrawer() {
    if (!drawer) return;
    const { opener } = drawer;
    drawer = null;
    drawerEl.classList.remove("drawer--open");
    scrim.classList.remove("scrim--open");
    setInert(false);
    markOpen(null);
    setTimeout(() => { if (!drawer) drawerEl.replaceChildren(); }, 250);
    focusBack(opener);
  }

  // ---- the drawer: a room ------------------------------------------------------------------

  function openRoom(id, opener) {
    if (!rooms.has(id)) return;
    openDrawer("room", id, opener, () => {
      bindRoom();
      updateRoomDrawer();
      renderDrawerCc();
      renderRoomHistory();
      renderExports();
    });
  }

  function roomError(message) {
    const node = drawer?.kind === "room" ? $("[data-d-error]", drawerEl) : null;
    if (node) showNotice(node, message);
    else toast(message);
  }

  async function roomPost(path, button, body, label = null) {
    const roomId = drawer.id;
    const error = $("[data-d-error]", drawerEl);
    if (error) error.hidden = true;
    busy(button, true, label);
    try {
      await api("POST", `/rooms/${encodeURIComponent(roomId)}/${path}`, body);
      return true;
    } catch (err) {
      if (err.status !== 401) roomError(err.message);
      return false;
    } finally {
      busy(button, false);
    }
  }

  function bindRoom() {
    for (const button of $$("[data-mode]", drawerEl)) {
      button.addEventListener("click", async () => {
        const room = rooms.get(drawer.id);
        if (!room || room.mode === button.dataset.mode) return;
        pressMode(button.dataset.mode);
        if (!(await roomPost("mode", button, { mode: button.dataset.mode }))) pressMode(room.mode);
      });
    }
    $("[data-d-start]", drawerEl).addEventListener("click", () => togglePick());
    $('[data-slot="station-reload"]', drawerEl).addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const error = $("[data-d-error]", drawerEl);
      error.hidden = true;
      busy(button, true, T.working);
      try {
        const reply = await api("POST", `/rooms/${encodeURIComponent(drawer.id)}/station/reload`);
        if (reply && reply.sent) toast(T.station_reloading);
        else showNotice(error, T.station_not_connected);
      } catch (err) {
        if (err.status !== 401) roomError(err.message);
      } finally {
        busy(button, false);
      }
    });
    $("[data-d-station-copy]", drawerEl).addEventListener("click", async () => {
      const input = $("[data-d-station-url]", drawerEl);
      let copied = true;
      try {
        await navigator.clipboard.writeText(input.value);   // needs HTTPS or localhost
      } catch {
        input.focus();
        input.select();
        try {
          copied = document.execCommand("copy");
        } catch {
          copied = false;
        }
      }
      toast(copied ? T.copied : T.copy_failed);
    });
    $("[data-d-pick-cancel]", drawerEl).addEventListener("click", () => togglePick(false));
    $("[data-d-pick]", drawerEl).addEventListener("submit", async (event) => {
      event.preventDefault();
      const picked = event.currentTarget.querySelector('input[name="talk"]:checked');
      if (!picked) return;
      if (await roomPost("start-talk", $("[data-d-pick-go]", drawerEl), { talk_id: picked.value }, T.working)) togglePick(false);
    });
    $("[data-d-end]", drawerEl).addEventListener("click", (event) => roomPost("end-talk", event.currentTarget, undefined, T.working));
    $("[data-d-reconnect]", drawerEl).addEventListener("click", (event) => {
      const button = event.currentTarget;
      roomPost(button.dataset.kind === "restart" ? "restart" : "reconnect", button, undefined, T.reconnecting);
    });
  }

  function pressMode(mode) {
    for (const button of $$("[data-mode]", drawerEl)) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }

  function togglePick(force) {
    const pick = $("[data-d-pick]", drawerEl);
    const open = force ?? pick.hidden;
    pick.hidden = !open;
    $("[data-d-start]", drawerEl).setAttribute("aria-expanded", String(open));
    if (!open) return;
    const room = rooms.get(drawer.id);
    const talks = agenda
      .filter((talk) => talk.room_id === drawer.id && talk.status !== "done")
      .sort((a, b) => a.start.localeCompare(b.start));
    const running = room?.talk?.id;
    const preselect = room?.next?.id ?? talks.find((talk) => talk.id !== running)?.id;
    const list = $("[data-d-pick-list]", drawerEl);
    if (!talks.length) {
      list.replaceChildren(el("li", {}, el("p", { class: "drawer__hint", text: T.no_talks_to_start })));
    } else {
      list.replaceChildren(...talks.map((talk) => el("li", {}, el("label", {},
        el("input", { type: "radio", name: "talk", value: talk.id, checked: talk.id === preselect,
                      disabled: talk.id === running }),
        el("span", { class: "tc", text: isoHm(talk.start) }),
        el("span", { lang: talk.language }, talk.title, talk.id === running ? el("em", { text: ` (${T.talk_running})` }) : null)))));
    }
    $("[data-d-pick-go]", drawerEl).disabled = !talks.some((talk) => talk.id !== running);
    list.querySelector("input:checked")?.focus();
  }

  function metric(node, text, severity = null, none = false) {
    node.replaceChildren();
    if (severity) node.append(el("i", { class: `led led--${severity}` }));
    node.append(text);
    node.classList.toggle("metrics__value--none", none);
  }

  function updateRoomDrawer() {
    const room = rooms.get(drawer.id);
    if (!room) return;
    const d = drawerEl;
    const key = $("[data-d-key]", d);
    key.textContent = room.key ?? "";
    key.hidden = !room.key;
    $("[data-d-name]", d).textContent = room.name;
    drawerEl.setAttribute("aria-label", room.name);
    $("[data-d-led]", d).className = `led led--${room.state}`;
    $("[data-d-state]", d).textContent = room.text.state_line;

    const talk = room.talk || room.next;
    const section = $("[data-d-talk-sec]", d);
    section.hidden = !talk;
    if (talk) {
      const title = $("[data-d-talk]", d);
      title.textContent = talk.title;
      title.lang = talk.language;
      $("[data-d-who]", d).textContent = talk.speakers.join(", ");
      $("[data-d-dir]", d).textContent = `${talk.language.toUpperCase()} ▸ ${talk.target.toUpperCase()}`;
      $("[data-d-engine]", d).textContent = talk.engine === "glossary" ? plural("glossary_terms", talk.glossary) : T.engine_fast;
      $("[data-d-slot]", d).textContent = talk.free ? "" : `${isoHm(talk.start)}–${isoHm(talk.end)}`;
      $("[data-d-tc]", d).dataset.start = room.talk && room.state !== "idle" ? room.talk.actual_start || "" : "";
    }

    pressMode(room.mode);
    const end = $("[data-d-end]", d);
    if (end.getAttribute("aria-busy") !== "true") end.disabled = !room.talk;
    const reconnect = $("[data-d-reconnect]", d);
    if (reconnect.getAttribute("aria-busy") !== "true") {
      const suggested = room.issue && ["reconnect", "restart"].includes(room.issue.action);
      reconnect.className = suggested ? `btn btn--${room.issue.severity}` : "btn";
      reconnect.dataset.kind = room.issue?.action === "restart" ? "restart" : "reconnect";
      reconnect.disabled = !room.talk;
    }
    $("[data-d-start]", d).disabled = !room.has_source;
    renderStation(room);
    $("[data-d-hint]", d).textContent = room.text.hint || T.manual_hint;

    const status = room.status;
    const live = room.state !== "idle";
    const kind = room.issue?.kind;
    const severity = room.issue?.severity;
    const vu = $("[data-d-vu]", d);
    if (live) {
      metric($("[data-d-level]", d), `${decibels(status.level_db)} dB`, kind === "level" || kind === "silence" ? severity : null);
      const level = Math.max(0, Math.min(1, (status.level_db + 60) / 60));
      vu.style.setProperty("--level", level.toFixed(3));
      vu.setAttribute("aria-valuenow", status.level_db.toFixed(0));
      vu.setAttribute("aria-valuetext", `${decibels(status.level_db)} dBFS`);
      vu.title = say("level_min", { limit: limit(cfg.limits.level) });
      $("[data-d-level-note]", d).textContent = "";
    } else {
      metric($("[data-d-level]", d), "—", null, true);
      $("[data-d-level-note]", d).textContent = T.no_signal;
    }
    vu.hidden = !live;
    const latency = status.latency_p50_s;
    metric($("[data-d-latency]", d), latency == null ? "—" : `${N1.format(latency)} s`, kind === "latency" ? severity : null, latency == null);
    $("[data-d-latency-note]", d).textContent = say(latency == null ? "latency_none" : "latency_note", { limit: limit(cfg.limits.latency) });
    const quality = status.quality;
    metric($("[data-d-quality]", d), quality == null ? "—" : N2.format(quality), kind === "quality" ? severity : null, quality == null);
    $("[data-d-quality-note]", d).textContent = say(quality == null ? "quality_none" : "quality_note", { limit: limit(cfg.limits.quality) });
    metric($("[data-d-cost]", d), money(room.cost_usd));
    $("[data-d-raw]", d).textContent = status.detail;
    $("[data-d-cc-sec]", d).hidden = !live;
    $("[data-d-cc]", d).classList.toggle("drawer__cc--frozen", room.state === "down");
  }

  function renderStation(room) {
    const d = drawerEl;
    const station = room.station;
    $("[data-d-station-sec]", d).hidden = !station;
    $('[data-slot="station-reload"]', d).hidden = !station;
    if (!station) return;
    $("[data-d-station-led]", d).className = `led led--${station.connected ? "live" : "idle"}`;
    let text = T.station_offline;
    if (station.connected) {
      text = say("station_connected", { device: station.device || T.station_unknown_device });
      text += ` ${station.last_audio_age_s == null ? T.station_no_audio : say("station_audio", {
        db: station.level_db == null ? "—" : decibels(station.level_db),
        age: N1.format(station.last_audio_age_s),
      })}`;
    }
    $("[data-d-station-state]", d).textContent = text;
    const link = cfg.stations?.[room.id];
    const url = $("[data-d-station-url]", d);
    if (link && !url.value) {
      url.value = link.url;                          // the server's string: the same one the QR encodes
      $("[data-d-station-qr]", d).src = link.qr;
    }
  }

  function renderDrawerCc() {
    if (drawer?.kind !== "room") return;
    const tail = tails.get(drawer.id);
    $("[data-d-closed]", drawerEl).textContent = tail ? tail.closed : "";
    $("[data-d-open]", drawerEl).textContent = tail ? tail.open : "";
    $("[data-d-cc]", drawerEl).lang = rooms.get(drawer.id)?.lang || "";
  }

  function renderRoomHistory() {
    if (drawer?.kind !== "room") return;
    const events = coalesce(logs.filter((event) => event.room_id === drawer.id).reverse()).slice(0, 40);
    $("[data-d-log]", drawerEl).replaceChildren(...events.map((row) => logItem(row, { compact: true })));
    $("[data-d-log-empty]", drawerEl).hidden = events.length > 0;
  }

  // task-11r-brief.md: a room's finished talks, with their live/corrected
  // export links and the corrected version's build status.
  async function renderExports() {
    if (drawer?.kind !== "room") return;
    const id = drawer.id;
    let list;
    try {
      list = await api("GET", "/exports");
    } catch {
      return; // best-effort: the rest of the drawer still works without it
    }
    if (drawer?.kind !== "room" || drawer.id !== id) return; // the drawer moved on while this was in flight
    const items = list.filter((entry) => entry.room_id === id);
    $("[data-d-exports]", drawerEl).replaceChildren(...items.map(exportItem));
    $("[data-d-exports-empty]", drawerEl).hidden = items.length > 0;
  }

  function exportItem(entry) {
    return el("li", { class: "exports__item" },
      el("b", { class: "exports__title" }, entry.title),
      ...entry.exports.map(exportLangRow));
  }

  function exportLangRow(row) {
    const parts = [el("b", { text: row.lang.toUpperCase() }), el("span", { text: `${T.export_live}:` }), ...exportLinks(row.live)];
    if (row.corrected) {
      const status = row.corrected.status;
      parts.push(el("span", { text: `${T.export_corrected}:` }));
      parts.push(el("span", { class: `exports__status exports__status--${status}`, text: T[`export_status_${status}`] || status }));
      if (row.corrected.links) parts.push(...exportLinks(row.corrected.links));
    }
    return el("div", { class: "exports__lang" }, ...parts);
  }

  function exportLinks(links) {
    return [
      el("a", { href: links.srt, text: "SRT" }),
      el("a", { href: links.vtt, text: "VTT" }),
      el("a", { href: links.txt, text: "TXT" }),
    ];
  }

  // ---- the drawer: a talk of the agenda ------------------------------------------------------

  const LIVE_FIELDS = new Set(["title", "targets", "glossary"]);
  const splitList = (text) => text.split(",").map((part) => part.trim()).filter(Boolean);

  function parseGlossary(text) {
    return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const at = line.indexOf("=");
      if (at < 0) return { term: line, keep_in_english: true };
      return { term: line.slice(0, at).trim(), keep_in_english: false, translation: line.slice(at + 1).trim() };
    }).filter((term) => term.term);
  }

  async function openTalk(id, opener) {
    let talk;
    try {
      talk = await api("GET", `/talks/${encodeURIComponent(id)}`);
    } catch (error) {
      if (error.status !== 401) toast(error.message);
      return;
    }
    const current = { talk };
    openDrawer("talk", id, opener, () => {
      fillTalk(current.talk);
      bindTalk(current);
    });
  }

  function fillTalk(talk) {
    const d = drawerEl;
    const form = $("[data-talk-form]", d);
    const field = (name) => form.elements.namedItem(name);
    const room = rooms.get(talk.room_id);
    drawerEl.setAttribute("aria-label", T.edit_talk);
    $("[data-t-where]", d).textContent =
      `${room ? room.name : talk.room_id}, ${isoHm(talk.start)}–${isoHm(talk.end)}. ${T[`status_${talk.status}`] || talk.status}.`;
    $("[data-t-led]", d).className = `led led--${talk.status === "live" ? room?.state || "live" : "idle"}`;
    field("title").value = talk.title;
    field("speakers").value = talk.speakers.join(", ");
    field("language").value = talk.language;
    for (const box of form.querySelectorAll('input[name="targets"]')) box.checked = talk.targets.includes(box.value);
    field("engine").value = talk.engine;
    field("start").value = talk.start.slice(0, 16);
    field("end").value = talk.end.slice(0, 16);
    field("abstract").value = talk.abstract;
    field("tags").value = talk.tags.join(", ");
    field("glossary").value = talk.glossary.map((g) => (g.translation ? `${g.term}=${g.translation}` : g.term)).join("\n");
    const live = talk.status === "live";
    for (const control of form.querySelectorAll("input, select, textarea")) {
      control.disabled = live && !LIVE_FIELDS.has(control.name);
    }
    const note = $("[data-t-note]", d);
    note.hidden = talk.status === "scheduled";
    note.textContent = live ? T.live_locked : T.done_note;
    $("[data-t-delete-sec]", d).hidden = talk.status !== "scheduled";
    $("[data-t-confirm]", d).hidden = true;
    $("[data-t-delete]", d).hidden = false;
  }

  function bindTalk(current) {
    const d = drawerEl;
    const form = $("[data-talk-form]", d);
    const field = (name) => form.elements.namedItem(name);
    const error = $("[data-t-error]", d);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.hidden = true;
      const talk = current.talk;
      const title = field("title").value.trim();
      if (!title) {
        showNotice(error, say("error_prefix", { detail: T.talk_title }));
        field("title").focus();
        return;
      }
      const body = {
        title,
        targets: Array.from(form.querySelectorAll('input[name="targets"]:checked'), (box) => box.value),
        glossary: parseGlossary(field("glossary").value),
      };
      if (talk.status !== "live") {
        Object.assign(body, {
          speakers: splitList(field("speakers").value),
          language: field("language").value,
          engine: field("engine").value,
          start: field("start").value,
          end: field("end").value,
          abstract: field("abstract").value,
          tags: splitList(field("tags").value),
        });
      }
      const save = $("[data-t-save]", d);
      busy(save, true, T.working);
      try {
        current.talk = await api("PUT", `/talks/${encodeURIComponent(talk.id)}`, body);
        fillTalk(current.talk);
        toast(T.saved);
        reloadAgenda();
      } catch (err) {
        if (err.status !== 401) showNotice(error, err.message);
      } finally {
        busy(save, false);
      }
    });
    $("[data-t-suggest-glossary]", d).addEventListener("click", async (event) => {
      const button = event.currentTarget;
      error.hidden = true;
      busy(button, true, T.working);
      try {
        const terms = await api("POST", `/talks/${encodeURIComponent(current.talk.id)}/suggest-glossary`);
        field("glossary").value = terms.map((g) => (g.translation ? `${g.term}=${g.translation}` : g.term)).join("\n");
        toast(terms.length ? T.glossary_suggested : T.glossary_suggest_empty);
      } catch (err) {
        if (err.status !== 401) showNotice(error, err.message);
      } finally {
        busy(button, false);
      }
    });
    const confirm = $("[data-t-confirm]", d);
    $("[data-t-delete]", d).addEventListener("click", (event) => {
      event.currentTarget.hidden = true;
      $("[data-t-confirm-text]", d).textContent = say("delete_confirm", { title: current.talk.title });
      confirm.hidden = false;
      $("[data-t-delete-no]", d).focus();
    });
    $("[data-t-delete-no]", d).addEventListener("click", () => {
      confirm.hidden = true;
      $("[data-t-delete]", d).hidden = false;
      $("[data-t-delete]", d).focus();
    });
    $("[data-t-delete-yes]", d).addEventListener("click", async (event) => {
      const button = event.currentTarget;
      busy(button, true, T.working);
      try {
        await api("DELETE", `/talks/${encodeURIComponent(current.talk.id)}`);
        drawer.opener = { node: null };             // that talk is gone: the focus goes to "Importar agenda"
        closeDrawer();
        toast(T.deleted);
        reloadAgenda();
      } catch (err) {
        busy(button, false);
        if (err.status !== 401) showNotice(error, err.message);
      }
    });
  }

  // ---- the drawer: importing the agenda --------------------------------------------------------

  function openImport(opener) {
    openDrawer("import", null, opener, () => {
      drawerEl.setAttribute("aria-label", T.import_agenda);
      const form = $("[data-import-form]", drawerEl);
      const field = (name) => form.elements.namedItem(name);
      const error = $("[data-i-error]", drawerEl);
      $("[data-use-nerdearla]", drawerEl).addEventListener("click", () => {
        field("url").value = cfg.nerdearlaUrl;
        field("format").value = "nerdearla";
        field("url").focus();
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        error.hidden = true;
        const file = field("file").files[0];
        const url = field("url").value.trim();
        if (Boolean(file) === Boolean(url)) {
          showNotice(error, T.import_pick_one);
          return;
        }
        const data = new FormData();
        if (file) data.append("file", file);
        else data.append("url", url);
        if (field("format").value) data.append("format", field("format").value);
        const go = $("[data-i-go]", drawerEl);
        busy(go, true, T.working);
        try {
          showImport(await api("POST", "/agenda/import", data));
          reloadAgenda();
        } catch (err) {
          if (err.status !== 401) showNotice(error, err.message);
        } finally {
          busy(go, false);
        }
      });
    });
  }

  function showImport(result) {
    const d = drawerEl;
    $("[data-i-result]", d).hidden = false;
    $("[data-i-imported]", d).textContent = plural("imported_n", result.imported);
    const lists = [
      ["skipped", result.skipped, (s) => [s.title || s.source_id || "?", " ", el("span", { text: s.reason })]],
      ["removed", result.removed, (r) => [r.title]],
    ];
    for (const [name, items, render] of lists) {
      const box = $(`[data-i-${name}-box]`, d);
      box.hidden = items.length === 0;
      $(`[data-i-${name}-sum]`, d).textContent = plural(`${name}_n`, items.length);
      $(`[data-i-${name}]`, d).replaceChildren(...items.map((item) => el("li", {}, ...render(item))));
    }
  }

  // ---- input ---------------------------------------------------------------------------------

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const action = target.closest("[data-action]");
    if (action && action.dataset.action) {
      event.stopPropagation();
      runAction(action);
      return;
    }
    if (target.closest("[data-close]") && drawerEl.contains(target)) return closeDrawer();
    if (target.closest("[data-scrim]")) return closeDrawer();
    const talk = target.closest("[data-talk-id]");
    if (talk) return openTalk(talk.dataset.talkId, talk);
    if (target.closest("[data-open-import]")) return openImport(target.closest("[data-open-import]"));
    const filter = target.closest("[data-log-filter]");
    if (filter) {
      logFilter = filter.dataset.logFilter;
      return renderLog();
    }
    if (target.closest("[data-log-more]")) {
      logFilter = "all";
      return renderLog();
    }
    const logged = target.closest("[data-log-room]");
    if (logged) return openRoom(logged.dataset.logRoom, logged);
    const monitor = target.closest("[data-monitor]");
    if (monitor) openRoom(monitor.dataset.monitor, $(".monitor__open", monitor));
  });

  $("[data-toast-close]", toastEl)?.addEventListener("click", () => { toastEl.hidden = true; });

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target instanceof Element ? event.target : null;
    if (event.key === "Tab" && drawer) {
      trapTab(event);
      return;
    }
    if (event.key === "Escape") {
      if (drawer) {
        event.preventDefault();
        closeDrawer();
      }
      return;
    }
    if (target && target.closest("input, textarea, select, [contenteditable]")) return;
    if (/^[1-9]$/.test(event.key)) {
      const room = Array.from(rooms.values()).find((r) => String(r.key) === event.key);
      if (room) {
        event.preventDefault();
        openRoom(room.id, $(".monitor__open", monitors.get(room.id)));   // Esc gives the focus back to it
      }
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && target?.matches("[data-log-room]")) {
      event.preventDefault();                       // a log row opens its room, like a key
      openRoom(target.dataset.logRoom, target);
    }
  });

  // ---- the stream ---------------------------------------------------------------------------

  let source = null;
  let stale = false;
  let lastLogId = null;
  let retryTimer = 0;

  function setStale(on) {
    if (stale === on || expired) return;
    stale = on;
    admin.classList.toggle("admin--stale", on);
    if (on) toast(T.connection_lost, { sticky: true });
    else toast(T.connection_back);
  }

  function connect() {
    if (expired) return;
    const url = cfg.streamUrl + (lastLogId !== null ? `&lastEventId=${lastLogId}` : "");
    source = new EventSource(url);
    const on = (name, handler) => source.addEventListener(name, (event) => handler(JSON.parse(event.data)));
    on("state", onState);
    on("log", onLog);
    on("cc", onCc);
    on("notice", (notice) => {
      if (notice.kind !== "room_reconnect") reloadAgenda();
      if (notice.kind === "talk_ended" || notice.kind === "export_ready" || notice.kind === "export_failed") {
        renderExports();
      }
    });
    on("bye", () => sessionExpired());
    source.addEventListener("error", () => {
      setStale(true);
      if (source && source.readyState === EventSource.CLOSED) {
        source = null;
        probe();
      }
    });
  }

  // EventSource gives up on a refused stream without saying why: ask the API.
  async function probe() {
    try {
      const response = await fetch("/api/admin/rooms", { headers: { [CSRF_HEADER]: "1" } });
      if (response.status === 401) {
        sessionExpired();
        return;
      }
    } catch {
      /* the server is down: try again below */
    }
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, RETRY_MS);
  }

  onTick.push(() => {
    if (source && Date.now() - lastFrame > STALE_MS) setStale(true);
  });

  const date = $("[data-event-date]");
  if (date) {
    try {
      const text = new Intl.DateTimeFormat(LOCALE, { timeZone: cfg.tz, weekday: "long", day: "numeric", month: "long" })
        .format(new Date(now()));
      date.textContent = text.charAt(0).toUpperCase() + text.slice(1);
    } catch {
      /* keep the server's text */
    }
  }

  onState(state);
  renderLog();
  loadAgenda();
  connect();
  tick();
})();
