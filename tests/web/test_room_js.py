"""room.js (the audience view) under Node with a tiny fake DOM
(tests/web/room_js_harness.js): what one caption stream draws. Skipped where
Node is not installed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from glosa.i18n import STRINGS

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("room_js_harness.js")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node.js to run room.js")

TALK = {"id": 1, "type": "talk", "data": {"talk_id": "t1", "title": "Charla", "speakers": [], "language": "es"}}


def _draw(msgs: list[dict], lang: str = "es") -> dict:
    numbered = [{"id": i, **m} for i, m in enumerate([TALK, *msgs], start=1)]
    done = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps({"lang": lang, "i18n": STRINGS["es"], "msgs": numbered}),
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(done.stdout)


def test_the_harness_draws_appended_phrases() -> None:
    out = _draw([
        {"type": "append", "seg": 0, "text": "Hola a"},
        {"type": "append", "seg": 0, "text": " todos."},
        {"type": "close", "seg": 0},
        {"type": "append", "seg": 1, "text": " Bienvenidos"},
    ])
    assert out["url"] == "/api/stream/r1/es"
    assert out["phrases"] == [
        {"seg": 0, "text": "Hola a todos.", "open": False},
        {"seg": 1, "text": "Bienvenidos", "open": True},
    ]


def test_set_replaces_the_text_of_the_open_phrase() -> None:
    out = _draw([
        {"type": "set", "seg": 4, "text": "por cierto"},
        {"type": "set", "seg": 4, "text": "Por cierto, cuando ustedes"},
        {"type": "set", "seg": 4, "text": "Por cierto, cuando ustedes reciben la factura "},
    ])
    assert out["phrases"] == [{"seg": 4, "text": "Por cierto, cuando ustedes reciben la factura", "open": True}]


def test_set_then_close_settles_the_phrase_and_the_next_set_starts_another() -> None:
    out = _draw([
        {"type": "set", "seg": 0, "text": "Hola a"},
        {"type": "set", "seg": 0, "text": "Hola a todos."},
        {"type": "close", "seg": 0},
        {"type": "set", "seg": 1, "text": "Bienvenidos"},
        {"type": "set", "seg": 1, "text": "Bienvenidos a Nerdearla"},
    ])
    assert out["phrases"] == [
        {"seg": 0, "text": "Hola a todos.", "open": False},
        {"seg": 1, "text": "Bienvenidos a Nerdearla", "open": True},
    ]
    assert "Hola a todos. Bienvenidos a Nerdearla" in out["live"]


def test_an_empty_set_removes_the_phrase() -> None:
    """transcribe-live's empty final: it was not speech after all."""
    out = _draw([
        {"type": "set", "seg": 0, "text": "Hola."},
        {"type": "close", "seg": 0},
        {"type": "set", "seg": 1, "text": "eh"},
        {"type": "set", "seg": 1, "text": ""},
        {"type": "close", "seg": 1},
        {"type": "set", "seg": 2, "text": "Sigo"},
    ])
    assert [(p["seg"], p["text"]) for p in out["phrases"]] == [(0, "Hola."), (2, "Sigo")]
    assert "Hola. Sigo" in out["live"]  # one space between them, not two


def test_an_empty_set_for_a_new_phrase_draws_nothing() -> None:
    out = _draw([{"type": "set", "seg": 0, "text": " "}, {"type": "close", "seg": 0}])
    assert out["phrases"] == []
