"""TranscribeLiveEngine (the "glossary" engine's speech-to-text half): message
mapping, vocabulary, cost, error classification and the session loop are
tested offline (real server message shapes + a fake client); one `live` test
talks to the real API (case 10.6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types

from glosa.audio.vad import EnergyVad
from glosa.clock import FakeClock, RealClock
from glosa.config import Settings
from glosa.engines.fake import FakeEngine
from glosa.engines.transcribe import MAX_VOCABULARY, MODEL, TranscribeLiveEngine
from glosa.models import AudioChunk, EngineConfig, EngineEvent

ROOT = Path(__file__).resolve().parent.parent.parent
TR_FIXTURE = ROOT / "samples" / "fixtures" / "tr_es.jsonl"

VOCAB = ["Kubernetes", "control plane", "namespaces", "labels", "workloads", "Grafana", "Loki", "AWS", "GCP", "Azure"]
CFG = EngineConfig(kind="glossary", source_lang="es", target_lang=None, vocabulary=VOCAB)
PRICE_PER_MIN = 0.009
PCM_1S = b"\x00\x00" * 16000  # 1 s of PCM16 mono @ 16 kHz

# Wire shapes as gemini-3.5-transcribe-live sent them (probe of samples/es_clip.opus,
# audio_stream_end after each EnergyVad pause). Interim text is cumulative per
# segment; the final carries the whole segment again, ~0.45 s after audio_stream_end.
INTERIM_1 = {"serverContent": {"interimInputTranscription": {"text": "Por cierto,"}}}
INTERIM_2 = {"serverContent": {"interimInputTranscription": {"text": "Por cierto, cuando ustedes"}}}
INTERIM_3 = {"serverContent": {"interimInputTranscription": {"text": "Por cierto, cuando ustedes reciben la"}}}
FINAL_1 = {"serverContent": {"inputTranscription": {"text": "Por cierto, cuando ustedes reciben la factura."}}}
GEN_DONE = {"serverContent": {"generationComplete": True}}
ACTIVITY_END = {"serverContent": {}, "voiceActivity": {"voiceActivityType": "ACTIVITY_END", "audioOffset": "8.400s"}}
ACTIVITY_START = {"serverContent": {}, "voiceActivity": {"voiceActivityType": "ACTIVITY_START", "audioOffset": "8.720s"}}
INTERIM_NEXT = {"serverContent": {"interimInputTranscription": {"text": "En nodos"}}}
FINAL_NEXT = {"serverContent": {"inputTranscription": {"text": "En nodos tenés tantos miles de dólares gastados."}}}


def _engine(
    clock: FakeClock | None = None,
    client: object | None = None,
    cfg: EngineConfig = CFG,
    price_per_min: float = PRICE_PER_MIN,
) -> TranscribeLiveEngine:
    return TranscribeLiveEngine(cfg, "test-key", clock or FakeClock(start=7.0), price_per_min=price_per_min, client=client)


def _event_tuple(ev: EngineEvent) -> tuple:
    return (ev.kind, ev.text, ev.lang, ev.meta)


def _map_all(engine: TranscribeLiveEngine, raws: list[dict]) -> list[EngineEvent]:
    return [ev for raw in raws for ev in engine._map_message(raw)]


# ------------------------------------------------------------------ mapping (10.3)


def test_interims_rewrite_the_open_segment_and_finals_close_it() -> None:
    engine = _engine(FakeClock(start=7.0))

    events = _map_all(
        engine,
        [INTERIM_1, INTERIM_2, INTERIM_3, FINAL_1, GEN_DONE, ACTIVITY_END, ACTIVITY_START, INTERIM_NEXT, FINAL_NEXT],
    )

    assert [_event_tuple(ev) for ev in events] == [
        ("source_delta", "Por cierto,", "es", {"interim": True}),
        ("source_delta", "Por cierto, cuando ustedes", "es", {"interim": True}),
        ("source_delta", "Por cierto, cuando ustedes reciben la", "es", {"interim": True}),
        ("source_final", "Por cierto, cuando ustedes reciben la factura.", "es", {}),
        ("source_delta", "En nodos", "es", {"interim": True}),
        ("source_final", "En nodos tenés tantos miles de dólares gastados.", "es", {}),
    ]
    assert all(ev.t_recv == 7.0 for ev in events)

    # What a caption view does with them: an interim replaces the open line's
    # text (it is the whole segment so far, not a delta); a final closes it.
    closed: list[str] = []
    open_line: str | None = None
    for ev in events:
        if ev.kind == "source_delta":
            open_line = ev.text
        else:
            closed.append(ev.text)
            open_line = None
    assert closed == [
        "Por cierto, cuando ustedes reciben la factura.",
        "En nodos tenés tantos miles de dólares gastados.",
    ]
    assert open_line is None


def test_map_message_accepts_sdk_message_objects() -> None:
    [ev] = _engine()._map_message(types.LiveServerMessage.model_validate(INTERIM_2))
    assert _event_tuple(ev) == ("source_delta", "Por cierto, cuando ustedes", "es", {"interim": True})


def test_a_repeated_interim_is_not_emitted_again() -> None:
    # The server re-sends an unchanged interim (e.g. "Por cierto," twice, 0.5 s
    # apart). It is no progress: it must not look like output to the stall watchdog.
    engine = _engine()
    events = _map_all(engine, [INTERIM_1, INTERIM_1, INTERIM_2, FINAL_1, INTERIM_1])

    assert [(ev.kind, ev.text) for ev in events] == [
        ("source_delta", "Por cierto,"),
        ("source_delta", "Por cierto, cuando ustedes"),
        ("source_final", "Por cierto, cuando ustedes reciben la factura."),
        ("source_delta", "Por cierto,"),  # a new segment that happens to start the same way
    ]


def test_an_empty_final_closes_an_open_segment_and_is_skipped_otherwise() -> None:
    engine = _engine()
    empty_final = {"serverContent": {"inputTranscription": {}}}

    assert engine._map_message(empty_final) == []  # nothing open: nothing to close
    engine._map_message(INTERIM_1)
    [ev] = engine._map_message(empty_final)
    assert (ev.kind, ev.text) == ("source_final", "")  # the open "Por cierto," was not speech after all
    assert engine._map_message(empty_final) == []


def test_language_code_from_the_server_wins_over_the_config() -> None:
    raw = {"serverContent": {"inputTranscription": {"text": "Hello.", "languageCode": "en"}}}
    [ev] = _engine()._map_message(raw)
    assert (ev.kind, ev.lang) == ("source_final", "en")


def test_go_away_maps_time_left() -> None:
    [ev] = _engine(FakeClock(start=3.0))._map_message({"goAway": {"timeLeft": "50s"}})
    assert _event_tuple(ev) == ("go_away", "", None, {"time_left_s": 50.0})
    assert ev.t_recv == 3.0


# ------------------------------------------- recorded session (tr_es.jsonl)


def _fixture_rows() -> list[dict]:
    return [json.loads(line) for line in TR_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def _recorded_events(rows: list[dict]) -> list[tuple]:
    return [
        (r["kind"], r["text"], r["meta"]["lang"], {"interim": True} if r["meta"].get("interim") else {})
        for r in rows
        if r["kind"] in ("source_delta", "source_final")
    ]


def test_recorded_session_interims_rewrite_the_segment_and_finals_close_it() -> None:
    # 60 s of samples/es_clip.opus recorded by the live test below: every server
    # message's wire JSON, replayed through a fresh engine.
    rows = _fixture_rows()
    events = _map_all(_engine(), [r["raw"] for r in rows if "raw" in r])

    assert [_event_tuple(ev) for ev in events] == _recorded_events(rows)
    segments: list[tuple[list[str], str]] = []
    interims: list[str] = []
    for ev in events:
        if ev.kind == "source_delta":
            interims.append(ev.text)
        else:
            segments.append((interims, ev.text))
            interims = []
    assert interims == []  # every segment was closed by its final
    assert len(segments) == 8  # 7 VAD pauses + the flush at the end of the clip
    for interims, final in segments:
        assert interims  # the text shows up before the final
        # Cumulative, not deltas: the last interim already holds (nearly) the whole segment.
        assert len(interims[-1].split()) >= 0.8 * len(final.split())
    transcript = " ".join(final for _, final in segments).lower()
    assert [term for term in VOCAB if term.lower() not in transcript] == []
    assert sum(1 for r in rows if r["kind"] == "interim_repeat") == 10  # dropped, not re-emitted


async def test_fake_engine_replays_the_recorded_session() -> None:
    rows = _fixture_rows()
    cfg = EngineConfig(kind="fake", source_lang="es", target_lang=None, fixture_path=str(TR_FIXTURE))
    engine = FakeEngine(cfg, FakeClock())
    await engine.connect()

    events = [ev async for ev in engine.events()]

    assert [_event_tuple(ev) for ev in events] == _recorded_events(rows) + [("closed", "", None, {})]


# ---------------------------------------------------------------------- config


class FakeSession:
    """Scripted session: receive() yields one turn per call, like the SDK
    (which stops iterating at turn_complete); when turns run out it raises
    `end_exc`, as the SDK does when the websocket closes."""

    def __init__(self, turns: list[list[dict]], end_exc: Exception) -> None:
        self.turns = [[types.LiveServerMessage.model_validate(raw) for raw in turn] for turn in turns]
        self.end_exc = end_exc
        self.sent: list[dict] = []

    async def receive(self):
        if not self.turns:
            raise self.end_exc
        for msg in self.turns.pop(0):
            yield msg

    async def send_realtime_input(self, **kwargs) -> None:
        self.sent.append(kwargs)


class FakeLive:
    def __init__(
        self,
        session: FakeSession | None = None,
        connect_exc: Exception | None = None,
        handshake_gate: asyncio.Event | None = None,
    ) -> None:
        self.session = session
        self.connect_exc = connect_exc
        self.handshake_gate = handshake_gate  # connect() blocks on it, like a slow handshake
        self.handshake_started = asyncio.Event()
        self.calls: list[dict] = []
        self.exited = False

    @contextlib.asynccontextmanager
    async def connect(self, *, model: str, config: types.LiveConnectConfig):
        self.calls.append({"model": model, "config": config})
        self.handshake_started.set()
        if self.handshake_gate is not None:
            await self.handshake_gate.wait()
        if self.connect_exc is not None:
            raise self.connect_exc
        try:
            yield self.session
        finally:
            self.exited = True


def _client(live: FakeLive) -> SimpleNamespace:
    return SimpleNamespace(aio=SimpleNamespace(live=live))


def _normal_close() -> errors.APIError:
    return errors.APIError(1000, "", None)


async def _connected(cfg: EngineConfig = CFG) -> tuple[TranscribeLiveEngine, FakeLive]:
    live = FakeLive(FakeSession([], _normal_close()))
    engine = _engine(client=_client(live), cfg=cfg)
    await engine.connect()
    return engine, live


async def test_connect_requests_transcribe_live_verbatim_with_vocabulary_and_language() -> None:
    _, live = await _connected()

    [call] = live.calls
    cfg: types.LiveConnectConfig = call["config"]
    assert call["model"] == MODEL == "gemini-3.5-transcribe-live"
    tr = cfg.input_audio_transcription
    assert tr is not None
    assert tr.mode == types.AudioTranscriptionConfigMode.VERBATIM  # SMART ignores the vocabulary
    assert tr.language_codes == ["es"]
    assert tr.custom_vocabulary == VOCAB
    assert cfg.output_audio_transcription is None
    assert cfg.translation_config is None


async def test_vocabulary_is_capped_at_100_terms_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    terms = [f"term{i}" for i in range(130)]

    with caplog.at_level(logging.WARNING, logger="glosa.engines.transcribe"):
        _, live = await _connected(EngineConfig(kind="glossary", source_lang="es", target_lang=None, vocabulary=terms))

    assert MAX_VOCABULARY == 100
    assert live.calls[0]["config"].input_audio_transcription.custom_vocabulary == terms[:100]
    assert "130" in caplog.text and "100" in caplog.text


async def test_vocabulary_drops_blanks_and_duplicates_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    terms = ["Loki", " ", "Grafana", "Loki", "", " AWS "]

    with caplog.at_level(logging.WARNING, logger="glosa.engines.transcribe"):
        _, live = await _connected(EngineConfig(kind="glossary", source_lang="es", target_lang=None, vocabulary=terms))

    assert live.calls[0]["config"].input_audio_transcription.custom_vocabulary == ["Loki", "Grafana", "AWS"]
    assert caplog.text == ""


async def test_no_vocabulary_sends_none() -> None:
    _, live = await _connected(EngineConfig(kind="glossary", source_lang="en", target_lang=None))
    tr = live.calls[0]["config"].input_audio_transcription
    assert tr.custom_vocabulary is None
    assert tr.language_codes == ["en"]


# ------------------------------------------------------------ audio and cost


async def test_send_audio_and_end_utterance_sends_audio_stream_end() -> None:
    engine, live = await _connected()
    session = live.session

    await engine.end_utterance()  # no audio yet: nothing to end
    await engine.send_audio(AudioChunk(pcm=b"\x01\x02" * 1600, t=0.0))
    await engine.end_utterance()
    await engine.end_utterance()  # nothing new since the last one
    await engine.send_audio(AudioChunk(pcm=b"\x03\x04" * 1600, t=0.1))
    await engine.end_utterance()

    assert [list(kw) for kw in session.sent] == [["audio"], ["audio_stream_end"], ["audio"], ["audio_stream_end"]]
    blob = session.sent[0]["audio"]
    assert (blob.data, blob.mime_type) == (b"\x01\x02" * 1600, "audio/pcm;rate=16000")
    assert session.sent[1] == {"audio_stream_end": True}


async def test_usd_is_an_increment_priced_per_minute_of_audio_sent() -> None:
    session = FakeSession([[INTERIM_1], [INTERIM_2]], _normal_close())
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()
    stream = engine.events()

    for _ in range(30):  # 30 s of audio
        await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))
    first = await anext(stream)
    second = await anext(stream)
    for _ in range(90):  # 90 s more
        await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))
    closed = await anext(stream)

    assert first.meta == {"interim": True, "usd": pytest.approx(0.5 * PRICE_PER_MIN)}
    assert second.meta == {"interim": True}  # already reported
    assert closed.kind == "closed"
    assert closed.meta == {"usd": pytest.approx(1.5 * PRICE_PER_MIN)}  # the rest rides on `closed`
    assert engine.usd_total == pytest.approx(2 * PRICE_PER_MIN)


async def test_price_per_min_is_configurable_and_defaults_to_0_009() -> None:
    assert TranscribeLiveEngine(CFG, "test-key", FakeClock(), client=object())._price_per_min == 0.009
    session = FakeSession([], _normal_close())
    engine = _engine(client=_client(FakeLive(session)), price_per_min=0.06)
    await engine.connect()
    for _ in range(60):
        await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))
    assert engine.usd_total == pytest.approx(0.06)


# -------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("exc", "expected_meta"),
    [
        (errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}), {"code": 429, "retryable": True}),
        (errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}), {"code": 503, "retryable": True}),
        (errors.ClientError(402, {"error": {"code": 402, "message": "Payment required"}}), {"code": 402, "retryable": False, "payment": True}),
        (errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}), {"code": 400, "retryable": False}),
        (
            errors.ClientError(429, {"error": {"code": 429, "message": "Your prepayment credits are depleted."}}),
            {"code": 402, "retryable": False, "payment": True},
        ),
        (errors.APIError(1011, "Internal error encountered.", None), {"code": 1011, "retryable": True}),
        (errors.APIError(1007, "Request contains an invalid argument.", None), {"code": 1007, "retryable": False}),
        (
            errors.APIError(
                1008,
                "Connection aborted because the client failed to close the connection after receiving"
                " a GoAway signal once the session durat",
                None,
            ),
            {"code": 1008, "retryable": True},
        ),
        (
            errors.APIError(
                1008,
                "models/gemini-x is not found for API version v1beta, or is not supported for bidiGenerateContent.",
                None,
            ),
            {"code": 1008, "retryable": False},
        ),
        (ConnectionResetError("reset by peer"), {"code": 0, "retryable": True}),
    ],
    ids=["429", "503", "402", "400", "prepaid-429", "ws-1011", "ws-1007", "ws-1008-goaway", "ws-1008-other", "network"],
)
def test_classify_error_like_live_translate(exc: Exception, expected_meta: dict) -> None:
    ev = _engine(FakeClock(start=3.0))._classify_error(exc)
    assert ev.kind == "error"
    assert ev.meta == expected_meta
    assert ev.t_recv == 3.0
    assert ev.text


# -------------------------------------------------------------- session loop


async def test_events_keeps_receiving_across_turns_until_the_socket_closes() -> None:
    session = FakeSession([[INTERIM_1, FINAL_1, GEN_DONE], [INTERIM_NEXT]], _normal_close())
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()

    kinds = [ev.kind async for ev in engine.events()]

    assert kinds == ["source_delta", "source_final", "source_delta", "closed"]  # a normal close is not an error


async def test_events_turns_a_session_exception_into_error_then_closed() -> None:
    session = FakeSession([[INTERIM_1]], errors.ClientError(429, {"error": {"code": 429}}))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()
    stream = engine.events()

    first = await anext(stream)
    await engine.send_audio(AudioChunk(pcm=PCM_1S * 2, t=0.0))
    rest = [ev async for ev in stream]

    assert [ev.kind for ev in [first, *rest]] == ["source_delta", "error", "closed"]
    assert rest[0].meta == {"code": 429, "retryable": True, "usd": pytest.approx(2 / 60 * PRICE_PER_MIN)}


async def test_connect_failure_is_reported_through_events() -> None:
    live = FakeLive(connect_exc=errors.ClientError(402, {"error": {"code": 402}}))
    engine = _engine(client=_client(live))

    await engine.connect()  # does not raise: the relay reads the failure from events()
    await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))  # dropped, no session: not billed
    await engine.end_utterance()
    events = [ev async for ev in engine.events()]

    assert [(ev.kind, ev.meta) for ev in events] == [
        ("error", {"code": 402, "retryable": False, "payment": True}),
        ("closed", {}),
    ]
    assert engine.usd_total == 0.0


async def test_close_stops_audio_and_releases_the_connection() -> None:
    engine, live = await _connected()

    await engine.close()
    await engine.close()  # idempotent
    await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))
    await engine.end_utterance()

    assert live.exited
    assert live.session.sent == []
    assert engine.usd_total == 0.0
    assert [ev.kind async for ev in engine.events()] == ["closed"]


async def test_close_during_the_handshake_does_not_leak_the_websocket() -> None:
    session = FakeSession([[INTERIM_1]], _normal_close())
    gate = asyncio.Event()
    live = FakeLive(session, handshake_gate=gate)
    engine = _engine(client=_client(live))

    connecting = asyncio.create_task(engine.connect())
    await live.handshake_started.wait()
    await engine.close()  # the relay retires the engine while the handshake is in flight
    gate.set()
    await asyncio.wait_for(connecting, timeout=1.0)

    assert live.exited  # the just-opened connection was closed again
    await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))
    assert session.sent == []
    assert [ev.kind async for ev in engine.events()] == ["closed"]


async def test_close_while_receiving_ends_with_closed_only() -> None:
    session = FakeSession([[INTERIM_1]], errors.APIError(1006, "Abnormal closure.", None))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()
    stream = engine.events()

    first = await anext(stream)
    await engine.close()
    rest = [ev.kind async for ev in stream]

    assert first.kind == "source_delta"
    assert rest == ["closed"]  # errors caused by our own close() are not reported


async def test_no_audio_is_sent_after_the_session_ended() -> None:
    session = FakeSession([], errors.ClientError(429, {"error": {"code": 429}}))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()
    assert [ev.kind async for ev in engine.events()] == ["error", "closed"]

    await engine.send_audio(AudioChunk(pcm=PCM_1S, t=0.0))  # before the relay gets to close() it

    assert session.sent == []
    assert engine.usd_total == 0.0


# ------------------------------------------------------------- 10.6 live run


ES_CLIP = ROOT / "samples" / "es_clip.opus"
LIVE_SECONDS = 60
PAUSE_S = 0.4  # EnergyVad's pause_ms: a pause fires this long after the speech ended


def _live_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key and (ROOT / ".env").exists():
        key = Settings.load(env_path=str(ROOT / ".env"), config_path=str(ROOT / "config.yaml")).gemini_api_key
    if not key:
        pytest.skip("GEMINI_API_KEY not set")
    return key


def _p(values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _raw_kind(raw: dict) -> str:
    """Record kind for a server message that maps to no EngineEvent."""
    sc = raw.get("serverContent") or {}
    if "voiceActivity" in raw:
        return "voice_activity"
    if sc.get("generationComplete"):
        return "generation_complete"
    if "interimInputTranscription" in sc:
        return "interim_repeat"
    if "usageMetadata" in raw:
        return "usage"
    return "server_other"


_RAW_TYPES = {
    "source_delta": "server_content.interim_input_transcription",
    "source_final": "server_content.input_transcription",
    "go_away": "go_away",
}


def _write_fixture(
    path: Path,
    t0: float,
    audio_start: float,
    received: list[tuple[float, dict, list[EngineEvent]]],
    ends: list[tuple[float, float, float]],
) -> None:
    """FakeEngine JSONL (t = s since connect). Each server message is one
    record carrying its wire JSON in "raw" (tests replay it through the
    engine). Client-side records, skipped by FakeEngine: audio_start, and
    one audio_stream_end per VAD pause (not the flush at the end of the
    clip)."""
    rows: list[dict] = [
        {"t": round(audio_start - t0, 3), "kind": "audio_start", "text": "", "raw_type": "client",
         "meta": {"clip": ES_CLIP.name, "seconds": LIVE_SECONDS, "vocabulary": VOCAB}},
    ]
    for sent, pause_t, _ in ends:
        rows.append({"t": round(sent - t0, 3), "kind": "audio_stream_end", "text": "", "raw_type": "client",
                     "meta": {"audio_t": round(pause_t, 3)}})
    for t, raw, events in received:
        if not events:
            rows.append({"t": round(t - t0, 3), "kind": _raw_kind(raw), "text": "", "raw_type": "server", "raw": raw})
        for i, ev in enumerate(events):
            meta = {"time_left_s": ev.meta["time_left_s"]} if ev.kind == "go_away" else {"lang": ev.lang}
            if ev.meta.get("interim"):
                meta["interim"] = True
            row = {"t": round(t - t0, 3), "kind": ev.kind, "text": ev.text, "raw_type": _RAW_TYPES[ev.kind], "meta": meta}
            if i == 0:
                row["raw"] = raw
            rows.append(row)
    rows.sort(key=lambda r: r["t"])
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.live
async def test_transcribe_live_es_latency() -> None:
    """Case 10.6: 60 s of the ES clip at real-time pace, end_utterance() on
    each EnergyVad pause (~$0.009). Measures, per final:

    - after the pause: final received - audio_stream_end sent;
    - after the speech: final received - when the last voiced audio was
      live (pause time - 400 ms, on a clock where audio position `a` is live
      at audio_start + a, the moment the chunk ending there is sent).

    Target: es text p50 < 1.5 s after the speech. With
    GLOSA_RECORD_TR_FIXTURE=1 it also rewrites samples/fixtures/tr_es.jsonl.
    """
    api_key = _live_api_key()
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(LIVE_SECONDS), "-i", str(ES_CLIP), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    clock = RealClock()
    engine = TranscribeLiveEngine(CFG, api_key, clock)
    received: list[tuple[float, dict, list[EngineEvent]]] = []
    map_message = engine._map_message

    def spy(raw: types.LiveServerMessage | dict) -> list[EngineEvent]:
        events = map_message(raw)
        wire = raw.model_dump(mode="json", exclude_none=True, by_alias=True) if isinstance(raw, types.LiveServerMessage) else raw
        received.append((clock.now(), wire, events))
        return events

    engine._map_message = spy  # type: ignore[method-assign]
    events: list[EngineEvent] = []
    ends: list[tuple[float, float, float]] = []  # (audio_stream_end sent, pause audio t, speech end audio t)
    tail: list[float] = []

    async def consume() -> None:
        async for ev in engine.events():
            events.append(ev)

    async def run() -> tuple[float, float]:
        await engine.connect()
        t0 = clock.now()
        consumer = asyncio.create_task(consume())
        vad = EnergyVad()
        audio_start = clock.now()
        for k, i in enumerate(range(0, len(pcm), 3200)):
            await asyncio.sleep(max(0.0, audio_start + (k + 1) * 0.1 - clock.now()))  # real-time pace
            chunk = AudioChunk(pcm=pcm[i : i + 3200], t=i / 32000)
            await engine.send_audio(chunk)
            for vev in vad.process(chunk):
                if vev.kind == "pause":
                    await engine.end_utterance()
                    ends.append((clock.now(), vev.t, vev.t - PAUSE_S))
        await engine.end_utterance()  # the clip ends mid-speech: flush the tail
        tail.append(clock.now())
        await asyncio.sleep(4)
        await engine.close()
        await asyncio.wait_for(consumer, timeout=10)
        return t0, audio_start

    t0, audio_start = await asyncio.wait_for(run(), timeout=LIVE_SECONDS + 30)

    finals = [ev for ev in events if ev.kind == "source_final"]
    after_pause: list[float] = []
    after_speech: list[float] = []
    fi = 0
    for n, (sent, _, speech_end) in enumerate(ends):
        next_sent = ends[n + 1][0] if n + 1 < len(ends) else tail[0]
        while fi < len(finals) and finals[fi].t_recv <= sent:
            fi += 1
        if fi < len(finals) and finals[fi].t_recv < next_sent:
            after_pause.append(finals[fi].t_recv - sent)
            after_speech.append(finals[fi].t_recv - (audio_start + speech_end))
            fi += 1
    usd_events = sum(ev.meta.get("usd", 0.0) for ev in events)
    transcript = " ".join(ev.text for ev in finals)
    found = [term for term in VOCAB if term.lower() in transcript.lower()]
    if os.environ.get("GLOSA_RECORD_TR_FIXTURE"):
        _write_fixture(TR_FIXTURE, t0, audio_start, received, ends)
        print(f"\nrecorded {TR_FIXTURE}")
    assert after_pause, [ev.kind for ev in events]
    print(
        f"\nLIVE transcribe: {len(ends)} pauses, {len(finals)} finals, {len(after_pause)} paired,"
        f" {sum(1 for ev in events if ev.meta.get('interim'))} interims"
        f"\n  final after audio_stream_end: p50={_p(after_pause, 0.5):.3f}s p90={_p(after_pause, 0.9):.3f}s"
        f" max={max(after_pause):.3f}s"
        f"\n  final after end of speech:    p50={_p(after_speech, 0.5):.3f}s p90={_p(after_speech, 0.9):.3f}s"
        f" max={max(after_speech):.3f}s"
        f"\n  per final (after pause / after speech): {[(round(a, 2), round(b, 2)) for a, b in zip(after_pause, after_speech)]}"
        f"\n  usd_total={engine.usd_total:.6f} (sum of meta usd {usd_events:.6f});"
        f" usageMetadata messages: {sum(1 for _, raw, _ in received if 'usageMetadata' in raw)}"
        f"\n  vocabulary found: {found}"
        f"\n  transcript: {transcript}"
    )

    assert not [ev for ev in events if ev.kind == "error"], [ev.text for ev in events if ev.kind == "error"]
    assert events[-1].kind == "closed"
    assert finals and all(ev.lang == "es" for ev in finals)
    assert len(after_pause) >= len(ends) - 1
    assert _p(after_speech, 0.5) < 1.5
    assert engine.usd_total == pytest.approx(LIVE_SECONDS / 60 * 0.009, rel=0.01)
    assert usd_events == pytest.approx(engine.usd_total)
    assert len(found) >= 3
