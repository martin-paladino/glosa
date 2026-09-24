"""Admin authentication: a single shared password (``Settings.admin_password``)
protects ``/admin`` and every ``/api/admin/*`` route.

``itsdangerous`` is not a project dependency, so the session cookie is
signed with stdlib HMAC-SHA256 instead. Its value is ``"<issued_at>.<hmac>"``
(Ruling 36): ``issued_at`` is the unix time the session was created, and the
HMAC key combines two things -- ``ADMIN_PASSWORD`` and a fresh per-process
secret (``new_admin_secret()``, 32 random bytes, created once by
``create_app`` and kept on ``app.state.admin_secret``). Because the process
secret is never persisted, restarting the server invalidates every
outstanding cookie at once, same as changing ``ADMIN_PASSWORD`` -- and
because ``issued_at`` is itself covered by the HMAC, tampering with it (to
extend a session) just breaks the signature. The server enforces the 24h
max age itself, from ``issued_at`` (``verify_session``'s ``now``), not only
via the cookie's own ``Max-Age`` -- a client can't extend its own session by
holding onto an old cookie past that point. (The previous design signed a
static, non-expiring value derived from the password alone: a leaked cookie
never expired, and the cookie was an offline oracle an attacker could grind
against candidate passwords. This fixes both.)

Both the login password and the cookie are compared in constant time
(``hmac.compare_digest``), always on UTF-8-encoded bytes on both sides --
never on ``str``, which raises for non-ASCII input instead of just
comparing unequal.

The cookie is ``HttpOnly`` (no JS access) and ``SameSite=Lax`` (sent on top-
level navigation, not on cross-site requests). ``Secure`` is set whenever
the request looks like HTTPS (``request.url.scheme`` or
``X-Forwarded-Proto``), and left off otherwise so the cookie still works
during local/plain-HTTP development.

Every state-changing ``/api/admin/*`` request also requires a custom
``X-Glosa-Admin: 1`` header (``require_csrf_header``): a classic HTML form
can't set custom headers, so this blocks cross-site form-based CSRF even
though the cookie is ``SameSite=Lax`` rather than ``Strict``.  ``admin.js``
sends it on every fetch; the login/logout forms don't need it (they don't
carry a session yet, or are simply clearing one).

Logging out clears the browser's cookie, but a *copy* of that cookie (taken
before logout) would otherwise still verify -- the token itself doesn't
change. ``app.state.sessions_valid_after`` closes that: logout bumps it to
"now", and ``verify_session`` rejects any token whose ``issued_at`` is
older, so every outstanding session -- not just the one browser that logged
out -- stops working at once (fine for a single shared admin password).

``issued_at`` is parsed with a strict, length-bounded pattern
(``[0-9]{1,12}``), not ``str.isdigit()``: that also accepts non-ASCII digit
characters ``int()`` then rejects, and places no bound on length, so a
crafted cookie could make ``int(issued_at)`` raise (Python's
integer-string-conversion limit) or make the later ``now - int(...)``
subtraction raise ``OverflowError`` converting a huge int to a float --
either would have reached the caller as an unhandled 500.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request

COOKIE_NAME = "glosa_admin"
COOKIE_MAX_AGE_S = 60 * 60 * 24  # 24h, enforced both by the cookie's Max-Age and server-side.
CSRF_HEADER = "x-glosa-admin"
CSRF_HEADER_VALUE = "1"

_SECRET_BYTES = 32
# A unix timestamp never needs more than 12 digits (that's the year 33658);
# bounding it here means `int(issued_at)` can never be huge enough to
# overflow the later float subtraction, and never long enough to hit
# Python's integer-string-conversion limit either -- both of which an
# unbounded `str.isdigit()` check let through (fix round 2, #1).
_ISSUED_AT_RE = re.compile(r"[0-9]{1,12}")


def new_admin_secret() -> bytes:
    """A fresh per-process signing key. ``create_app`` calls this once and
    keeps the result on ``app.state.admin_secret``; never persisted."""
    return secrets.token_bytes(_SECRET_BYTES)


def _session_key(admin_secret: bytes, admin_password: str) -> bytes:
    return hmac.new(admin_secret, admin_password.encode("utf-8"), hashlib.sha256).digest()


def _session_mac(admin_secret: bytes, admin_password: str, issued_at: str) -> str:
    return hmac.new(_session_key(admin_secret, admin_password), issued_at.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_session(admin_secret: bytes, admin_password: str, *, issued_at: float | None = None) -> str:
    """A fresh session token, ``"<issued_at>.<hmac>"``, for a correct
    ``admin_password``."""
    ts = str(int(time.time() if issued_at is None else issued_at))
    return f"{ts}.{_session_mac(admin_secret, admin_password, ts)}"


def verify_session(
    admin_secret: bytes,
    admin_password: str,
    token: str | None,
    *,
    now: float | None = None,
    valid_after: float = 0.0,
) -> bool:
    """Whether ``token`` (a cookie value) is a valid, unexpired session for
    ``admin_password`` signed with ``admin_secret``, issued at or after
    ``valid_after`` (``app.state.sessions_valid_after``: logout bumps this
    to "now", so a copy of an already-logged-out cookie stops working even
    though nothing about the token itself changed)."""
    if not admin_password or not token:
        return False
    issued_at, sep, mac = token.partition(".")
    # A strict, length-bounded pattern -- not str.isdigit(), which also
    # accepts non-ASCII digit characters int() then rejects, and which lets
    # an attacker hand us an arbitrarily long digit string (int() on one
    # over Python's conversion limit raises ValueError; under that limit but
    # still huge, the subtraction below raises OverflowError converting it
    # to a float). Either used to reach the caller as an unhandled 500.
    if not sep or not mac or not _ISSUED_AT_RE.fullmatch(issued_at):
        return False
    try:
        issued_at_s = int(issued_at)
    except (ValueError, OverflowError):
        return False  # defense in depth; _ISSUED_AT_RE should already rule this out
    if issued_at_s < valid_after:
        return False
    now = time.time() if now is None else now
    try:
        age = now - issued_at_s
    except OverflowError:
        return False
    if age < -5 or age > COOKIE_MAX_AGE_S:  # small tolerance for clock skew, not for replay
        return False
    expected = _session_mac(admin_secret, admin_password, issued_at)
    # Always compare bytes: hmac.compare_digest raises for a non-ASCII str,
    # and `mac` comes straight from the (untrusted) cookie.
    return hmac.compare_digest(expected.encode("utf-8"), mac.encode("utf-8"))


def verify_password(candidate: str, admin_password: str) -> bool:
    """Constant-time comparison of a login attempt against ``ADMIN_PASSWORD``."""
    if not admin_password:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), admin_password.encode("utf-8"))


def cookie_is_secure(request: Request) -> bool:
    """Whether ``request`` looks like it arrived over HTTPS, directly or
    behind a reverse proxy that sets ``X-Forwarded-Proto``."""
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def is_authenticated(request: Request) -> bool:
    """Whether ``request`` carries a valid, not-since-logged-out admin
    session cookie."""
    settings = request.app.state.settings
    secret = request.app.state.admin_secret
    valid_after = getattr(request.app.state, "sessions_valid_after", 0.0)
    return verify_session(
        secret, settings.admin_password, request.cookies.get(COOKIE_NAME), valid_after=valid_after
    )


def require_admin(request: Request) -> None:
    """FastAPI dependency for ``/api/admin/*`` routes: 401 without a valid cookie."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="admin login required")


def require_csrf_header(request: Request) -> None:
    """FastAPI dependency for state-changing ``/api/admin/*`` routes: 403
    without the ``X-Glosa-Admin`` header a plain HTML form cannot send."""
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER!r} header")
