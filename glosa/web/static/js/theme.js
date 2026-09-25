/* Glosa: the reader's theme, shared by the audience pages (the room view and
   the room list). Plain JS, no build.

   base.html applies the saved theme before first paint; this cycles it:
   system (prefers-color-scheme) -> light -> dark -> high contrast -> system,
   saved in localStorage as "glosa.theme" (no key = follow the system). Each
   page wires its own button: GlosaTheme.cycle() on click, then
   GlosaTheme.label(button, "Tema: {name}. Cambiar tema", names) to keep its
   accessible name in step. docs/design/README.md §4 "Temas". */
(() => {
  "use strict";

  const THEMES = ["system", "light", "dark", "contrast"];
  const KEY = "glosa.theme";
  const root = document.documentElement;

  const current = () => (THEMES.includes(root.dataset.theme) ? root.dataset.theme : "system");

  function cycle() {
    const next = THEMES[(THEMES.indexOf(current()) + 1) % THEMES.length];
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch { /* private mode: the choice just doesn't persist */ }
    return next;
  }

  function label(button, template, names) {
    if (!button) return;
    const text = template.replace("{name}", names[current()] || "");
    button.setAttribute("aria-label", text);
    button.title = text;
  }

  window.GlosaTheme = { THEMES, current, cycle, label };
})();
