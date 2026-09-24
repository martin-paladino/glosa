"""Tests for glosa.web.auth: the admin login, session cookie and the
require_admin/require_csrf_header dependencies.

7.1 + fix round 1 (Ruling 35, Ruling 36):
  - with no cookie, the admin page redirects to the login form; a wrong
    password is a 401; the right password sets a signed session cookie and
    the admin page renders;
  - the session cookie is ``<issued_at>.<hmac>``, keyed by a per-process
    secret (``glosa.web.auth.new_admin_secret()``, kept on
    ``app.state.admin_secret``) combined with ``Settings.admin_password``,
    so a restart or a password change invalidates every outstanding
    session, and the cookie itself is not an offline oracle for the
    password. The server enforces the 24h max age itself, from
    ``issued_at``, not just via the cookie's own ``Max-Age``;
  - both the login password and the cookie are compared in constant time,
    on bytes (a non-ASCII cookie must be rejected, not raise).

Cookies are set on the TestClient's own cookie jar (``client.cookies.set``)
rather than passed per-request: httpx deprecates (and ``-W error`` then
fails on) a per-request ``cookies=`` argument.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.config import Settings
from glosa.web import admin_api
from glosa.web.auth import (
    COOKIE_NAME,
    new_admin_secret,
    require_admin,
    require_csrf_header,
    sign_session,
    verify_password,
    verify_session,
)

STATIC_DIR = Path(admin_api.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-pw"
CSRF = {"X-Glosa-Admin": "1"}


def _settings(**overrides) -> Settings:
    values: dict = dict(gemini_api_key="unused", admin_password=ADMIN_PASSWORD)
    values.update(overrides)
    return Settings(**values)


def _make_app(settings: Settings | None = None, workers: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {}
    app.state.admin_secret = new_admin_secret()
    app.state.sessions_valid_after = 0.0
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_api.router)
    app.include_router(admin_api.api_router)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), follow_redirects=False)


# ---- GET /admin, GET/POST /admin/login ----------------------------------


def test_admin_without_a_cookie_redirects_to_login(client: TestClient) -> None:
    response = client.get("/admin")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/admin/login"


def test_login_page_shows_a_password_form(client: TestClient) -> None:
    response = client.get("/admin/login")

    assert response.status_code == 200
    html = response.text
    assert '<form' in html and 'action="/admin/login"' in html
    assert 'name="password"' in html
    assert 'type="password"' in html


def test_login_page_loads_admin_js_too(client: TestClient) -> None:
    # Fix round 1, #4: admin.js's inline "wrong password" message only
    # works if the script is loaded on the logged-out page too.
    response = client.get("/admin/login")

    assert 'src="/static/js/admin.js"' in response.text


def test_login_with_the_wrong_password_is_401(client: TestClient) -> None:
    response = client.post("/admin/login", data={"password": "not-it"})

    assert response.status_code == 401
    assert COOKIE_NAME not in client.cookies


def test_login_with_the_right_password_sets_a_cookie_and_redirects_to_admin(
    client: TestClient,
) -> None:
    response = client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    token = client.cookies.get(COOKIE_NAME)
    assert token
    # <issued_at>.<hmac>, per Ruling 36 -- not the old bare-hmac format.
    issued_at, _, mac = token.partition(".")
    assert issued_at.isdigit()
    assert len(mac) == 64  # sha256 hex digest


def test_admin_cookie_is_httponly_and_samesite_lax(client: TestClient) -> None:
    response = client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


def test_admin_cookie_is_not_secure_over_plain_http(client: TestClient) -> None:
    response = client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    assert "secure" not in response.headers["set-cookie"].lower()


def test_admin_cookie_is_secure_behind_a_forwarded_https_proxy(client: TestClient) -> None:
    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD}, headers={"X-Forwarded-Proto": "https"}
    )

    assert "secure" in response.headers["set-cookie"].lower()


def test_admin_with_a_valid_cookie_shows_the_admin_page(client: TestClient) -> None:
    client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    response = client.get("/admin")

    assert response.status_code == 200
    assert "data-admin" in response.text


def test_admin_with_a_bad_cookie_redirects_to_login(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "tampered")

    response = client.get("/admin")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/admin/login"


def test_login_page_redirects_to_admin_when_already_authenticated(
    client: TestClient,
) -> None:
    client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    response = client.get("/admin/login")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/admin"


def test_logout_clears_the_cookie(client: TestClient) -> None:
    client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    response = client.post("/admin/logout")

    assert response.status_code in (302, 303, 307)
    admin = client.get("/admin")
    assert admin.status_code in (302, 303, 307)
    assert admin.headers["location"] == "/admin/login"


def test_logout_invalidates_a_copy_of_the_token_too(client: TestClient) -> None:
    # Fix round 2, #4: logout must invalidate every outstanding session for
    # the one shared ADMIN_PASSWORD, not just delete this browser's cookie
    # -- a copy taken before logout (a different browser, a saved bookmark
    # with the cookie baked in, whatever) must stop working too.
    client.post("/admin/login", data={"password": ADMIN_PASSWORD})
    stolen_token = client.cookies.get(COOKIE_NAME)
    assert stolen_token

    client.post("/admin/logout")

    other_client = TestClient(client.app, follow_redirects=False)
    other_client.cookies.set(COOKIE_NAME, stolen_token)
    response = other_client.get("/admin")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/admin/login"


# ---- signing helpers (Ruling 36: <issued_at>.<hmac>, per-process secret) --


def test_sign_and_verify_session_round_trip() -> None:
    secret = new_admin_secret()
    token = sign_session(secret, ADMIN_PASSWORD)

    assert verify_session(secret, ADMIN_PASSWORD, token)
    assert not verify_session(secret, ADMIN_PASSWORD, "tampered")
    assert not verify_session(secret, "a-different-password", token)
    assert not verify_session(secret, ADMIN_PASSWORD, None)


def test_verify_session_rejects_a_token_signed_with_a_different_process_secret() -> None:
    token = sign_session(new_admin_secret(), ADMIN_PASSWORD)

    assert not verify_session(new_admin_secret(), ADMIN_PASSWORD, token)


def test_verify_session_rejects_an_expired_token() -> None:
    secret = new_admin_secret()
    issued_at = 1_000_000.0
    token = sign_session(secret, ADMIN_PASSWORD, issued_at=issued_at)

    just_in_time = verify_session(secret, ADMIN_PASSWORD, token, now=issued_at + 24 * 3600 - 1)
    too_late = verify_session(secret, ADMIN_PASSWORD, token, now=issued_at + 24 * 3600 + 1)

    assert just_in_time
    assert not too_late


def test_verify_session_rejects_a_tampered_issued_at() -> None:
    secret = new_admin_secret()
    token = sign_session(secret, ADMIN_PASSWORD, issued_at=1_000_000.0)
    issued_at, _, mac = token.partition(".")
    forged = f"{int(issued_at) + 3600}.{mac}"  # push the expiry out without the key

    assert not verify_session(secret, ADMIN_PASSWORD, forged, now=1_000_000.0)


def test_verify_session_rejects_a_non_ascii_cookie_without_raising() -> None:
    secret = new_admin_secret()

    assert not verify_session(secret, ADMIN_PASSWORD, "1000.ééé")


# ---- fix round 2, #1: a crafted issued_at must never crash verify_session --


def test_verify_session_rejects_a_400_digit_issued_at_without_crashing() -> None:
    # now - int(issued_at) would OverflowError (int too large to convert to
    # float) with the old bare isdigit() check.
    secret = new_admin_secret()

    assert not verify_session(secret, ADMIN_PASSWORD, f"{'1' + '0' * 400}.deadbeef")


def test_verify_session_rejects_an_issued_at_past_the_int_string_limit() -> None:
    # int() on a 5000-digit string raises ValueError on its own (Python's
    # integer-string conversion limit), independent of the subtraction.
    secret = new_admin_secret()

    assert not verify_session(secret, ADMIN_PASSWORD, f"{'9' * 5000}.deadbeef")


def test_verify_session_rejects_a_unicode_digit_issued_at() -> None:
    # "²" (superscript two) passes str.isdigit() but int() rejects it.
    secret = new_admin_secret()

    assert not verify_session(secret, ADMIN_PASSWORD, "².deadbeef")


def test_verify_session_rejects_an_empty_issued_at() -> None:
    secret = new_admin_secret()

    assert not verify_session(secret, ADMIN_PASSWORD, ".deadbeef")


@pytest.mark.parametrize(
    "bad_issued_at", ["1" + "0" * 400, "9" * 5000, ""], ids=["400-digit", "5000-digit", "empty"]
)
def test_admin_with_a_crafted_cookie_redirects_not_crashes(client: TestClient, bad_issued_at: str) -> None:
    # The unicode-digit case (test_verify_session_rejects_a_unicode_digit_issued_at
    # above) isn't repeated here: it's a valid attack over a raw HTTP socket,
    # but httpx's own cookie jar refuses to even construct a non-ASCII
    # Cookie header, so there's no way to reach it through this client.
    client.cookies.set(COOKIE_NAME, f"{bad_issued_at}.deadbeef")

    response = client.get("/admin")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/admin/login"


def test_verify_password_accepts_only_the_right_one() -> None:
    assert verify_password(ADMIN_PASSWORD, ADMIN_PASSWORD)
    assert not verify_password("wrong", ADMIN_PASSWORD)


def test_verify_password_and_session_reject_everything_when_admin_password_is_blank() -> None:
    # Defense in depth for Ruling 35: Settings itself refuses to boot with a
    # blank admin_password, but these must never authenticate one either.
    secret = new_admin_secret()

    assert not verify_password("", "")
    assert not verify_password("anything", "")
    assert not verify_session(secret, "", sign_session(secret, ADMIN_PASSWORD))


# ---- the require_admin dependency, used by /api/admin/* ------------------


def test_require_admin_dependency_needs_a_valid_cookie() -> None:
    app = FastAPI()
    app.state.settings = _settings()
    app.state.admin_secret = new_admin_secret()

    @app.get("/protected")
    def protected(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/protected").status_code == 401
    client.cookies.set(COOKIE_NAME, "garbage")
    assert client.get("/protected").status_code == 401
    good = sign_session(app.state.admin_secret, ADMIN_PASSWORD)
    client.cookies.set(COOKIE_NAME, good)
    ok = client.get("/protected")
    assert ok.status_code == 200 and ok.json() == {"ok": True}


def test_a_router_built_with_both_dependencies_protects_any_route_on_it() -> None:
    # Fix round 2, #3: admin_api.api_router is built exactly this way
    # (APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin),
    # Depends(require_csrf_header)])) so any future route added to it is
    # covered automatically. This rebuilds the same pattern independently,
    # on a dummy route, rather than adding one to the real router.
    dummy_router = APIRouter(
        prefix="/api/admin", dependencies=[Depends(require_admin), Depends(require_csrf_header)]
    )

    @dummy_router.post("/dummy")
    def dummy() -> dict:
        return {"ok": True}

    app = FastAPI()
    app.state.settings = _settings()
    app.state.admin_secret = new_admin_secret()
    app.state.sessions_valid_after = 0.0
    app.include_router(dummy_router)
    client = TestClient(app)

    no_cookie = client.post("/api/admin/dummy", headers=CSRF)
    assert no_cookie.status_code == 401  # require_admin first

    client.cookies.set(COOKIE_NAME, sign_session(app.state.admin_secret, ADMIN_PASSWORD))
    no_csrf = client.post("/api/admin/dummy")
    assert no_csrf.status_code == 403  # require_csrf_header second

    ok = client.post("/api/admin/dummy", headers=CSRF)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
