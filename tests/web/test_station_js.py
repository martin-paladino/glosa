"""station.js's pure helper functions (Ruling 62: standby URL building and
the close-code -> next-action decision) under Node
(tests/web/station_js_harness.js). Skipped where Node is not installed.

station.js has no DOM/WebSocket/AudioContext harness (unlike
room.js/room_js_harness.js) -- so, per the brief's own fallback, only the
two pure decisions round 2 adds are exercised here. Everything else is a
manual check: open a station's URL in two tabs, confirm the second one
4409-closes the first (which shows the "replaced"/standby state, not the
setup screen); close the second tab and, within ~10 s, watch the first
recover into a normal "Audio OK" connection on its own, with no click and
no audible glitch on whichever tab was actually capturing the room's mic
the whole time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("station_js_harness.js")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node.js to run station.js")


def _call(fn: str, *args: object) -> object:
    payload = {"fn": fn, "args": list(args)}
    done = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(payload),
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(done.stdout)["result"]


def test_station_ws_url_has_no_standby_param_by_default() -> None:
    url = _call("stationWsUrl", "/ws/station/r1?key=abc", "https://host.example/station/r1", False)
    assert url == "wss://host.example/ws/station/r1?key=abc"


def test_station_ws_url_downgrades_to_ws_over_plain_http() -> None:
    url = _call("stationWsUrl", "/ws/station/r1?key=abc", "http://host.example/station/r1", False)
    assert url == "ws://host.example/ws/station/r1?key=abc"


def test_station_ws_url_standby_adds_the_standby_query_param() -> None:
    url = _call("stationWsUrl", "/ws/station/r1?key=abc", "https://host.example/station/r1", True)
    assert url == "wss://host.example/ws/station/r1?key=abc&standby=1"


def test_next_station_action_is_standby_on_4409_and_reconnect_otherwise() -> None:
    assert _call("nextStationAction", 4409) == "standby"
    assert _call("nextStationAction", 1000) == "reconnect"
    assert _call("nextStationAction", 1006) == "reconnect"
