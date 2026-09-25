// Runs glosa/web/static/js/room.js under Node with a tiny fake DOM, feeds one
// SSE stream the CaptionMsg list read from stdin as JSON ({"lang", "i18n",
// "msgs"}),
// and prints the phrases on the page as JSON. Used by tests/web/test_room_js.py.
// Only what room.js touches is faked; the room.html hooks are data-* attributes.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOM_JS = path.join(__dirname, "..", "..", "glosa", "web", "static", "js", "room.js");

const kebab = (key) => key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());

function matches(el, selector) {
  return selector.split(",").some((part) => {
    const m = part.trim().match(/^([a-z]*)((?:\[[^\]]+\])*)$/i);
    if (!m) return false;
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    const attrs = m[2].match(/\[[^\]]+\]/g) || [];
    return attrs.every((a) => {
      const [, name, value] = a.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return value === undefined ? el.hasAttribute(name) : el.getAttribute(name) === value;
    });
  });
}

class FakeNode {
  constructor() { this.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get previousSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) - 1] || null;
  }
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const i = parent.childNodes.indexOf(this);
    this.remove();
    parent.childNodes.splice(i, 0, ...nodes.map((n) => parent.adopt(n)));
  }
}

class FakeText extends FakeNode {
  constructor(text) { super(); this.nodeType = 3; this.data = String(text); }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.options = [];
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    const props = {};
    this.style = { getPropertyValue: (k) => props[k] || "", setProperty: (k, v) => { props[k] = v; } };
  }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get classList() {
    const get = () => this.className.split(/\s+/).filter(Boolean);
    const set = (list) => { this.className = list.join(" "); };
    return {
      add: (...c) => set([...new Set([...get(), ...c])]),
      remove: (...c) => set(get().filter((x) => !c.includes(x))),
      contains: (c) => get().includes(c),
      toggle: (c, force) => {
        const want = force === undefined ? !get().includes(c) : Boolean(force);
        set(want ? [...new Set([...get(), c])] : get().filter((x) => x !== c));
        return want;
      },
    };
  }
  get dataset() {
    return new Proxy({}, {
      get: (_, k) => (typeof k === "string" ? (this.getAttribute("data-" + kebab(k)) ?? undefined) : undefined),
      set: (_, k, v) => { this.setAttribute("data-" + kebab(k), v); return true; },
      deleteProperty: (_, k) => { this.removeAttribute("data-" + kebab(k)); return true; },
      has: (_, k) => this.hasAttribute("data-" + kebab(k)),
    });
  }
  adopt(node) {
    const n = typeof node === "string" || typeof node === "number" ? new FakeText(node) : node;
    if (n.parentNode) n.remove();
    n.parentNode = this;
    return n;
  }
  addEventListener(type, fn) { (this._listeners ??= {})[type] ??= []; this._listeners[type].push(fn); }
  dispatchEvent(type, evt) { for (const fn of (this._listeners && this._listeners[type]) || []) fn(evt); }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) this.childNodes.splice(i, 1);
    node.parentNode = null;
  }
  append(...nodes) { for (const n of nodes) this.childNodes.push(this.adopt(n)); }
  prepend(...nodes) { this.childNodes.unshift(...nodes.map((n) => this.adopt(n))); }
  replaceChildren(...nodes) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.replaceChildren(String(v)); }
  closest(selector) {
    for (let n = this; n; n = n.parentNode) if (n.nodeType === 1 && matches(n, selector)) return n;
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matches(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  requestFullscreen() { return Promise.resolve(); }
}

function h(tag, attrs = {}, ...children) {
  const node = new FakeElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const lang = input.lang;

const config = {
  slug: "r1", streamBase: "/api/stream/r1/", summaryBase: "/api/summary/r1/",
  langs: [lang], defaultLang: lang, forcedLang: lang,
  source: lang, talkId: null, endonyms: {}, langNames: {}, i18n: input.i18n,
};
const select = h("select", { "data-lang-select": "" });
select.options = [h("option", { value: lang })];
const transcript = h("div", { "data-transcript": "", "data-layout": "single" },
  h("p", { "data-stage-note": "waiting" }, "waiting"));
const summaryToggle = h("button", { "data-action": "summary", "data-summary-toggle": "", "aria-expanded": "false" });
summaryToggle.hidden = true;
const summaryScrim = h("div", { "data-action": "summary-close", "data-summary-scrim": "" });
const summaryBullets = h("ul", { "data-summary-bullets": "" });
const summaryAgo = h("p", { "data-summary-ago": "" });
const summaryPanel = h("div", { "data-summary-panel": "" },
  h("button", { "data-action": "summary-close", "data-summary-close": "" }, "×"),
  summaryBullets, summaryAgo);
const html = h("html", {},
  h("body", {},
    h("span", { "data-direction": "" }),
    h("span", { "data-status": "", "data-state": "idle" }),
    h("p", { "data-talk-title": "", hidden: "" }),
    h("p", { "data-talk-meta": "", hidden: "" }, h("span", { "data-talk-speakers": "" })),
    select,
    h("button", { "data-action": "theme" }),
    h("button", { "data-action": "fullscreen" }),
    h("main", { "data-stage": "" }, transcript),
    h("button", { "data-action": "live", hidden: "" }),
    summaryToggle, summaryScrim, summaryPanel,
    h("script", { id: "glosa-room" }, JSON.stringify(config)),
  ));

const documentListeners = {};
const document = {
  documentElement: html,
  fullscreenEnabled: false,
  fullscreenElement: null,
  visibilityState: "visible",
  getElementById: (id) => html.querySelector(`[id="${id}"]`),
  querySelector: (s) => html.querySelector(s),
  querySelectorAll: (s) => html.querySelectorAll(s),
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_, tag) => new FakeElement(tag),
  addEventListener(type, fn) { (documentListeners[type] ??= []).push(fn); },
  dispatchEvent(type, evt) { for (const fn of documentListeners[type] || []) fn(evt); },
};

const sources = [];
class EventSource {
  constructor(url) { this.url = url; this.readyState = 1; sources.push(this); }
  close() { this.readyState = 2; }
}
EventSource.CLOSED = 2;

// Task 17: a single canned response ({"status", "body"}) every fetch() call
// resolves to (default: 404, no summary) -- input.fetch overrides it. Real
// timers (setInterval/clearInterval) are stubbed out: the harness drives the
// summary poll itself (init call, then optionally one simulated click).
const fetchResponse = input.fetch || { status: 404 };
let fetchCalls = 0;
async function fetchStub(url) {
  fetchCalls += 1;
  return {
    ok: fetchResponse.status >= 200 && fetchResponse.status < 300,
    status: fetchResponse.status,
    json: async () => fetchResponse.body,
  };
}

const storage = new Map();
const context = {
  document,
  EventSource,
  ResizeObserver: class { observe() {} },
  requestAnimationFrame: () => 0,
  fetch: fetchStub,
  setInterval: () => 0,
  clearInterval: () => {},
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  navigator: {},
  location: { href: "http://localhost/s/r1" },
  history: { replaceState() {} },
  URL,
  Date,
  performance,
  setTimeout,
  clearTimeout,
  console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(ROOM_JS, "utf8"), context, { filename: "room.js" });

const [source] = sources;
if (source.onopen) source.onopen();
for (const msg of input.msgs) source.onmessage({ data: JSON.stringify(msg) });

// A real macrotask flushes every microtask queued so far (Node drains the
// whole microtask queue, however many await hops each one has, before a
// macrotask runs) -- used both before and after simulating a click, so the
// click lands the way a reader's would: after the page's own initial
// summary poll has already resolved (real life: the button stays hidden
// until then), and its own re-poll gets to resolve too before we read state.
const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  await flush();   // the init pollSummary() resolves

  // input.click: a data-action name (or list of them, dispatched in order,
  // each flushed before the next -- e.g. ["summary", "summary-close"] opens
  // then closes the panel). input.keys: key names dispatched as keydown
  // events on document, same way (e.g. "Escape").
  const clicks = Array.isArray(input.click) ? input.click : input.click ? [input.click] : [];
  for (const action of clicks) {
    const target = html.querySelector(`[data-action="${action}"]`);
    document.dispatchEvent("click", { target, preventDefault() {} });
    await flush();   // that click's own re-poll (if any) resolves before the next one
  }
  for (const key of input.keys || []) {
    document.dispatchEvent("keydown", { key, target: html, closest: () => null, preventDefault() {} });
    await flush();
  }

  const page = transcript.querySelector("[data-page]");
  const phrases = page.querySelectorAll("[data-seg]").map((span) => ({
    seg: Number(span.getAttribute("data-seg")),
    text: span.textContent,
    open: span.hasAttribute("data-open"),
  }));
  const live = page.querySelector("[data-live]");
  process.stdout.write(JSON.stringify({
    url: source.url,
    phrases,
    text: page.querySelectorAll("p").map((p) => p.textContent).join(" | "),
    live: live ? live.textContent : null,
    summary: {
      fetchCalls,
      toggleHidden: summaryToggle.hidden,
      ariaExpanded: summaryToggle.getAttribute("aria-expanded"),
      ariaDisabled: summaryToggle.getAttribute("aria-disabled"),
      panelOpen: summaryPanel.classList.contains("summary-panel--open"),
      scrimOpen: summaryScrim.classList.contains("summary-scrim--open"),
      bullets: summaryBullets.querySelectorAll("li").map((li) => li.textContent),
      ago: summaryAgo.textContent,
    },
  }));
})();
