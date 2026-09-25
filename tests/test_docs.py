"""Lightweight content checks for README.md/README.es.md against
glosa/config.py's/glosa/web/app.py's actual secrets-loading behavior
(B-Minor #10, C-I2): a doc claim that drifts from the code is worse than
no claim at all, and an undocumented shipped feature looks broken to a
judge who finds it by accident. This is not a full doc-vs-code diff, just
the specific facts the final-review fixes touched.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_readme_documents_the_env_file_override() -> None:
    # B-Minor #10: Settings.load()/main() read .env from the working
    # directory by default, or $GLOSA_ENV_FILE when set (glosa/web/app.py's
    # main(), config.py's Settings.load(env_path=...)) -- the same kind of
    # override GLOSA_CONFIG gives config.yaml, documented right next to it,
    # but GLOSA_ENV_FILE itself was undocumented anywhere user-facing.
    assert "GLOSA_ENV_FILE" in _read("README.md")


def test_readme_es_documents_the_env_file_override() -> None:
    assert "GLOSA_ENV_FILE" in _read("README.es.md")


# ---- C-I2: shipped features missing from "What's in the box" -----------------


def test_readmes_document_the_what_did_i_miss_summaries() -> None:
    # glosa/summary.py's SummaryScheduler (Task 17): GET
    # /api/summary/{slug}/{lang}, every SUMMARY_EVERY_S (180s == 3 min) while
    # a talk is live, gemini-3.5-flash-lite. Verified undocumented by
    # final-review-C.
    for name in ("README.md", "README.es.md"):
        text = _read(name)
        assert "/api/summary/{slug}/{lang}" in text, name
        assert "3.5-flash-lite" in text, name
        assert "3 min" in text, name


def test_readmes_document_picture_in_picture_captions() -> None:
    # room.js's Picture-in-Picture captions (Task 20): desktop Chrome/Edge
    # only (window.documentPictureInPicture).
    for name in ("README.md", "README.es.md"):
        text = _read(name)
        assert "Picture-in-Picture" in text, name
        assert "Chrome/Edge" in text, name


def _box_list(text: str) -> str:
    # "### What's in the box" / "### Qué trae la caja" up to the next "###".
    start = text.index("### What's in the box") if "### What's" in text else text.index("### Qué trae la caja")
    end = text.index("\n### ", start + 10)
    return text[start:end]


def test_readme_links_the_silence_gate_from_the_box_list() -> None:
    assert "(#scaling)" in _box_list(_read("README.md"))


def test_readme_es_links_the_silence_gate_from_the_box_list() -> None:
    assert "(#escala)" in _box_list(_read("README.es.md"))


def test_readme_links_local_mode_from_the_box_list() -> None:
    assert "(#local-mode-no-cloud)" in _box_list(_read("README.md"))


def test_readme_es_links_local_mode_from_the_box_list() -> None:
    assert "(#modo-local-sin-nube)" in _box_list(_read("README.es.md"))


def test_readmes_document_the_jev_talk_mismatch_suggestion() -> None:
    # glosa/talk_check.py (Task 18): admin-only panel notice, needs
    # TYPESAFE_API_KEY, i18n key issue_talk_mismatch_next_title == "¿Pasar
    # a manual?" / "Switch to manual?".
    for name in ("README.md", "README.es.md"):
        text = _read(name)
        assert "TYPESAFE_API_KEY" in text, name
        assert "Pasar a manual" in text, name


def _quick_start(text: str) -> str:
    # Both READMEs use "## Quick start"/"## Inicio rápido" as the section
    # heading and "## Configuration"/"## Configuración" as the next one.
    start = text.index("## Quick start") if "## Quick start" in text else text.index("## Inicio rápido")
    end = text.index("## Config", start)
    return text[start:end]


def test_readmes_quick_start_notes_the_demo_fake_clip_loops() -> None:
    # C-I2: a judge who takes a bit longer to open the browser used to see
    # "no captions" once the demo's single ~90s pass ended (final-review-C).
    # The other agent adds `loop: true` to config.demo-fake.yaml; this is
    # the doc half -- Quick Start must say so, not just mention "loop"
    # somewhere else in the file.
    for name in ("README.md", "README.es.md"):
        assert "loop" in _quick_start(_read(name)).lower(), name


def test_readmes_quick_start_mentions_probar_con_audio_replay() -> None:
    for name in ("README.md", "README.es.md"):
        assert "Probar con audio" in _quick_start(_read(name)), name
