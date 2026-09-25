"""LocalTranslator (Task 16, engine_mode "local"): translate()'s
signature/behavior parity with glosa.text.translator.Translator, using a
fake generate callable injected -- no mlx_lm needed to run these tests
(Linux-safe)."""

from __future__ import annotations

import asyncio
import sys

import pytest

from glosa.clock import FakeClock
from glosa.config import ConfigError
from glosa.models import GlossaryTerm
from glosa.text.local_translator import LocalTranslator, _SharedTranslateModel, reset_shared_model
from glosa.text.translator import Translation


@pytest.fixture(autouse=True)
def _fresh_shared_model():
    reset_shared_model()
    yield
    reset_shared_model()


async def test_translate_calls_generate_with_text_source_and_target_and_returns_usd_zero() -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_generate(text: str, source_lang: str, target_lang: str) -> str:
        calls.append((text, source_lang, target_lang))
        return f"[{target_lang}] {text}"

    clock = FakeClock()
    clock.advance(1.0)
    translator = LocalTranslator(source_lang="en", generate=fake_generate, clock=clock)

    result = await translator.translate("Hello there.", "es", [], [])

    assert calls == [("Hello there.", "en", "es")]
    assert isinstance(result, Translation)
    assert result.text == "[es] Hello there."
    assert result.usd == 0.0
    assert result.latency_s == 0.0  # FakeClock never advanced during the call


async def test_glossary_and_context_are_accepted_but_not_forwarded_to_generate() -> None:
    """TranslateGemma's prompt format has no slot for extra instructions
    (see the module docstring): both are accepted for signature parity with
    Translator and silently ignored."""
    calls: list[tuple[str, str, str]] = []
    translator = LocalTranslator(
        source_lang="es", generate=lambda text, s, t: calls.append((text, s, t)) or "ok"
    )
    glossary = [GlossaryTerm("Kubernetes", True)]
    context = [("previous segment", "previous translation")]

    result = await translator.translate("segmento", "en", glossary, context)

    assert calls == [("segmento", "es", "en")]
    assert result.text == "ok"


async def test_source_lang_is_fixed_at_construction_not_per_call() -> None:
    calls: list[tuple[str, str, str]] = []
    translator = LocalTranslator(
        source_lang="es", generate=lambda text, s, t: calls.append((text, s, t)) or "x"
    )
    await translator.translate("uno", "en", [], [])
    await translator.translate("dos", "fr", [], [])
    assert [c[1] for c in calls] == ["es", "es"]
    assert [c[2] for c in calls] == ["en", "fr"]


async def test_aclose_is_a_safe_no_op() -> None:
    translator = LocalTranslator(source_lang="en", generate=lambda text, s, t: "x")
    await translator.aclose()  # must not raise; no HTTP connections to release


async def test_lazy_import_without_the_local_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mlx_lm", None)  # simulates "not installed"
    with pytest.raises(ConfigError, match="local"):
        LocalTranslator(source_lang="en")


async def test_building_with_a_fake_generate_never_touches_mlx_lm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mlx_lm", None)
    translator = LocalTranslator(source_lang="en", generate=lambda text, s, t: "ok")  # must not raise
    result = await translator.translate("hi", "es", [], [])
    assert result.text == "ok"


async def test_two_translators_share_one_process_wide_model() -> None:
    t1 = LocalTranslator(source_lang="en", generate=lambda text, s, t: "from t1")
    t2 = LocalTranslator(source_lang="es", generate=lambda text, s, t: "from t2 (never used)")

    r1 = await t1.translate("hi", "es", [], [])
    r2 = await t2.translate("hola", "en", [], [])

    assert r1.text == "from t1"
    assert r2.text == "from t1"  # t1's fake won the race for the shared singleton


async def test_shared_model_serializes_calls_with_a_lock() -> None:
    def slow(text: str, source_lang: str, target_lang: str) -> str:
        import time

        time.sleep(0.02)
        return text

    model = _SharedTranslateModel(slow)
    results = await asyncio.gather(
        model.generate("a", "en", "es"), model.generate("b", "en", "es"), model.generate("c", "en", "es")
    )
    assert set(results) == {"a", "b", "c"}
    assert model.max_calls_in_flight == 1


# ---- shutdown of the shared MLX thread (task-16-review.md Important #1) --------


async def test_shutdown_shared_model_stops_the_mlx_thread_and_drops_the_singleton() -> None:
    import glosa.text.local_translator as local_translator

    model = await local_translator._get_shared_model(lambda text, src, tgt: text)
    assert await model.generate("hola", "es", "en") == "hola"
    threads = list(model._executor._threads)
    assert threads and all(t.is_alive() for t in threads)

    await local_translator.shutdown_shared_model(timeout=2.0)

    assert not any(t.is_alive() for t in threads)
    assert local_translator._shared_model is None
    await local_translator.shutdown_shared_model(timeout=2.0)  # a no-op
