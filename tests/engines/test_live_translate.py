"""LiveTranslateEngine: message mapping, error classification and the session
loop are tested offline (recorded message shapes + a fake client); one
`live` test talks to the real API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types

from glosa.clock import FakeClock, RealClock
from glosa.engines.live_translate import MODEL, LiveTranslateEngine
from glosa.models import AudioChunk, EngineConfig, EngineEvent

ROOT = Path(__file__).resolve().parent.parent.parent
LT_MESSAGES = json.loads((ROOT / "tests" / "fixtures" / "lt_messages.json").read_text(encoding="utf-8"))
CFG = EngineConfig(**LT_MESSAGES["cfg"])
PRICE_PER_MIN = 0.0368
USD_PER_AUDIO_S = PRICE_PER_MIN / 60


def _engine(clock: FakeClock | None = None, client: object | None = None) -> LiveTranslateEngine:
    return LiveTranslateEngine(CFG, "test-key", clock or FakeClock(start=12.0), client=client)


def _event_dict(ev: EngineEvent) -> dict:
    return {"kind": ev.kind, "text": ev.text, "lang": ev.lang, "meta": ev.meta}


def _msg(raw: dict) -> types.LiveServerMessage:
    return types.LiveServerMessage.model_validate(raw)


# ---------------------------------------------------------------- 2.2 mapping


@pytest.mark.parametrize("case", LT_MESSAGES["cases"], ids=lambda c: c["name"])
def test_map_message_recorded_shapes(case: dict) -> None:
    clock = FakeClock(start=12.0)
    engine = _engine(clock)

    events = engine._map_message(case["raw"])

    assert [_event_dict(ev) for ev in events] == case["expected"]
    assert all(ev.t_recv == 12.0 for ev in events)
    assert engine.usd_total == pytest.approx(case.get("usd_total", 0.0), abs=1e-9)


def test_map_message_accepts_sdk_message_objects() -> None:
    raw = LT_MESSAGES["cases"][1]["raw"]  # output_transcription
    assert [_event_dict(ev) for ev in _engine()._map_message(_msg(raw))] == LT_MESSAGES["cases"][1]["expected"]


def test_usage_cost_rides_on_the_next_event_as_an_increment() -> None:
    engine = _engine()
    usage = {"usageMetadata": {"promptTokenCount": 50, "responseTokenCount": 50, "totalTokenCount": 100}}
    delta = {"serverContent": {"outputTranscription": {"text": " hola", "languageCode": "es"}}}

    assert engine._map_message(usage) == []
    first = engine._map_message(delta)
    second = engine._map_message(delta)

    assert first[0].meta == {"usd": pytest.approx(2 * USD_PER_AUDIO_S)}  # 50 tokens = 2 s of audio
    assert second[0].meta == {}  # already reported
    assert engine.usd_total == pytest.approx(2 * USD_PER_AUDIO_S)


def test_price_per_min_is_configurable() -> None:
    engine = LiveTranslateEngine(CFG, "test-key", FakeClock(), price_per_min=0.06)
    engine._map_message({"usageMetadata": {"promptTokenCount": 1500}})  # 60 s of audio
    assert engine.usd_total == pytest.approx(0.06)


def test_empty_or_missing_language_transcriptions() -> None:
    engine = _engine()
    assert engine._map_message({"serverContent": {"inputTranscription": {"finished": True}}}) == []
    [ev] = engine._map_message({"serverContent": {"outputTranscription": {"text": "hola"}}})
    assert (ev.kind, ev.lang) == ("target_delta", "es")  # falls back to cfg.target_lang


# ------------------------------------------------------------- 2.3 errors


def test_classify_error_uses_the_shared_policy_and_carries_the_usage() -> None:
    """The cases are in tests/engines/test_gemini_live.py (one policy for both engines)."""
    engine = _engine(FakeClock(start=3.0))
    engine._usd_unreported = 0.25
    ev = engine._classify_error(errors.APIError(1011, "Internal error encountered.", None))
    assert (ev.kind, ev.t_recv) == ("error", 3.0)
    assert ev.meta == {"code": 1011, "retryable": True, "usd": 0.25}
    assert ev.text == "APIError: 1011 None. Internal error encountered."



# ------------------------------------------------- session loop (fake client)


class FakeSession:
    """Scripted session: receive() yields one turn per call, like the SDK
    (which stops iterating at turn_complete); when turns run out it raises
    `end_exc`, as the SDK does when the websocket closes."""

    def __init__(self, turns: list[list[dict]], end_exc: Exception) -> None:
        self.turns = [[_msg(raw) for raw in turn] for turn in turns]
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


SRC = {"serverContent": {"inputTranscription": {"text": "Great starting", "languageCode": "en"}}}
TGT = {"serverContent": {"outputTranscription": {"text": "Un gran", "languageCode": "es"}}}
DONE = {"serverContent": {"turnComplete": True}}


async def test_connect_requests_live_translate_with_spec_config() -> None:
    live = FakeLive(FakeSession([], errors.APIError(1000, "", None)))
    engine = _engine(client=_client(live))

    await engine.connect()

    [call] = live.calls
    cfg: types.LiveConnectConfig = call["config"]
    assert call["model"] == MODEL == "gemini-3.5-live-translate-preview"
    assert cfg.response_modalities == [types.Modality.AUDIO]
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None
    assert cfg.translation_config.target_language_code == "es"
    assert cfg.translation_config.echo_target_language is True


async def test_events_keeps_receiving_across_turns_until_the_socket_closes() -> None:
    session = FakeSession([[SRC, DONE], [TGT, DONE]], errors.APIError(1000, "", None))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()

    kinds = [ev.kind async for ev in engine.events()]

    assert kinds == ["source_delta", "target_delta", "closed"]  # a normal close is not an error


async def test_events_turns_a_session_exception_into_error_then_closed() -> None:
    usage = {"usageMetadata": {"promptTokenCount": 25}}
    session = FakeSession([[SRC, usage]], errors.ClientError(429, {"error": {"code": 429}}))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()

    events = [ev async for ev in engine.events()]

    assert [ev.kind for ev in events] == ["source_delta", "error", "closed"]
    assert events[1].meta == {"code": 429, "retryable": True, "usd": pytest.approx(USD_PER_AUDIO_S)}


async def test_connect_failure_is_reported_through_events() -> None:
    live = FakeLive(connect_exc=errors.ClientError(402, {"error": {"code": 402}}))
    engine = _engine(client=_client(live))

    await engine.connect()  # does not raise: the relay reads the failure from events()
    await engine.send_audio(AudioChunk(pcm=b"\x00" * 3200, t=0.0))  # dropped, no session
    events = [ev async for ev in engine.events()]

    assert [(ev.kind, ev.meta) for ev in events] == [
        ("error", {"code": 402, "retryable": False, "payment": True}),
        ("closed", {}),
    ]


async def test_send_audio_end_utterance_and_close() -> None:
    session = FakeSession([], errors.APIError(1000, "", None))
    live = FakeLive(session)
    engine = _engine(client=_client(live))
    await engine.connect()

    await engine.send_audio(AudioChunk(pcm=b"\x01\x02" * 1600, t=0.0))
    await engine.end_utterance()
    await engine.close()
    await engine.send_audio(AudioChunk(pcm=b"\x00" * 3200, t=0.1))  # after close: dropped

    blob = session.sent[0]["audio"]
    assert (blob.data, blob.mime_type) == (b"\x01\x02" * 1600, "audio/pcm;rate=16000")
    assert session.sent[1:] == [{"audio_stream_end": True}]
    assert live.exited
    assert [ev.kind async for ev in engine.events()] == ["closed"]


async def test_close_during_the_handshake_does_not_leak_the_websocket() -> None:
    session = FakeSession([[SRC]], errors.APIError(1000, "", None))
    gate = asyncio.Event()
    live = FakeLive(session, handshake_gate=gate)
    engine = _engine(client=_client(live))

    connecting = asyncio.create_task(engine.connect())
    await live.handshake_started.wait()
    await engine.close()  # the relay retires the engine while the handshake is in flight
    gate.set()
    await asyncio.wait_for(connecting, timeout=1.0)

    assert live.exited  # the just-opened connection was closed again
    await engine.send_audio(AudioChunk(pcm=b"\x00" * 3200, t=0.0))
    assert session.sent == []
    assert [ev.kind async for ev in engine.events()] == ["closed"]


async def test_close_while_receiving_ends_with_closed_only() -> None:
    session = FakeSession([[SRC]], errors.APIError(1006, "Abnormal closure.", None))
    engine = _engine(client=_client(FakeLive(session)))
    await engine.connect()
    stream = engine.events()

    first = await anext(stream)
    await engine.close()
    rest = [ev.kind async for ev in stream]

    assert first.kind == "source_delta"
    assert rest == ["closed"]  # errors caused by our own close() are not reported


# ------------------------------------------------------------- 2.4 live smoke


EN_CLIP = ROOT / "samples" / "en_clip.opus"


@pytest.mark.live
async def test_lt_smoke() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set")
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", "20", "-i", str(EN_CLIP), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    engine = LiveTranslateEngine(
        EngineConfig(kind="fast", source_lang="en", target_lang="es"), api_key, RealClock()
    )
    events: list[EngineEvent] = []

    async def consume() -> None:
        async for ev in engine.events():
            events.append(ev)

    async def run() -> None:
        await engine.connect()
        consumer = asyncio.create_task(consume())
        for i in range(0, len(pcm), 3200):  # 100 ms chunks at real-time pace
            await engine.send_audio(AudioChunk(pcm=pcm[i : i + 3200], t=i / 32000))
            await asyncio.sleep(0.1)
        await engine.end_utterance()
        await asyncio.sleep(5)  # let the tail of the translation arrive
        await engine.close()
        await asyncio.wait_for(consumer, timeout=10)

    await asyncio.wait_for(run(), timeout=60)

    targets = [ev for ev in events if ev.kind == "target_delta"]
    assert targets, [ev.kind for ev in events]
    assert all(ev.lang == "es" for ev in targets)
    assert "".join(ev.text for ev in targets).strip()
    assert not [ev for ev in events if ev.kind == "error"]
    assert events[-1].kind == "closed"
    assert engine.usd_total > 0
