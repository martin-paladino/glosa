"""QualityMeter: window-based source/target pairing (13.1), the no-key path
(13.2), and the mocked-client rolling average (13.3). 13.4 is a single,
timeout-bounded call to the real Jev API and is skipped unless
TYPESAFE_API_KEY is set in /Users/mpaladino/repos/glosa/.env.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

from glosa.quality import FIDELITY_QUESTION, ClosedSegment, QualityMeter, find_matching_source

MAIN_REPO_ENV = Path("/Users/mpaladino/repos/glosa/.env")


def _mock_client(prob: float) -> AsyncMock:
    """A mocked TypeSafe client whose system_one() answers one Noul question."""
    client = AsyncMock()
    client.system_one.return_value = SimpleNamespace(answers={"fidelity": SimpleNamespace(noul=prob)})
    return client


# --------------------------------------------------------- 13.1 window pairing


def test_find_matching_source_picks_the_overlapping_segment() -> None:
    # target window = [t_start - 1.0, t_end + 0.5] = [9.0, 12.5]
    target = ClosedSegment(text="Hola mundo", t_start=10.0, t_end=12.0)
    too_early = ClosedSegment(text="before", t_start=5.0, t_end=8.5)  # ends before window
    too_late = ClosedSegment(text="after", t_start=13.0, t_end=15.0)  # starts after window
    correct = ClosedSegment(text="Hello world", t_start=9.5, t_end=11.5)  # inside window

    match = find_matching_source(target, [too_early, too_late, correct])

    assert match is correct


def test_find_matching_source_prefers_the_larger_overlap() -> None:
    target = ClosedSegment(text="tgt", t_start=10.0, t_end=12.0)  # window = [9.0, 12.5]
    sliver = ClosedSegment(text="sliver", t_start=8.7, t_end=9.1)  # overlaps by 0.1s
    correct = ClosedSegment(text="correct", t_start=9.2, t_end=12.4)  # overlaps by 3.2s

    match = find_matching_source(target, [sliver, correct])

    assert match is correct


def test_find_matching_source_returns_none_when_nothing_overlaps() -> None:
    target = ClosedSegment(text="tgt", t_start=10.0, t_end=12.0)
    far = ClosedSegment(text="far", t_start=0.0, t_end=1.0)

    assert find_matching_source(target, [far]) is None


def test_find_matching_source_returns_none_for_touching_boundary() -> None:
    # ends exactly where the window starts: zero-width overlap, not a match.
    target = ClosedSegment(text="tgt", t_start=10.0, t_end=12.0)  # window = [9.0, 12.5]
    touching = ClosedSegment(text="touching", t_start=7.0, t_end=9.0)

    assert find_matching_source(target, [touching]) is None


# ------------------------------------------------------------------ 13.2 no key


async def test_no_key_means_score_returns_none() -> None:
    meter = QualityMeter(api_key=None)

    assert await meter.score("Hello", "Hola") is None


async def test_no_key_means_no_client_calls() -> None:
    client = AsyncMock()
    meter = QualityMeter(api_key=None, client=client)

    await meter.score("Hello", "Hola")

    client.system_one.assert_not_called()


async def test_no_key_means_avg_is_none() -> None:
    meter = QualityMeter(api_key=None)

    assert meter.avg() is None


# ------------------------------------------------------- 13.3 mocked client, avg


async def test_score_asks_jev_the_exact_fidelity_question() -> None:
    client = _mock_client(0.83)
    meter = QualityMeter(api_key="test-key", client=client)

    score = await meter.score("We deploy with Helm.", "Desplegamos con Helm.")

    assert score == pytest.approx(0.83)
    _, kwargs = client.system_one.call_args
    question = kwargs["questions"]["fidelity"]
    assert question.instructions == FIDELITY_QUESTION
    assert kwargs["model"] == "jev-latest"
    assert "We deploy with Helm." in str(kwargs["state"])
    assert "Desplegamos con Helm." in str(kwargs["state"])


async def test_add_ignores_none_scores() -> None:
    meter = QualityMeter(api_key="test-key", client=AsyncMock())

    meter.add(0.7)
    meter.add(None)

    assert meter.avg() == pytest.approx(0.7)


async def test_rolling_average_of_default_window_10() -> None:
    meter = QualityMeter(api_key="test-key", client=AsyncMock())

    for p in [0.9, 0.9, 0.9]:  # pushed out once the window fills up
        meter.add(p)
    for p in [0.1] * 10:
        meter.add(p)

    assert meter.avg() == pytest.approx(0.1)


async def test_rolling_average_window_is_configurable() -> None:
    meter = QualityMeter(api_key="test-key", client=AsyncMock(), window=2)

    meter.add(0.0)
    meter.add(1.0)
    meter.add(0.5)

    assert meter.avg() == pytest.approx(0.75)  # only the last 2: (1.0 + 0.5) / 2


async def test_avg_is_none_before_any_score_is_added() -> None:
    meter = QualityMeter(api_key="test-key", client=AsyncMock())

    assert meter.avg() is None


async def test_score_then_add_feeds_the_rolling_average() -> None:
    client = _mock_client(0.9)
    meter = QualityMeter(api_key="test-key", client=client, window=2)

    for _ in range(3):
        meter.add(await meter.score("en", "es"))

    assert meter.avg() == pytest.approx(0.9)
    assert client.system_one.await_count == 3


async def test_aclose_closes_a_client_it_created_itself() -> None:
    meter = QualityMeter(api_key="test-key")  # no injected client: builds its own

    await meter.aclose()  # must not raise, and must actually release the real httpx client


async def test_aclose_leaves_an_injected_client_alone() -> None:
    client = AsyncMock()
    meter = QualityMeter(api_key="test-key", client=client)

    await meter.aclose()

    client.aclose.assert_not_called()


async def test_aclose_is_a_noop_without_a_key() -> None:
    meter = QualityMeter(api_key=None)

    await meter.aclose()  # must not raise


# --------------------------------------------------------------- 13.4 live


@pytest.mark.live
async def test_live_good_and_corrupted_pairs_separate() -> None:
    env = dotenv_values(MAIN_REPO_ENV) if MAIN_REPO_ENV.exists() else {}
    api_key = env.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY not set in /Users/mpaladino/repos/glosa/.env")

    good = [
        ("We deploy our Kubernetes operator with Helm.", "Desplegamos nuestro operador de Kubernetes con Helm."),
        ("Thank you all for coming today.", "Gracias a todos por venir hoy."),
        ("Every service owns its data.", "Cada servicio es dueño de sus datos."),
        ("The cluster autoscaler adds nodes under load.", "El autoescalador del cluster agrega nodos bajo carga."),
        ("This API returns a JSON response.", "Esta API devuelve una respuesta en formato JSON."),
    ]
    corrupted = [
        ("We deploy our Kubernetes operator with Helm.", "Desplegamos nuestro operador de Docker con Helm."),  # wrong term
        ("Thank you all for coming today.", "No gracias a nadie por venir hoy."),  # inverted meaning
        ("Every service owns its data and its schema.", "Cada servicio es dueño de sus datos."),  # omission
        ("The cluster autoscaler adds nodes under load.", "El autoescalador del cluster elimina nodos bajo carga."),  # inverted
        ("This API returns a JSON response with 200 items.", "Esta API devuelve una respuesta en formato JSON."),  # omission
    ]

    meter = QualityMeter(api_key=api_key)

    async def run() -> tuple[list[float | None], list[float | None]]:
        try:
            good_scores = [await meter.score(src, tgt) for src, tgt in good]
            bad_scores = [await meter.score(src, tgt) for src, tgt in corrupted]
        finally:
            await meter.aclose()
        return good_scores, bad_scores

    good_scores, bad_scores = await asyncio.wait_for(run(), timeout=30.0)

    assert all(s is not None for s in good_scores)
    assert all(s is not None for s in bad_scores)
    good_avg = sum(good_scores) / len(good_scores)
    bad_avg = sum(bad_scores) / len(bad_scores)
    assert good_avg > 0.6, (good_avg, good_scores)
    assert bad_avg < 0.4, (bad_avg, bad_scores)
