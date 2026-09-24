/* Solo demo para las maquetas: A−/A+ y tema sobre la .room más cercana.
   En la app, lo mismo se aplica sobre <html> y se guarda en localStorage. */
(() => {
  const SCALES = [0.8, 0.9, 1, 1.15, 1.3, 1.5, 1.75];
  const THEMES = ["light", "dark", "contrast"];
  const NAMES = { light: "claro", dark: "oscuro", contrast: "alto contraste" };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const scope = button.closest(".room") || document.documentElement;

    if (button.dataset.action === "size") {
      const current = parseFloat(getComputedStyle(scope).getPropertyValue("--caption-scale")) || 1;
      let i = SCALES.indexOf(current);
      if (i < 0) i = 2;
      i = Math.max(0, Math.min(SCALES.length - 1, i + Number(button.dataset.step)));
      scope.style.setProperty("--caption-scale", SCALES[i]);
    }

    if (button.dataset.action === "theme") {
      const current = scope.dataset.theme
        || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
      scope.dataset.theme = next;
      button.setAttribute("aria-label", `Tema: ${NAMES[next]}. Cambiar tema`);
    }
  });
})();
