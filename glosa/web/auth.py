"""Admin authentication: a single shared password (``Settings.admin_password``)
protects ``/admin`` and every ``/api/admin/*`` route.

``itsdangerous`` is not a project dependency, so the session cookie is
signed with stdlib HMAC-SHA256 instead (addendum, Task 7): its value is
``hmac.new(key, b"admin-session", sha256).hexdigest()``, where ``key`` is
derived from ``ADMIN_PASSWORD`` with SHA-256. There is no separate secret
store and no expiry beyond the cookie's own ``Max-Age``: changing
``ADMIN_PASSWORD`` (which needs a restart to take effect) invalidates every
outstanding cookie at once, which is enough for a single-event admin panel.

Both the login password and the cookie's value are compared in constant
time (``hmac.compare_digest``) so neither can be recovered by timing.

The cookie is ``HttpOnly`` (no JS access) and ``SameSite=Lax`` (sent on
top-level navigation, not on cross-site requests), and unset (``secure``
left off) because the MVP is meant to run behind whatever TLS termination
the deployer puts in front of it, not to force HTTPS itself.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

COOKIE_NAME = "glosa_admin"
COOKIE_MAX_AGE_S = 60 * 60 * 24  # 24h; a restart or a changed password invalidates it anyway.

_SESSION_MESSAGE = b"admin-session"


def _cookie_key(admin_password: str) -> bytes:
    return hashlib.sha256(f"glosa-admin-cookie:{admin_password}".encode("utf-8")).digest()


def sign_session(admin_password: str) -> str:
    """The signed cookie value for a correct ``admin_password``."""
    return hmac.new(_cookie_key(admin_password), _SESSION_MESSAGE, hashlib.sha256).hexdigest()


def verify_session(admin_password: str, token: str | None) -> bool:
    """Whether ``token`` (a cookie value) is a valid session for ``admin_password``."""
    if not token:
        return False
    expected = sign_session(admin_password)
    return hmac.compare_digest(expected, token)


def verify_password(candidate: str, admin_password: str) -> bool:
    """Constant-time comparison of a login attempt against ``ADMIN_PASSWORD``."""
    return hmac.compare_digest(candidate.encode("utf-8"), admin_password.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    """Whether ``request`` carries a valid admin session cookie."""
    settings = request.app.state.settings
    return verify_session(settings.admin_password, request.cookies.get(COOKIE_NAME))


def require_admin(request: Request) -> None:
    """FastAPI dependency for ``/api/admin/*`` routes: 401 without a valid cookie."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="admin login required")
