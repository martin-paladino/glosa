/* Glosa: the OBS/vMix overlay (/overlay/{room}?lang=&lines=&size=&logo=1).
   Task 14b. A transparent, chrome-less page: reads the same CaptionMsg SSE
   stream room.js reads (/api/stream/{slug}/{lang}, glosa/web/public_api.py)
   --

     {"type": "append", "seg", "text"}   text is added to the end of phrase `seg`
     {"type": "set", "seg", "text"}      the whole text of the open phrase `seg` so far
     {"type": "close", "seg"}            phrase `seg` is finished
     {"type": "talk", "data": {"talk_id", ...}}

   -- but keeps a state machine of its own rather than importing room.js's:
   that module also carries scroll/follow, bilingual, theme and text-size
   logic this page has no use for (a fixed-size box with a transparent
   background, meant to sit inside a video frame). This is that same wire
   format against a much smaller surface, not a fork of room.js's caption
   logic, and it never touches room.js itself.

   The roll-up itself is plain CSS (.overlay__window: a fixed-height flex
   column, its single child bottom-anchored and clipped -- glosa.css): each
   appended phrase just grows the paragraph; the old lines that no longer
   fit are pushed above the visible window and clipped, without this script
   moving anything. `trim()` below only exists so that paragraph doesn't
   grow forever across a multi-hour talk -- everything it drops has already
   scrolled out of view.
*/
(() => {
  "use strict";

  const configEl = document.getElementById("glosa-overlay");
  if (!configEl) return;
  const cfg = JSON.parse(configEl.textContent);

  const overlay = document.querySelector("[data-overlay]");
  const textEl = document.querySelector("[data-overlay-text]");
  if (!overlay || !textEl) return;

  const RETRY_MS = 5000;
  const MAX_PHRASES = 40; // well above what the box ever shows; keeps the DOM bounded

  const phrases = new Map(); // seg -> <span>
  const trailing = new Map(); // seg -> whitespace held back from the end of the phrase
  let open = null;
  let talkId;

  function el(tag, attrs) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }

  function settle(span) {
    span.classList.remove("phrase--open");
    span.removeAttribute("data-open");
    if (open === span) open = null;
  }

  function trim() {
    while (textEl.childElementCount > MAX_PHRASES) {
      const first = textEl.firstElementChild;
      phrases.delete(first.dataset.seg);
      trailing.delete(first.dataset.seg);
      first.remove();
      if (textEl.firstChild && textEl.firstChild.nodeType === 3 && !textEl.firstChild.textContent.trim()) {
        textEl.firstChild.remove(); // the separating space that went with it
      }
    }
  }

  function append(seg, text) {
    let span = phrases.get(seg);
    if (!span) {
      if (open) settle(open);
      text = text.replace(/^\s+/, "");
      if (!text) return;
      span = el("span", { class: "phrase phrase--open", "data-seg": seg, "data-open": "" });
      if (textEl.childNodes.length) textEl.append(" ");
      textEl.append(span);
      phrases.set(seg, span);
      open = span;
      overlay.hidden = false;
      trim();
    }
    const full = (trailing.get(seg) || "") + text;
    const visible = full.replace(/\s+$/, "");
    trailing.set(seg, full.slice(visible.length));
    if (visible) span.append(visible);
  }

  function set(seg, text) {
    const span = phrases.get(seg);
    if (!span) {
      append(seg, text);
      return;
    }
    const full = text.replace(/^\s+/, "");
    const visible = full.replace(/\s+$/, "");
    trailing.set(seg, full.slice(visible.length));
    if (visible) {
      span.replaceChildren(visible);
      return;
    }
    const before = span.previousSibling;
    if (before && before.nodeType === 3 && !before.textContent.trim()) before.remove();
    span.remove();
    phrases.delete(seg);
    trailing.delete(seg);
    if (open === span) open = null;
  }

  function close(seg) {
    const span = phrases.get(seg);
    if (span && span === open) settle(span);
  }

  function clear() {
    textEl.replaceChildren();
    phrases.clear();
    trailing.clear();
    open = null;
    overlay.hidden = true;
  }

  function onTalk(data) {
    const id = data.talk_id || null;
    if (id && id !== talkId) clear(); // a new talk starts on an empty box
    talkId = id;
    if (!id) overlay.hidden = true; // between talks: nothing to burn into the stream
  }

  let retry = null;

  function connect() {
    const source = new EventSource(cfg.streamBase + encodeURIComponent(cfg.lang));
    source.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "append":
          if (typeof msg.text === "string" && msg.seg != null) append(msg.seg, msg.text);
          break;
        case "set":
          if (typeof msg.text === "string" && msg.seg != null) set(msg.seg, msg.text);
          break;
        case "close":
          close(msg.seg);
          break;
        case "talk":
          onTalk(msg.data || {});
          break;
        default:
          break;
      }
    };
    source.onerror = () => {
      // EventSource retries on its own unless the server refused the stream
      // outright; a fresh one (no Last-Event-ID) replays the whole history,
      // so the box is rebuilt from scratch to match, same rule as room.js.
      if (source.readyState === EventSource.CLOSED && !retry) {
        retry = setTimeout(() => {
          retry = null;
          source.close();
          clear();
          talkId = undefined;
          connect();
        }, RETRY_MS);
      }
    };
  }

  connect();
})();
