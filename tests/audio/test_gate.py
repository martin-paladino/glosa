"""SilenceGate: unit tests against synthetic AudioChunk streams (no PCM
synthesis needed -- the gate only looks at chunk.t and the caller's
``voiced`` flag, like EnergyVad.process() only looks at chunk.pcm/t)."""

from __future__ import annotations

from glosa.audio.gate import PREROLL_S, SilenceGate
from glosa.models import AudioChunk

CHUNK_S = 0.1


def _chunks(n: int, start: float = 0.0) -> list[AudioChunk]:
    return [AudioChunk(pcm=b"", t=round(start + i * CHUNK_S, 3)) for i in range(n)]


def _feed(gate: SilenceGate, chunks: list[AudioChunk], voiced: bool) -> list[list[AudioChunk]]:
    return [gate.update(c, voiced) for c in chunks]


def test_passes_chunks_through_while_voiced() -> None:
    gate = SilenceGate(after_s=20.0)
    chunks = _chunks(5)
    out = _feed(gate, chunks, voiced=True)
    assert out == [[c] for c in chunks]
    assert gate.gated is False


def test_passes_chunks_through_during_a_short_silence() -> None:
    gate = SilenceGate(after_s=20.0)
    chunks = _chunks(50)  # 5 s: well under the 20 s threshold
    out = _feed(gate, chunks, voiced=False)
    assert out == [[c] for c in chunks]
    assert gate.gated is False
    assert gate.gated_s == 0.0


def test_gate_closes_after_exactly_20s_and_not_before() -> None:
    gate = SilenceGate(after_s=20.0)
    chunks = _chunks(210)  # 21 s of continuous silence
    out = _feed(gate, chunks, voiced=False)
    closed_at = next(i for i, sent in enumerate(out) if sent == [])
    # every chunk before the close was forwarded (not gated "before" 20 s)
    assert all(out[i] == [chunks[i]] for i in range(closed_at))
    assert round(chunks[closed_at].t, 3) == 20.0
    assert gate.gated is True


def test_gate_stays_open_just_under_20s_of_silence() -> None:
    gate = SilenceGate(after_s=20.0)
    chunks = _chunks(199)  # 19.9 s: one chunk short of the threshold
    out = _feed(gate, chunks, voiced=False)
    assert out == [[c] for c in chunks]
    assert gate.gated is False


def test_preroll_is_exactly_1s_in_order_sent_before_the_live_chunk() -> None:
    gate = SilenceGate(after_s=20.0, preroll_s=1.0)
    silence = _chunks(230, start=0.0)  # 23 s: gate closes at 20 s, stays closed 3 s
    _feed(gate, silence, voiced=False)
    assert gate.gated is True

    speech = _chunks(3, start=silence[-1].t + CHUNK_S)
    first_out = gate.update(speech[0], voiced=True)

    # exactly PREROLL_S / CHUNK_S saved chunks, then the live one, in order
    n_preroll = round(PREROLL_S / CHUNK_S)
    assert len(first_out) == n_preroll + 1
    assert first_out[-1] is speech[0]
    assert [round(c.t, 3) for c in first_out[:-1]] == [
        round(speech[0].t - CHUNK_S * k, 3) for k in range(n_preroll, 0, -1)
    ]
    assert gate.gated is False

    # normal live forwarding resumes right after, no gap/duplicate
    rest = [gate.update(c, voiced=True) for c in speech[1:]]
    assert rest == [[c] for c in speech[1:]]


def test_no_gap_or_duplicate_across_the_whole_transition() -> None:
    gate = SilenceGate(after_s=20.0, preroll_s=1.0)
    before = _chunks(50, start=0.0)  # 5 s of speech
    silence = _chunks(250, start=before[-1].t + CHUNK_S)  # 25 s of silence: gate closes and stays closed
    after = _chunks(20, start=silence[-1].t + CHUNK_S)  # speech resumes

    sent: list[AudioChunk] = []
    for c in before:
        sent += gate.update(c, voiced=True)
    for c in silence:
        sent += gate.update(c, voiced=False)
    for i, c in enumerate(after):
        sent += gate.update(c, voiced=(i == 0))  # VAD flips in_speech True on the first one

    ts = [c.t for c in sent]
    assert ts == sorted(ts)  # strictly in order
    assert len(ts) == len(set(ts))  # no duplicates
    # every "before" and "after" chunk made it through; only interior silence
    # (older than the 1 s pre-roll) was ever dropped
    for c in before + after:
        assert c in sent
    assert set(ts) <= {c.t for c in before + silence + after}


def test_voice_blip_during_the_20s_window_resets_the_timer() -> None:
    gate = SilenceGate(after_s=20.0)
    _feed(gate, _chunks(150, start=0.0), voiced=False)  # 15 s of silence
    assert gate.gated is False
    gate.update(AudioChunk(pcm=b"", t=15.0), voiced=True)  # a blip: resets the silence clock
    out = _feed(gate, _chunks(150, start=15.1), voiced=False)  # another 15 s: still short of 20 s
    assert all(sent != [] for sent in out)
    assert gate.gated is False


def test_gated_s_counts_only_audio_dropped_for_good_not_the_preroll() -> None:
    gate = SilenceGate(after_s=20.0, preroll_s=1.0)
    silence = _chunks(200 + 50, start=0.0)  # closes at 20 s, stays closed 5 s more
    _feed(gate, silence, voiced=False)
    # 5 s closed, 1 s of that is still held as pre-roll (recoverable) -> 4 s gone for good
    assert round(gate.gated_s, 3) == 4.0

    speech = gate.update(AudioChunk(pcm=b"", t=silence[-1].t + CHUNK_S), voiced=True)
    assert len(speech) == round(PREROLL_S / CHUNK_S) + 1
    assert round(gate.gated_s, 3) == 4.0  # the flushed pre-roll never counted as "gone"


def test_paused_s_tracks_how_long_the_gate_has_been_closed() -> None:
    gate = SilenceGate(after_s=20.0)
    silence = _chunks(230, start=0.0)  # closes at 20 s, 3 s more after that
    for c in silence:
        gate.update(c, voiced=False)
    assert round(gate.paused_s, 3) == 2.9  # last chunk at t=22.9, closed at t=20.0
    gate.update(AudioChunk(pcm=b"", t=silence[-1].t + CHUNK_S), voiced=True)
    assert gate.paused_s == 0.0


def test_disabled_with_zero_never_gates() -> None:
    gate = SilenceGate(after_s=0.0)
    assert gate.enabled is False
    out = _feed(gate, _chunks(400, start=0.0), voiced=False)  # 40 s of silence
    assert out == [[c] for c in _chunks(400, start=0.0)]
    assert gate.gated is False
    assert gate.gated_s == 0.0
