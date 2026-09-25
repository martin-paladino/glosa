"""Tests for glosa.web.app's own machinery that isn't create_app() itself:
the access-log redaction filter and main()'s wiring (Task 14a fix round 1,
review #2 and #4). create_app()'s room/lifespan/router wiring is covered by
tests/web/test_public_api.py, test_admin_api.py and test_station.py.
"""

from __future__ import annotations

import logging

import pytest

from glosa.web import app as app_module
from glosa.web.app import RedactStationKeyFilter


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """Shaped exactly like uvicorn's own access log call (uvicorn's
    protocols.http.*_impl.py): msg is a %-style template, args is a tuple
    (client_addr, method, full_path, http_version, status_code) -- the
    query string, if any, is part of `full_path` (args[2])."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", path, "1.1", status),
        exc_info=None,
    )


# ---- RedactStationKeyFilter ---------------------------------------------------


def test_redact_filter_rewrites_the_key_in_the_query_string() -> None:
    record = _access_record("/station/main-stage?key=abc123def456ghi789")

    assert RedactStationKeyFilter().filter(record) is True  # filters must return True to keep the record
    assert record.args[2] == "/station/main-stage?key=REDACTED"
    assert record.getMessage() == '127.0.0.1:54321 - "GET /station/main-stage?key=REDACTED HTTP/1.1" 200'


def test_redact_filter_covers_the_websocket_upgrade_path_too() -> None:
    record = _access_record("/ws/station/main-stage?key=abc123def456ghi789", status=101)

    RedactStationKeyFilter().filter(record)

    assert record.args[2] == "/ws/station/main-stage?key=REDACTED"


def test_redact_filter_stops_at_the_next_query_param() -> None:
    record = _access_record("/station/main-stage?key=abc123&lang=es")

    RedactStationKeyFilter().filter(record)

    assert record.args[2] == "/station/main-stage?key=REDACTED&lang=es"


def test_redact_filter_leaves_keyless_paths_untouched() -> None:
    record = _access_record("/s/main-stage")

    RedactStationKeyFilter().filter(record)

    assert record.args[2] == "/s/main-stage"


def test_redact_filter_tolerates_a_record_with_no_args() -> None:
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="plain message, no formatting", args=None, exc_info=None,
    )

    assert RedactStationKeyFilter().filter(record) is True  # must not raise


# ---- main(): installs the filter, bounds the WS frame size --------------------


def test_main_installs_the_redact_filter_and_bounds_ws_frame_size(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_run(app_path, **kwargs):
        calls["app_path"] = app_path
        calls["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)
    access_logger = logging.getLogger("uvicorn.access")
    before = list(access_logger.filters)
    try:
        app_module.main()

        assert calls["app_path"] == "glosa.web.app:app_from_env"
        assert calls["kwargs"]["ws_max_size"] == 65536
        assert any(isinstance(f, RedactStationKeyFilter) for f in access_logger.filters)
    finally:
        access_logger.filters = before  # uvicorn.access is a global singleton: don't leak into other tests
