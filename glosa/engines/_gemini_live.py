"""What the two Gemini Live engines share: LiveTranslateEngine ("fast",
glosa/engines/live_translate.py) and TranscribeLiveEngine ("glossary",
glosa/engines/transcribe.py). Both speak the same Live API websocket, so
they send the same audio format and fail the same ways.

Error policy (``classify_error``), the same for both engines and relied on
by the relay (glosa/engines/relay.py):

- 402, or a message with a payment hint (prepaid billing reports exhausted
  credit as a 429 RESOURCE_EXHAUSTED): ``{"code": 402, "retryable": False,
  "payment": True}``, a stop, never a retry loop;
- 429, 503 and any 5xx: retryable;
- websocket 1008 whose reason mentions GoAway: retryable (the server kills a
  session kept past its GoAway, observed at 591 s in the T0.5 run; a fresh
  session works), and 1008 "The operation was aborted." (seen 5 times on
  transcribe-live, each while no audio reached the session -- station gone,
  silence gate closed; a new session worked at once);
- any other 4xx, 1007 (invalid argument) and 1008 (model not found, config
  rejected): hard, retrying would only loop;
- anything else (1011 internal error, 1006 abnormal closure, network, 0 =
  unknown): retryable.
"""

from __future__ import annotations

from collections.abc import Iterable

from google.genai import errors
from websockets.exceptions import ConnectionClosed

from glosa.models import EngineEvent

AUDIO_MIME = "audio/pcm;rate=16000"

NORMAL_CLOSE = 1000
# Websocket close codes that won't get better by retrying: 1007 invalid
# argument, 1008 policy violation (model not found / not supported for
# bidiGenerateContent, config rejected). Exception: see GOAWAY_ABORT.
NON_RETRYABLE_WS = frozenset({1007, 1008})
GOAWAY_ABORT = 1008
RETRYABLE_1008_HINTS = ("goaway", "operation was aborted")  # a 1008 a fresh session gets past
PAYMENT_HINTS = ("prepayment", "credits are depleted", "payment required", "payment_required")


def error_code(exc: BaseException) -> int:
    """The HTTP status or websocket close code of ``exc``; 0 if it has none."""
    if isinstance(exc, errors.APIError):  # includes websocket closes, see google.genai.live
        return exc.code if isinstance(exc.code, int) else 0
    if isinstance(exc, ConnectionClosed):
        return exc.rcvd.code if exc.rcvd is not None else 1006
    return 0


def error_meta(exc: BaseException) -> dict:
    """``{"code", "retryable"}`` (plus ``"payment": True``) for ``exc``, per
    the policy in the module docstring."""
    code = error_code(exc)
    reason = (str(exc) or exc.__class__.__name__).lower()
    if code == 402 or any(hint in reason for hint in PAYMENT_HINTS):
        return {"code": 402, "retryable": False, "payment": True}
    if code in (429, 503) or 500 <= code < 600:
        return {"code": code, "retryable": True}
    if code == GOAWAY_ABORT and any(hint in reason for hint in RETRYABLE_1008_HINTS):
        return {"code": code, "retryable": True}
    if 400 <= code < 500 or code in NON_RETRYABLE_WS:
        return {"code": code, "retryable": False}
    return {"code": code, "retryable": True}  # 1011, 1006, network, unknown


def classify_error(exc: BaseException, t_recv: float) -> EngineEvent:
    """The ``error`` EngineEvent an engine reports for ``exc``."""
    reason = str(exc) or exc.__class__.__name__
    return EngineEvent(kind="error", text=f"{exc.__class__.__name__}: {reason}", t_recv=t_recv, meta=error_meta(exc))


def duration_s(value: str | None) -> float:
    """Protobuf Duration as JSON ("50s", "1.5s") -> seconds; unknown -> 0."""
    if not value:
        return 0.0
    return float(value.rstrip("s"))


def vocabulary(terms: Iterable[str], limit: int | None = None) -> list[str]:
    """Terms for transcribe-live's ``customVocabulary``: stripped, blanks
    dropped, duplicates dropped ignoring case (``casefold``; the first
    spelling wins), in order, then the first ``limit``. Deduping comes
    first so repeats never push a real term past the cap."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in terms:
        term = raw.strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out if limit is None else out[:limit]
