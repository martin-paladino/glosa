/* Glosa: the room list (/). Plain JS, no build. Works without it: the page
   is complete as the server renders it; this only adds

   - the theme button (static/js/theme.js, shared with the room view);
   - "Ver más" on abstracts that don't fit in their clamp (button with
     aria-expanded; without JS the abstract simply shows in full);
   - the minute track and "Quedan N min" / "Empieza en N min" kept current
     while the page stays open (the server rendered them for page load). */
(() => {
  "use strict";

  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const format = (template, vars) =>
    template.replace(/\{(\w+)\}/g, (_, key) => (key in vars ? vars[key] : ""));

  // ---- theme ------------------------------------------------------------------

  const themeButton = document.querySelector('[data-action="theme"]');
  if (themeButton && window.GlosaTheme) {
    let names = {};
    try { names = JSON.parse(themeButton.dataset.themeNames || "{}"); } catch { /* keep {} */ }
    const label = () => window.GlosaTheme.label(themeButton, themeButton.dataset.themeLabel || "", names);
    themeButton.addEventListener("click", () => { window.GlosaTheme.cycle(); label(); });
    label();
  }

  // ---- "Ver más" ----------------------------------------------------------------

  const abouts = $$("[data-about]");
  for (const about of abouts) about.dataset.clamp = "";

  function fitMore() {
    for (const about of abouts) {
      const text = about.querySelector("p");
      const button = about.querySelector("[data-more]");
      if (!text || !button) continue;
      const open = button.getAttribute("aria-expanded") === "true";
      button.hidden = !open && text.scrollHeight <= text.clientHeight + 1;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-more]");
    if (!button) return;
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? button.dataset.lessLabel : button.dataset.moreLabel;
    button.closest("[data-about]").toggleAttribute("data-open", open);
  });

  fitMore();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitMore);
  window.addEventListener("resize", fitMore);

  // ---- time ---------------------------------------------------------------------

  const ceilMinutes = (ms) => Math.max(0, Math.ceil(ms / 60000));

  function tick() {
    const now = Date.now();
    for (const track of $$("[data-track]")) {
      const start = Date.parse(track.dataset.start);
      const end = Date.parse(track.dataset.end);
      if (!(end > start)) continue;
      const bar = track.querySelector(".room-card__bar");
      const left = track.querySelector("[data-left]");
      const progress = Math.min(1, Math.max(0, (now - start) / (end - start)));
      if (bar) bar.style.setProperty("--progress", progress.toFixed(3));
      if (left) {
        left.textContent = now < end
          ? format(track.dataset.leftTemplate, { n: Math.max(1, ceilMinutes(end - now)) })
          : "";
      }
    }
    for (const soon of $$("[data-soon]")) {
      const start = Date.parse(soon.dataset.start);
      if (start > now) soon.textContent = format(soon.dataset.soonTemplate, { n: ceilMinutes(start - now) });
    }
  }

  setInterval(tick, 30000);
})();
