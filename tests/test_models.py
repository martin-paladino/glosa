"""Sanity checks for the shared dataclasses in glosa.models: defaults apply
and mutable defaults (meta, vocabulary, ...) aren't accidentally shared
between instances.
"""

from __future__ import annotations

from glosa.models import AudioChunk, CaptionMsg, EngineConfig, EngineEvent


def test_engine_event_defaults() -> None:
    event = EngineEvent(kind="source_delta", text="hola")
    assert event.lang is None
    assert event.t_recv == 0.0
    assert event.meta == {}


def test_engine_event_meta_default_is_not_shared_between_instances() -> None:
    a = EngineEvent(kind="go_away")
    b = EngineEvent(kind="go_away")
    a.meta["time_left_s"] = 10.0
    assert b.meta == {}


def test_engine_config_vocabulary_default_is_not_shared_between_instances() -> None:
    a = EngineConfig(kind="glossary", source_lang="es", target_lang="en")
    b = EngineConfig(kind="glossary", source_lang="es", target_lang="en")
    a.vocabulary.append("Kubernetes")
    assert b.vocabulary == []


def test_audio_chunk_fields() -> None:
    chunk = AudioChunk(pcm=b"\x00" * 3200, t=1.2)
    assert len(chunk.pcm) == 3200
    assert chunk.t == 1.2


def test_caption_msg_ts_defaults_to_none_and_accepts_epoch_seconds() -> None:
    unset = CaptionMsg(id=1, type="append", seg=0, text="Hola")
    assert unset.ts is None

    timestamped = CaptionMsg(id=1, type="append", seg=0, text="Hola", ts=1735000000.0)
    assert timestamped.ts == 1735000000.0
