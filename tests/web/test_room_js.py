"""room.js (the audience view) under Node with a tiny fake DOM
(tests/web/room_js_harness.js): what one caption stream draws. Skipped where
Node is not installed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from glosa.i18n import STRINGS

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("room_js_harness.js")
CLAMP_HARNESS = Path(__file__).with_name("room_clamp_harness.js")
TEMPLATES = Path(__file__).resolve().parents[2] / "glosa" / "web" / "templates"


def _page_scripts(template: str) -> list[str]:
    """The template's own static/js scripts, in load order: the harness runs
    the ones it knows (theme.js, room.js) exactly as the page would."""
    return re.findall(r'<script src="/static/js/([\w.-]+\.js)"', (TEMPLATES / template).read_text())

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node.js to run room.js")

TALK = {"id": 1, "type": "talk", "data": {"talk_id": "t1", "title": "Charla", "speakers": [], "language": "es"}}


def _draw(
    msgs: list[dict],
    lang: str = "es",
    *,
    fetch: dict | None = None,
    click: str | list[str] | None = None,
    keys: list | None = None,
    pip: bool = False,
    drag_stored: dict | None = None,
    viewport: dict | None = None,
    resize: dict | None = None,
    station: bool = False,
    storage: dict | None = None,
) -> dict:
    numbered = [{"id": i, **m} for i, m in enumerate([TALK, *msgs], start=1)]
    payload = {"lang": lang, "i18n": STRINGS["es"], "msgs": numbered}
    if fetch is not None:
        payload["fetch"] = fetch
    if click is not None:
        payload["click"] = click
    if keys is not None:
        payload["keys"] = keys
    if pip:
        payload["pip"] = True
    if drag_stored is not None:
        payload["dragStored"] = drag_stored
    if viewport is not None:
        payload["viewport"] = viewport
    if resize is not None:
        payload["resize"] = resize
    if station:
        payload["station"] = True
    if storage is not None:
        payload["storage"] = storage
    payload["scripts"] = _page_scripts("station.html" if station else "room.html")
    done = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(payload),
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(done.stdout)


def _clamp(x, y, panel_w, panel_h, viewport_w, viewport_h, margin=8) -> dict:
    payload = {
        "x": x, "y": y, "panelW": panel_w, "panelH": panel_h,
        "viewportW": viewport_w, "viewportH": viewport_h, "margin": margin,
    }
    done = subprocess.run(
        [NODE, str(CLAMP_HARNESS)], input=json.dumps(payload),
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


# ---- "¿Qué me perdí?" (Task 17): the summary button and panel -----------------------


def test_the_station_page_draws_captions_without_the_audience_only_controls() -> None:
    """station.html reuses room.js but has no summary panel and no PiP
    button: room.js must still open the stream and draw the captions."""
    out = _draw([
        {"type": "append", "seg": 0, "text": "Hola a todos."},
        {"type": "close", "seg": 0},
    ], station=True)
    assert out["url"] == "/api/stream/r1/es"
    assert out["phrases"] == [{"seg": 0, "text": "Hola a todos.", "open": False}]


def test_the_station_page_ignores_a_summary_position_stored_by_the_audience_view() -> None:
    """Same origin: a panel dragged on /s/{slug} leaves glosa.summaryPos in
    localStorage, and the station has no panel to place."""
    out = _draw([{"type": "append", "seg": 0, "text": "Hola."}],
                station=True, drag_stored={"x": 40, "y": 60}, resize={"width": 800, "height": 600})
    assert out["phrases"] == [{"seg": 0, "text": "Hola.", "open": True}]


def test_the_station_page_draws_captions_with_the_room_list_folded_on_the_audience_view() -> None:
    """Task ui3: the room list's fold toggle is audience-only (station.html
    has no room list), and a fold remembered on /s/{slug} (glosa.roomNav,
    same origin) reaches the station too: room.js must still draw."""
    out = _draw([
        {"type": "append", "seg": 0, "text": "Hola a todos."},
        {"type": "close", "seg": 0},
    ], station=True, storage={"glosa.roomNav": "closed"})
    assert out["url"] == "/api/stream/r1/es"
    assert out["phrases"] == [{"seg": 0, "text": "Hola a todos.", "open": False}]


def test_summary_button_stays_hidden_with_no_summary_yet() -> None:
    """GET /api/summary/{slug}/{lang} 404s (no fetch response override: the
    harness's default): the button is hidden and disabled."""
    out = _draw([])["summary"]
    assert out["fetchCalls"] >= 1  # the page polls on load (and again once the talk is announced)
    assert out["toggleHidden"] is True
    assert out["ariaDisabled"] == "true"
    assert out["panelOpen"] is False


def test_clicking_the_button_opens_the_panel_with_the_bullets_and_ago_text() -> None:
    generated_at = time.time() - 125  # ~2 min ago
    out = _draw(
        [],
        fetch={"status": 200, "body": {"talk_id": "t1", "generated_at": generated_at, "bullets": ["Uno", "Dos"]}},
        click="summary",
    )["summary"]

    assert out["toggleHidden"] is False
    assert out["ariaDisabled"] == "false"
    assert out["ariaExpanded"] == "true"
    assert out["panelOpen"] is True
    assert out["scrimOpen"] is True
    assert out["bullets"] == ["Uno", "Dos"]
    assert out["ago"] == STRINGS["es"]["summary_ago"].format(n=2)
    assert out["fetchCalls"] >= 2  # at least the initial poll, then one more on open (freshest)


def test_escape_closes_the_open_panel() -> None:
    out = _draw(
        [],
        fetch={"status": 200, "body": {"talk_id": "t1", "generated_at": time.time(), "bullets": ["Uno"]}},
        click="summary",
        keys=["Escape"],
    )["summary"]

    assert out["panelOpen"] is False
    assert out["scrimOpen"] is False
    assert out["ariaExpanded"] == "false"


def test_the_close_button_also_dismisses_the_panel() -> None:
    out = _draw(
        [],
        fetch={"status": 200, "body": {"talk_id": "t1", "generated_at": time.time(), "bullets": ["Uno"]}},
        click=["summary", "summary-close"],
    )["summary"]

    assert out["panelOpen"] is False
    assert out["scrimOpen"] is False
    assert out["ariaExpanded"] == "false"


# ---- Picture-in-Picture captions (Task 20) -------------------------------------------


def test_pip_button_stays_hidden_without_the_documentpictureinpicture_api() -> None:
    """No `window.documentPictureInPicture` faked (input.pip omitted): the
    same as a phone, Safari or Firefox today -- no broken button."""
    out = _draw([])["pip"]
    assert out["hidden"] is True
    assert out["opened"] == 0


def test_clicking_pip_opens_the_window_with_the_current_captions() -> None:
    out = _draw(
        [
            {"type": "set", "seg": 0, "text": "Hola a"},
            {"type": "set", "seg": 0, "text": "Hola a todos."},
            {"type": "close", "seg": 0},
            {"type": "set", "seg": 1, "text": "Bienvenidos"},
            {"type": "set", "seg": 1, "text": "Bienvenidos a Nerdearla"},
        ],
        pip=True,
        click="pip",
    )["pip"]

    assert out["hidden"] is False
    assert out["opened"] == 1
    assert out["closed"] is False
    assert out["ariaPressed"] == "true"
    assert out["lines"], "the mirror should show the live caption line"
    assert "Hola a todos. Bienvenidos a Nerdearla" in out["lines"][-1]


def test_closing_the_pip_window_restores_the_page_and_flips_aria_pressed() -> None:
    """Clicking the button again closes the pop-out (pipWindow.close(), which
    fires "pagehide" the same way the reader closing it via the OS chrome
    would) -- no second window is opened, and the main transcript was never
    moved out of the page in the first place, so there is nothing to put
    back."""
    out = _draw([], pip=True, click=["pip", "pip"])["pip"]

    assert out["opened"] == 1  # the second click closed the same window, it did not open another
    assert out["closed"] is True
    assert out["ariaPressed"] == "false"
    assert out["mainTranscriptIntact"] is True


def test_pip_mirrors_the_reader_theme_onto_the_pop_out_document() -> None:
    """Theme/text size "follow the main page" (spec): a theme picked before
    the pop-out opens is mirrored onto its own document's root, since it
    carries its own copy of the stylesheet ([data-theme] selectors)."""
    out = _draw([], pip=True, click=["theme", "pip"])["pip"]

    assert out["theme"] == "light"   # cycleTheme(): system -> light


# ---- the draggable "¿Qué me perdí?" panel (user feedback) -------------------------------


def test_clamp_keeps_the_panel_fully_inside_the_viewport() -> None:
    assert _clamp(700, 700, 300, 200, 800, 600) == {"x": 492, "y": 392}  # bottom/right edge
    assert _clamp(-50, -50, 300, 200, 800, 600) == {"x": 8, "y": 8}      # top/left edge
    assert _clamp(100, 100, 300, 200, 800, 600) == {"x": 100, "y": 100}  # already inside: untouched


def test_clamp_centres_a_panel_bigger_than_the_viewport() -> None:
    """The margin alone can't be honoured both sides -- max() below the min()
    keeps the panel on screen rather than pushed off by its own size."""
    out = _clamp(50, 50, 900, 900, 800, 600, margin=8)
    assert out == {"x": 8, "y": 8}


def test_the_drag_handle_has_the_accessible_name_to_move_the_panel() -> None:
    out = _draw([])["drag"]
    assert out["handleLabel"] == "Mover"


def test_arrow_keys_on_the_handle_move_the_panel_and_remember_it() -> None:
    out = _draw(
        [], keys=[{"key": "ArrowRight", "on": "[data-summary-drag-handle]"},
                  {"key": "ArrowDown", "on": "[data-summary-drag-handle]"}],
    )["drag"]

    assert out["left"] == "16px"
    assert out["top"] == "24px"
    assert out["stored"] == {"x": 16, "y": 24}   # persisted (localStorage) for next visit


def test_home_resets_the_dragged_position_and_forgets_it() -> None:
    out = _draw(
        [], keys=[{"key": "ArrowRight", "on": "[data-summary-drag-handle]"},
                  {"key": "Home", "on": "[data-summary-drag-handle]"}],
    )["drag"]

    assert out["left"] == ""
    assert out["top"] == ""
    assert out["stored"] is None


def test_a_remembered_position_from_an_earlier_visit_is_restored_on_load() -> None:
    out = _draw([], drag_stored={"x": 40, "y": 60})["drag"]

    assert out["left"] == "40px"
    assert out["top"] == "60px"


def test_a_corrupt_stored_position_is_ignored() -> None:
    """store.get()/JSON.parse() wrapped in try/catch (room.js): a malformed
    value falls back to the CSS default instead of crashing the page."""
    out = _draw([], drag_stored="not-json")["drag"]  # the harness JSON.stringifies this as-is

    assert out["left"] == ""
    assert out["top"] == ""


def test_below_600px_only_the_vertical_offset_is_draggable() -> None:
    """Narrow phones (< 600 px): the panel stays the full-width sheet it
    always was -- room.js only ever sets its `top`, never `left`."""
    out = _draw(
        [], viewport={"width": 400, "height": 800},
        keys=[{"key": "ArrowRight", "on": "[data-summary-drag-handle]"},
              {"key": "ArrowDown", "on": "[data-summary-drag-handle]"}],
    )["drag"]

    assert out["left"] == ""
    assert out["top"] == "24px"


def test_a_dragged_position_is_re_clamped_on_resize() -> None:
    out = _draw([], drag_stored={"x": 900, "y": 500}, resize={"width": 500, "height": 400})["drag"]

    assert out["stored"] == {"x": 492, "y": 392}
    assert out["top"] == "392px"
    assert out["left"] == ""   # the resize also crossed below the 600px sheet threshold


def test_arrow_keys_elsewhere_do_not_move_the_panel() -> None:
    """Only keydown on the handle itself drives the drag (event.target, same
    way the existing Esc-closes-the-summary and f/+/- shortcuts work)."""
    out = _draw([], keys=["ArrowRight"])["drag"]

    assert out["left"] == ""
    assert out["stored"] is None
