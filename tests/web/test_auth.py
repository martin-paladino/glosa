"""Tests for glosa.web.auth: the admin login and the require_admin dependency.

7.1: with no cookie, the admin page redirects to the login form; a wrong
password is a 401; the right password sets a signed session cookie and the
admin page renders. The cookie is HMAC-SHA256 (stdlib: itsdangerous is not
installed) over a key derived from Settings.admin_password, HttpOnly and
SameSite=Lax. Both the login password and the cookie are compared in
constant time.

Cookies are set on the TestClient's own cookie jar (``client.cookies.set``)
rather than passed per-request: httpx deprecates (and ``-W error`` then
fails on) a per-request ``cookies=`` argument.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from glosa.config import Settings
from glosa.web import admin_api
from glosa.web.auth import (
    COOKIE_NAME,
    require_admin,
    sign_session,
    verify_password,
    verify_session,
)

STATIC_DIR = Path(admin_api.__file__).parent / "static"
ADMIN_PASSWORD = "s3cr3t-pw"


def _settings(**overrides) -> Settings:
    values: dict = dict(gemini_api_key="unused", admin_password=ADMIN_PASSWORD)
    values.update(overrides)
    return Settings(**values)


def _make_app(settings: Settings | None = None, workers: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings if settings is not None else _settings()
    app.state.workers = workers if workers is not None else {}
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_api.router)
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
    assert client.cookies.get(COOKIE_NAME)


def test_admin_cookie_is_httponly_and_samesite_lax(client: TestClient) -> None:
    response = client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()


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


# ---- signing helpers -------------------------------------------------------


def test_sign_and_verify_session_round_trip() -> None:
    token = sign_session(ADMIN_PASSWORD)

    assert verify_session(ADMIN_PASSWORD, token)
    assert not verify_session(ADMIN_PASSWORD, "tampered")
    assert not verify_session("a-different-password", token)
    assert not verify_session(ADMIN_PASSWORD, None)


def test_verify_password_accepts_only_the_right_one() -> None:
    assert verify_password(ADMIN_PASSWORD, ADMIN_PASSWORD)
    assert not verify_password("wrong", ADMIN_PASSWORD)


# ---- the require_admin dependency, used by /api/admin/* ------------------


def test_require_admin_dependency_needs_a_valid_cookie() -> None:
    app = FastAPI()
    app.state.settings = _settings()

    @app.get("/protected")
    def protected(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    client = TestClient(app)

    assert client.get("/protected").status_code == 401
    client.cookies.set(COOKIE_NAME, "garbage")
    assert client.get("/protected").status_code == 401
    client.cookies.set(COOKIE_NAME, sign_session(ADMIN_PASSWORD))
    ok = client.get("/protected")
    assert ok.status_code == 200 and ok.json() == {"ok": True}
