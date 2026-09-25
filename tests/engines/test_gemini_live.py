"""glosa.engines._gemini_live: what the two Gemini Live engines share. The
error classification is tested here once, for both (each engine's own test
only checks that its _classify_error uses it)."""

from __future__ import annotations

import pytest
from google.genai import errors
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from glosa.engines._gemini_live import classify_error, duration_s, error_code, vocabulary

SPEND_CAP = (
    "Your project has exceeded its monthly spending cap. Please go to AI Studio at https://ai.studio/spend"
    " to manage your project."  # the rest was cut in the log
)
FREE_TIER_RPM = (
    "You exceeded your current quota, please check your plan and billing details. For more information on"
    " this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits."
)


@pytest.mark.parametrize(
    ("exc", "expected_meta"),
    [
        (errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}), {"code": 429, "retryable": True}),
        (errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}), {"code": 503, "retryable": True}),
        (errors.ClientError(402, {"error": {"code": 402, "message": "Payment required"}}), {"code": 402, "retryable": False, "payment": True}),
        (errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}), {"code": 400, "retryable": False}),
        # Prepaid billing reports exhausted credit as a 429: must still be a payment stop, not a retry loop.
        (
            errors.ClientError(429, {"error": {"code": 429, "message": "Your prepayment credits are depleted."}}),
            {"code": 402, "retryable": False, "payment": True},
        ),
        # A spending cap is a payment stop too, whatever its code: the Live API closes with 1011
        # (seen on every room when the project hit its monthly cap); generate_content says 429.
        (errors.APIError(1011, SPEND_CAP, None), {"code": 402, "retryable": False, "payment": True, "cap": True}),
        (
            errors.ClientError(429, {"error": {"code": 429, "message": SPEND_CAP, "status": "RESOURCE_EXHAUSTED"}}),
            {"code": 402, "retryable": False, "payment": True, "cap": True},
        ),
        # ...but the free tier's per-minute quota ("check your plan and billing details") is a rate limit.
        (
            errors.ClientError(429, {"error": {"code": 429, "message": FREE_TIER_RPM, "status": "RESOURCE_EXHAUSTED"}}),
            {"code": 429, "retryable": True},
        ),
        # The Live API surfaces websocket close frames as APIError(<close code>, <reason>).
        (errors.APIError(1011, "Internal error encountered.", None), {"code": 1011, "retryable": True}),
        (errors.APIError(1007, "Request contains an invalid argument.", None), {"code": 1007, "retryable": False}),
        # Observed at 591 s in the 25-min run (samples/fixtures/lt_en.jsonl): a session kept past its
        # GoAway is killed with 1008. A fresh session works, so it must be retried.
        (
            errors.APIError(
                1008,
                "Connection aborted because the client failed to close the connection after receiving"
                " a GoAway signal once the session durat",
                None,
            ),
            {"code": 1008, "retryable": True},
        ),
        # The server's own close, seen 5 times on sala-estacion (transcribe-live), each time while no
        # audio was being sent (station gone or silence gate closed): a fresh session worked at once.
        (errors.APIError(1008, "The operation was aborted.", None), {"code": 1008, "retryable": True}),
        # Any other 1008 (model not found / not supported for bidiGenerateContent, config rejected)
        # is a hard failure: retrying would just loop.
        (
            errors.APIError(
                1008,
                "models/gemini-x is not found for API version v1beta, or is not supported for"
                " bidiGenerateContent.",
                None,
            ),
            {"code": 1008, "retryable": False},
        ),
        (ConnectionResetError("reset by peer"), {"code": 0, "retryable": True}),
    ],
    ids=["429", "503", "402", "400", "prepaid-429", "cap-1011", "cap-429", "free-tier-429", "ws-1011", "ws-1007", "ws-1008-goaway", "ws-1008-aborted", "ws-1008-other", "network"],
)
def test_classify_error(exc: Exception, expected_meta: dict) -> None:
    ev = classify_error(exc, t_recv=3.0)
    assert ev.kind == "error"
    assert ev.meta == expected_meta
    assert ev.t_recv == 3.0
    assert ev.text  # human-readable reason for the event log


def test_error_code_of_api_errors_websocket_closes_and_the_rest() -> None:
    assert error_code(errors.APIError(1011, "Internal error encountered.", None)) == 1011
    assert error_code(ConnectionClosedError(Close(1011, "boom"), None)) == 1011
    assert error_code(ConnectionClosedError(None, None)) == 1006  # no close frame: abnormal closure
    assert error_code(ConnectionResetError("reset by peer")) == 0


def test_duration_s_parses_protobuf_durations() -> None:
    assert duration_s("50s") == 50.0
    assert duration_s("1.5s") == 1.5
    assert duration_s(None) == 0.0 and duration_s("") == 0.0


def test_vocabulary_strips_drops_blanks_and_dedupes_ignoring_case_keeping_the_first() -> None:
    terms = ["Loki", " ", "Grafana", "loki", "", " AWS ", "GRAFANA", "Straße", "STRASSE"]
    assert vocabulary(terms) == ["Loki", "Grafana", "AWS", "Straße"]


def test_vocabulary_dedupes_before_capping() -> None:
    # 150 entries but only 60 distinct terms once case is ignored: none is cut
    terms = [f"term{i}" for i in range(60)] + [f"TERM{i}" for i in range(60)] + [f"Term{i}" for i in range(30)]
    assert vocabulary(terms, limit=100) == [f"term{i}" for i in range(60)]
    assert vocabulary([f"t{i}" for i in range(130)], limit=100) == [f"t{i}" for i in range(100)]
