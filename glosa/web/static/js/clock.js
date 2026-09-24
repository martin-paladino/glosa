/* Glosa: house clock. Plain JS, no build, no dependencies.

   Fills every [data-clock] with the local time and unhides it. The value
   picks the format: "hms" gives HH:MM:SS (the control room), anything else
   HH:MM (the audience, where a ticking second would be the only thing moving
   besides the caption cursor).
*/
(() => {
  "use strict";

  const clocks = [...document.querySelectorAll("[data-clock]")];
  if (!clocks.length) return;

  const pad = (n) => String(n).padStart(2, "0");
  const withSeconds = clocks.some((clock) => clock.dataset.clock === "hms");

  function tick() {
    const now = new Date();
    const hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const hms = `${hm}:${pad(now.getSeconds())}`;
    for (const clock of clocks) {
      const text = clock.dataset.clock === "hms" ? hms : hm;
      if (clock.textContent !== text) {
        clock.textContent = text;
        clock.setAttribute("datetime", text);
      }
      clock.hidden = false;
    }
    const wait = withSeconds
      ? 1000 - now.getMilliseconds()
      : (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
    setTimeout(tick, wait + 20);
  }

  tick();
})();
