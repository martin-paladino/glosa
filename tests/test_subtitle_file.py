"""Tests for scripts/subtitle_file.py (task-21a-brief.md): no network.

  - parse_glossary(): the "term=translation,..." CLI spec -> Talk.glossary.
  - build_parser()/main(): CLI args parsed (required --lang/--targets,
    --engine choices, --targets split+stripped, --glossary threaded
    through) and a clear error for a missing input file (before anything
    that would need an API key).
  - _write_exports(): given segments already in a scratch Database (no
    RoomWorker), the VTT/SRT this script writes are well-formed (WEBVTT
    header, sequential SRT numbering), non-empty and cue-time monotone --
    glosa/exports.py's own render() is exercised for real here, just
    without paying for a room run to populate the DB.
  - run_subtitle_file(): one real RoomWorker run, engine_mode "fake"
    (FakeEngine, no network -- glosa.web.app.make_engine_factory, the same
    factory production uses), proving the whole pipeline wiring (Room/Talk
    -> RoomWorker -> Database -> glosa/exports.py -> files on disk) end to
    end. tests/fixtures/subtitle_file_fake.jsonl's last record sits at
    t=9.0s -- past tests/fixtures/short_clip.wav's 2.5s plus RoomWorker's
    default 5s tail -- so the room's own clean end tears the session down
    (expected), rather than FakeEngine exhausting its records first (which
    glosa/room.py's SessionRelay treats as an unexpected close and
    reconnects, eventually falling back to the glossary engine -- a real
    robustness feature, just not what this test wants to exercise).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import subtitle_file
from scripts.subtitle_file import (
    Result,
    _write_exports,
    build_parser,
    main,
    parse_glossary,
    run_subtitle_file,
)
from glosa.db import init_db
from glosa.models import GlossaryTerm

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_FIXTURE = FIXTURES / "subtitle_file_fake.jsonl"
SHORT_CLIP = FIXTURES / "short_clip.wav"

_VTT_TIME = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) --> ")
_SRT_TIME = re.compile(r"^(\d\d):(\d\d):(\d\d),(\d\d\d) --> ")


def _cue_starts(text: str, pattern: re.Pattern) -> list[float]:
    starts = []
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            h, mnt, s, ms = (int(g) for g in m.groups())
            starts.append(h * 3600 + mnt * 60 + s + ms / 1000)
    return starts


# --------------------------------------------------------------------- parse_glossary


def test_parse_glossary_empty():
    assert parse_glossary(None) == []
    assert parse_glossary("") == []
    assert parse_glossary("   ") == []


def test_parse_glossary_bare_and_translated_terms():
    terms = parse_glossary("Kubernetes, namespaces=espacios de nombres , labels=etiquetas")
    assert terms == [
        GlossaryTerm("Kubernetes", True),
        GlossaryTerm("namespaces", False, translation="espacios de nombres"),
        GlossaryTerm("labels", False, translation="etiquetas"),
    ]


# --------------------------------------------------------------------- CLI parsing


def test_build_parser_defaults():
    args = build_parser().parse_args(["clip.mp4", "--lang", "es", "--targets", "en"])
    assert args.engine == "glossary"
    assert args.out_dir is None
    assert args.glossary is None


def test_build_parser_requires_lang_and_targets():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clip.mp4"])


def test_build_parser_rejects_unknown_engine():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clip.mp4", "--lang", "es", "--targets", "en", "--engine", "bogus"])


def test_main_missing_input_file_is_a_clear_error(tmp_path, capsys):
    missing = tmp_path / "nope.mp4"
    code = main([str(missing), "--lang", "es", "--targets", "en"])
    assert code == 1
    err = capsys.readouterr().err
    assert "input file not found" in err
    assert str(missing) in err


def test_main_threads_parsed_args_into_run_subtitle_file(monkeypatch, tmp_path):
    """No RoomWorker run here: run_subtitle_file() itself is replaced, just
    to inspect what main() parsed and passed it -- targets split/stripped,
    glossary parsed, the (mocked) resolved API key threaded through."""
    input_file = tmp_path / "clip.mp4"
    input_file.write_bytes(b"not really a video, never opened by this test")
    captured: dict = {}

    async def fake_run_subtitle_file(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return Result(cost_usd=0.0, audio_s=0.0, written=[])

    monkeypatch.setattr(subtitle_file, "resolve_api_key", lambda: "fake-key-never-real")
    monkeypatch.setattr(subtitle_file, "run_subtitle_file", fake_run_subtitle_file)

    code = main(
        [str(input_file), "--lang", "es", "--targets", "en, fr", "--glossary", "Kubernetes,namespaces=espacios de nombres"]
    )

    assert code == 0
    assert captured["path"] == input_file
    assert captured["source_lang"] == "es"
    assert captured["targets"] == ["en", "fr"]
    assert captured["engine"] == "glossary"
    assert captured["api_key"] == "fake-key-never-real"
    assert captured["glossary"] == [
        GlossaryTerm("Kubernetes", True),
        GlossaryTerm("namespaces", False, translation="espacios de nombres"),
    ]


# --------------------------------------------------------------------- _write_exports (no RoomWorker)


async def test_write_exports_well_formed_and_monotone(tmp_path):
    db = init_db(tmp_path / "scratch.db")
    await db.save_segment("t1", "r1", "es", "source", "live", "Hola mundo.", t_start=1.0, t_end=2.0)
    await db.save_segment("t1", "r1", "es", "source", "live", "Adios amigos.", t_start=3.0, t_end=4.0)
    await db.save_segment("t1", "r1", "en", "translation", "live", "Hello world.", t_start=1.2, t_end=2.1)

    written = await _write_exports(db, "t1", ["es", "en"], shift_s=0.5, out_dir=tmp_path, stem="clip")
    db.close()

    assert {p.name for p in written} == {"clip.es.vtt", "clip.es.srt", "clip.en.vtt", "clip.en.srt"}

    es_vtt = (tmp_path / "clip.es.vtt").read_text(encoding="utf-8")
    assert es_vtt.startswith("WEBVTT\n")
    assert "Hola mundo." in es_vtt
    assert "Adios amigos." in es_vtt
    es_starts = _cue_starts(es_vtt, _VTT_TIME)
    assert len(es_starts) == 2
    assert es_starts == sorted(es_starts)
    assert es_starts[0] == pytest.approx(0.5)  # 1.0 - shift_s, Ruling 6

    es_srt = (tmp_path / "clip.es.srt").read_text(encoding="utf-8")
    assert es_srt.startswith("1\n")
    assert "Hola mundo." in es_srt and "Adios amigos." in es_srt
    srt_starts = _cue_starts(es_srt, _SRT_TIME)
    assert srt_starts == es_starts

    en_vtt = (tmp_path / "clip.en.vtt").read_text(encoding="utf-8")
    assert "Hello world." in en_vtt


async def test_write_exports_empty_track_is_still_well_formed(tmp_path):
    """A talk_id/lang with no saved segments: render() still produces a
    valid (if minimal) VTT/empty SRT -- no crash, no bogus cues."""
    db = init_db(tmp_path / "scratch.db")
    written = await _write_exports(db, "no-such-talk", ["es"], shift_s=1.0, out_dir=tmp_path, stem="clip")
    db.close()

    vtt = (tmp_path / "clip.es.vtt").read_text(encoding="utf-8")
    srt = (tmp_path / "clip.es.srt").read_text(encoding="utf-8")
    assert vtt == "WEBVTT\n"
    assert srt == ""
    assert len(written) == 2


# --------------------------------------------------------------------- full pipeline, engine_mode fake


async def test_run_subtitle_file_fake_engine_end_to_end(tmp_path):
    result = await run_subtitle_file(
        SHORT_CLIP,
        source_lang="es",
        targets=["en"],
        engine="fast",
        out_dir=tmp_path,
        api_key="fake-key-never-real",
        engine_mode="fake",
        fake_fixture=str(FAKE_FIXTURE),
        quiet=True,
    )

    assert isinstance(result, Result)
    assert result.cost_usd == 0.0  # engine_mode fake: no network, no spend
    assert result.audio_s >= 7.0  # short_clip.wav (2.5s) + the room's tail
    assert {p.name for p in result.written} == {
        "short_clip.es.vtt", "short_clip.es.srt", "short_clip.en.vtt", "short_clip.en.srt",
    }
    assert all(p.parent == tmp_path for p in result.written)

    es_vtt = (tmp_path / "short_clip.es.vtt").read_text(encoding="utf-8")
    en_vtt = (tmp_path / "short_clip.en.vtt").read_text(encoding="utf-8")
    assert es_vtt.startswith("WEBVTT\n")
    assert "Hola mundo." in es_vtt
    assert "Adios amigos." in es_vtt
    assert "Hello world." in en_vtt
    assert "Goodbye friends." in en_vtt

    es_starts = _cue_starts(es_vtt, _VTT_TIME)
    en_starts = _cue_starts(en_vtt, _VTT_TIME)
    # Both short segments land inside the default 2.4 s shift, so after
    # clipping they may share one readable cue (exports._readable): no text is lost.
    assert 1 <= len(es_starts) <= 2
    assert 1 <= len(en_starts) <= 2
    assert es_starts == sorted(es_starts)
    assert en_starts == sorted(en_starts)

    es_srt = (tmp_path / "short_clip.es.srt").read_text(encoding="utf-8")
    assert es_srt.startswith("1\n")
    assert "Hola mundo." in es_srt and "Adios amigos." in es_srt
