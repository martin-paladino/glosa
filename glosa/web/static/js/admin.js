/* Glosa: minimal admin panel (/admin). Plain JS, no build, no dependencies.

   Two independent bits, both progressive enhancement over plain HTML:
     - the login form posts to /admin/login on its own; here we intercept it
       only to show the wrong-password message inline instead of a raw 401
       page;
     - each room's Start/Stop button posts to /api/admin/rooms/{id}/start or
       .../stop (with the X-Glosa-Admin header the server requires as a CSRF
       check -- a plain <form> can't send a custom header, which is exactly
       the point) and reloads the page so the room list (state, current
       talk) comes back from the server rather than being reconstructed in
       JS. A failure (a stale session, a room with no source, a network
       error) is shown inline instead of silently doing nothing.

   Elements are found by data-* hooks, as in room.js; this page's own visual
   classes come from glosa.css (Task 12 replaces the markup, not this file's
   approach).
*/
(() => {
  "use strict";

  const CSRF_HEADER = "X-Glosa-Admin";

  function showNotice(el, message) {
    if (!el) return;
    el.textContent = "";
    el.append(message);
    el.setAttribute("data-visible", "");
  }

  function showSessionExpired(el) {
    if (!el) return;
    el.textContent = "";
    el.append("Your session expired. ");
    const a = document.createElement("a");
    a.href = "/admin/login";
    a.textContent = "Log in again";
    el.append(a);
    el.append(".");
    el.setAttribute("data-visible", "");
  }

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
        showNotice(error, "Could not reach the server. Check your connection and try again.");
        return;
      }
      // Under redirect: "manual", our own 303 to /admin comes back as an
      // opaque response (type "opaqueredirect"); a 401 (wrong password) is
      // a normal, readable response.
      if (response.type === "opaqueredirect" || response.ok) {
        window.location.assign("/admin");
        return;
      }
      showNotice(error, "Wrong password.");
    });
  }

  const rooms = document.querySelector("[data-rooms]");
  if (!rooms) return;
  const roomsError = document.querySelector("[data-rooms-error]");

  rooms.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action !== "start" && action !== "stop") return;
    if (roomsError) roomsError.removeAttribute("data-visible");
    button.disabled = true;
    try {
      let response;
      try {
        response = await fetch(
          `/api/admin/rooms/${encodeURIComponent(button.dataset.roomId)}/${action}`,
          { method: "POST", headers: { [CSRF_HEADER]: "1" } }
        );
      } catch {
        showNotice(roomsError, "Could not reach the server. Check your connection and try again.");
        return;
      }
      if (response.ok) {
        window.location.reload();
        return;
      }
      if (response.status === 401) {
        showSessionExpired(roomsError);
        return;
      }
      let detail = "";
      try {
        detail = (await response.json()).detail || "";
      } catch {
        /* not JSON, fall through to the generic message */
      }
      showNotice(roomsError, detail || `Could not ${action} this room (${response.status}).`);
    } finally {
      button.disabled = false;
    }
  });
})();
