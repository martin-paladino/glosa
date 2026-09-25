"""Tests for glosa.web.app's own machinery that isn't create_app() itself:
the log redaction filter and main()'s wiring (Task 14a fix rounds 1-2,
review #2 and #4). create_app()'s room/lifespan/router wiring is covered by
tests/web/test_public_api.py, test_admin_api.py and test_station.py.

Fix round 2: a filter attached only to "uvicorn.access" never saw the
WebSocket accept/reject/response lines -- those are logged on
"uvicorn.error" by WebSocketsSansIOProtocol (the WS protocol uvicorn picks
when `websockets` is installed, which it is here -- see
.venv/.../uvicorn/protocols/websockets/websockets_sansio_impl.py and
protocols/websockets/auto.py), so every station WS (re)connect -- including
our own 4401 key check, which uvicorn logs as its generic "403" line
regardless of the ASGI-level close code -- still wrote the raw `?key=...`
to stdout. `_ws_record` below is shaped exactly like those three call
sites (lines ~440, ~464, ~479 of that file).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from glosa.models import Talk
from glosa.web import app as app_module
from glosa.web.app import RedactStationKeyFilter, _export_talk


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


def _ws_record(msg: str, path: str, status: int | None = None) -> logging.LogRecord:
    """Shaped exactly like uvicorn's WebSocketsSansIOProtocol log calls, on
    "uvicorn.error" (not "uvicorn.access"): the query string sits at
    args[1] here, not args[2] as in the HTTP access record above --
    accept and reject (our 4401 case) are 2-arg calls, the
    websocket.http.response.start line is a 3-arg call with the status
    appended."""
    args = ("127.0.0.1:54321", path) if status is None else ("127.0.0.1:54321", path, status)
    return logging.LogRecord(
        name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
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


def test_redact_filter_covers_a_pre_formatted_msg_defensively() -> None:
    """No current uvicorn call site pre-formats (all three WS lines and the
    access line format lazily from args -- confirmed by reading the
    source), but the filter covers `record.msg` too in case a future
    version does; harmless either way, since the pattern only matches
    `key=...`."""
    record = logging.LogRecord(
        name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
        msg="already formatted: /station/main-stage?key=abc123 done", args=(), exc_info=None,
    )

    RedactStationKeyFilter().filter(record)

    assert record.msg == "already formatted: /station/main-stage?key=REDACTED done"


# ---- fix round 2: the WebSocket lines, on "uvicorn.error" ----------------------


def test_redact_filter_covers_the_ws_accept_line() -> None:
    """websockets_sansio_impl.py ~line 440: logger.info('%s - "WebSocket %s"
    [accepted]', client_addr, full_path)."""
    record = _ws_record('%s - "WebSocket %s" [accepted]', "/ws/station/main-stage?key=abc123def456")

    RedactStationKeyFilter().filter(record)

    assert record.args[1] == "/ws/station/main-stage?key=REDACTED"
    assert record.getMessage() == '127.0.0.1:54321 - "WebSocket /ws/station/main-stage?key=REDACTED" [accepted]'


def test_redact_filter_covers_the_ws_reject_line() -> None:
    """~line 464: logger.info('%s - "WebSocket %s" 403', client_addr,
    full_path) -- uvicorn logs this generic "403" line for every
    websocket.close sent before accept(), which is exactly how our own
    4401 key check rejects a station (Starlette's WebSocket.close()
    before accept() sends a "websocket.close" ASGI message, not a
    "websocket.http.response.start" one -- the ASGI-level 4401 code
    doesn't change what uvicorn logs here)."""
    record = _ws_record('%s - "WebSocket %s" 403', "/ws/station/main-stage?key=abc123def456")

    RedactStationKeyFilter().filter(record)

    assert record.args[1] == "/ws/station/main-stage?key=REDACTED"
    assert record.getMessage() == '127.0.0.1:54321 - "WebSocket /ws/station/main-stage?key=REDACTED" 403'


def test_redact_filter_covers_the_ws_http_response_line() -> None:
    """~line 479: logger.info('%s - "WebSocket %s" %d', client_addr,
    full_path, status) -- a websocket.http.response.start (the Denial
    Response extension path; not one Glosa uses today, but the same
    logger call shape)."""
    record = _ws_record('%s - "WebSocket %s" %d', "/ws/station/main-stage?key=abc123def456", status=403)

    RedactStationKeyFilter().filter(record)

    assert record.args[1] == "/ws/station/main-stage?key=REDACTED"
    assert record.getMessage() == '127.0.0.1:54321 - "WebSocket /ws/station/main-stage?key=REDACTED" 403'


# ---- main(): installs the filter, bounds the WS frame size --------------------


def test_main_installs_the_redact_filter_and_bounds_ws_frame_size(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_run(app_path, **kwargs):
        calls["app_path"] = app_path
        calls["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)
    access_logger = logging.getLogger("uvicorn.access")
    error_logger = logging.getLogger("uvicorn.error")
    before_access = list(access_logger.filters)
    before_error = list(error_logger.filters)
    try:
        app_module.main()

        assert calls["app_path"] == "glosa.web.app:app_from_env"
        assert calls["kwargs"]["ws_max_size"] == 65536
        # Fix round 2: both loggers, not just uvicorn.access -- the WS
        # accept/reject/response lines are logged on uvicorn.error.
        assert any(isinstance(f, RedactStationKeyFilter) for f in access_logger.filters)
        assert any(isinstance(f, RedactStationKeyFilter) for f in error_logger.filters)
    finally:
        # Both are global singletons: don't leak filters into other tests.
        access_logger.filters = before_access
        error_logger.filters = before_error


# ---------------------------------------------------- _export_talk (task-11r-brief.md item 5 / decision 1)


class _FakeAdminEvents:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, kind: str, data: dict) -> None:
        self.published.append((kind, data))


def _talk(talk_id: str, *, language: str = "en", targets: tuple[str, ...] = ("es",)) -> Talk:
    start = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
    return Talk(
        id=talk_id, room_id="r1", title="A Talk", speakers=[], language=language, targets=list(targets),
        engine="fast", start=start, end=start + timedelta(minutes=20), abstract="", tags=[], glossary=[],
        status="done", actual_start=start, actual_end=start + timedelta(minutes=20),
    )


async def test_export_talk_skips_a_free_session_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_build_corrected(talk_id, lang, *, db, api_key, model="gemini-3.8-flash"):
        calls.append((talk_id, lang))
        return "ready"

    monkeypatch.setattr(app_module, "build_corrected", fake_build_corrected)
    events = _FakeAdminEvents()
    talk = _talk("free-r1-20260924T100000")

    await _export_talk(talk, db=object(), settings=_FakeSettings(), admin_events=events)

    assert calls == []
    assert events.published == []


async def test_export_talk_builds_every_target_language_but_not_the_source_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_build_corrected(talk_id, lang, *, db, api_key, model="gemini-3.8-flash"):
        calls.append((talk_id, lang))
        return "ready" if lang == "es" else "failed"

    monkeypatch.setattr(app_module, "build_corrected", fake_build_corrected)
    events = _FakeAdminEvents()
    talk = _talk("t1", language="en", targets=("en", "es", "pt"))  # "en" target == source: skipped
    db = _StatusDb()

    await _export_talk(talk, db=db, settings=_FakeSettings(), admin_events=events)

    assert sorted(calls) == [("t1", "es"), ("t1", "pt")]
    assert db.statuses == [("t1", "es", "pending"), ("t1", "pt", "pending")]  # I4: all queued first
    assert sorted(events.published) == [
        ("export_failed", {"talk_id": "t1", "room_id": "r1", "lang": "pt"}),
        ("export_ready", {"talk_id": "t1", "room_id": "r1", "lang": "es"}),
    ]


class _FakeSettings:
    gemini_api_key = "unused"


class _StatusDb:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, str]] = []

    async def set_export_status(self, talk_id: str, lang: str, status: str) -> None:
        self.statuses.append((talk_id, lang, status))


# No TestClient-based integration test for the WS 4401 case: Starlette's
# WebSocketTestSession (starlette/testclient.py) calls the ASGI app
# callable directly through an in-process portal -- it never opens a real
# socket, so uvicorn's own protocol classes (including
# WebSocketsSansIOProtocol, where this logging actually happens) are never
# instantiated at all. There is nothing for a "uvicorn.error" log-capture
# assertion to observe in that path; the three tests above (exercising the
# filter against records shaped exactly like uvicorn's real calls) and the
# manual, real-server verification already done for this task are the
# coverage available here.


# ---------------------------------------------------- shutdown (final-review-A I1)


async def test_shutdown_acloses_every_room_worker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RoomWorker.stop() runs between talks and keeps the Jev meter's HTTP
    client; the lifespan's shutdown calls RoomWorker.aclose(), which also
    closes it."""
    from glosa.config import RoomCfg, Settings
    from glosa.room import RoomWorker
    from glosa.web.app import create_app

    closed: list[str] = []

    async def fake_aclose(self) -> None:
        closed.append(self.room.id)

    monkeypatch.setattr(RoomWorker, "aclose", fake_aclose)
    settings = Settings(
        gemini_api_key="unused", admin_password="test-password", db_path=str(tmp_path / "glosa.db"),
        rooms=[
            RoomCfg(id="r1", name="Uno", source_type="file", source_url=None, default_targets=["es"]),
            RoomCfg(id="r2", name="Dos", source_type="file", source_url=None, default_targets=["es"]),
        ],
    )
    app = create_app(settings, autopilot_interval_s=3600)
    async with app.router.lifespan_context(app):
        assert closed == []

    assert sorted(closed) == ["r1", "r2"]


async def test_shutdown_stops_the_shared_mlx_threads_within_the_hook_grace(tmp_path, monkeypatch) -> None:
    """task-16-review.md Important #1: the lifespan shuts the local mode's
    shared MLX executors down, bounded by HOOK_GRACE_S."""
    from glosa.config import RoomCfg, Settings
    from glosa.engines import local as local_engine
    from glosa.text import local_translator
    from glosa.web.app import HOOK_GRACE_S, create_app

    calls: list[tuple[str, float]] = []

    def recorder(name: str):
        async def shutdown_shared_model(timeout: float) -> None:
            calls.append((name, timeout))

        return shutdown_shared_model

    monkeypatch.setattr(local_engine, "shutdown_shared_model", recorder("parakeet"))
    monkeypatch.setattr(local_translator, "shutdown_shared_model", recorder("translategemma"))
    settings = Settings(
        gemini_api_key="unused", admin_password="test-password", db_path=str(tmp_path / "glosa.db"),
        rooms=[RoomCfg(id="r1", name="Uno", source_type="file", source_url=None, default_targets=["es"])],
    )
    app = create_app(settings, autopilot_interval_s=3600)
    async with app.router.lifespan_context(app):
        assert calls == []

    assert sorted(calls) == [("parakeet", HOOK_GRACE_S), ("translategemma", HOOK_GRACE_S)]
