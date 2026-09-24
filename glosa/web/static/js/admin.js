/* Glosa: minimal admin panel (/admin). Plain JS, no build, no dependencies.

   Two independent bits, both progressive enhancement over plain HTML:
     - the login form posts to /admin/login on its own; here we intercept it
       only to show the wrong-password message inline instead of a raw 401
       page;
     - each room's Start/Stop button posts to /api/admin/rooms/{id}/start or
       .../stop and reloads the page so the room list (state, current talk)
       comes back from the server rather than being reconstructed in JS.

   Elements are found by data-* hooks, as in room.js; this page's own visual
   classes come from glosa.css (Task 12 replaces the markup, not this file's
   approach).
*/
(() => {
  "use strict";

  const loginForm = document.querySelector("[data-admin-login-form]");
  if (loginForm) {
    const error = document.querySelector("[data-admin-login-error]");
    loginForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (error) error.removeAttribute("data-visible");
      let response;
      try {
        response = await fetch(loginForm.action, {
          method: "POST",
          body: new FormData(loginForm),
          redirect: "manual",
        });
      } catch {
        if (error) error.setAttribute("data-visible", "");
        return;
      }
      // Under redirect: "manual", our own 303 to /admin comes back as an
      // opaque response (type "opaqueredirect"); a 401 (wrong password) is
      // a normal, readable response.
      if (response.type === "opaqueredirect" || response.ok) {
        window.location.assign("/admin");
        return;
      }
      if (error) error.setAttribute("data-visible", "");
    });
  }

  const rooms = document.querySelector("[data-rooms]");
  if (!rooms) return;

  rooms.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action !== "start" && action !== "stop") return;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/admin/rooms/${encodeURIComponent(button.dataset.roomId)}/${action}`,
        { method: "POST" }
      );
      if (response.ok) {
        window.location.reload();
        return;
      }
    } finally {
      button.disabled = false;
    }
  });
})();
