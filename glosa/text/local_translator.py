"""LocalTranslator: Task 16's 100%-local translation collaborator
(``engine_mode: local``) -- TranslateGemma
(``mlx-community/translategemma-4b-it-4bit``) via ``mlx_lm``. No cloud API,
no API key, ``usd`` is always 0.

Same ``translate(segment, target, glossary, context) -> Translation``
signature as ``glosa.text.translator.Translator`` (and returns the same
``Translation`` type), so ``glosa/room_text.py``'s ``TranslationLane`` /
``LivePipeline`` wiring works completely unchanged.

Prompt format: TranslateGemma's chat template (``chat_template.jinja`` on
the model repo, read 2026-09-25) takes exactly one structured ``content``
item per user turn -- ``{"type": "text", "source_lang_code": ...,
"target_lang_code": ..., "text": ...}`` -- and builds the whole instruction
itself; a "system" role is not accepted and a user turn's ``content`` must
be exactly that one item, so there is no slot in the template to splice in
extra free-text instructions.

Glossary and context: for this reason (verified against the real template,
not guessed), this local mode does NOT use the talk's glossary for
translation, nor the previous couple of segments Translator normally passes
for continuity -- both parameters are accepted (for signature parity) and
ignored. This mirrors Parakeet's own lack of a custom-vocabulary hook in
local mode (noted in glosa/engines/local.py): local mode is a demo/fallback,
honestly weaker on terminology and cross-segment continuity than the cloud
"glossary" engine. A per-term few-shot turn was considered but would need
extra generation turns per glossary term on every segment -- not worth the
added latency/complexity for a lowest-priority hackathon fallback.

Stopping: the model's own ``generation_config.json`` lists only ``<eos>``
(token id 1) as an end-of-sequence token, not ``<end_of_turn>`` (id varies
by tokenizer but is looked up by name below) -- confirmed empirically
2026-09-25: without fixing this, mlx_lm.generate keeps sampling
``<end_of_turn>`` tokens until ``max_tokens``, correct text but ~6s wasted
per call. Adding the ``<end_of_turn>`` id(s) to the tokenizer's
``eos_token_ids`` (a plain mutable set) once, right after load, fixes this
(~0.2-1s per short segment instead); the trailing token is stripped again
below as a cheap belt-and-suspenders in case a future tokenizer revision
changes IDs.

Lazy import + one shared model instance per process: same rationale as
glosa/engines/local.py's _SharedParakeetModel -- this module must import
without mlx_lm installed, building a real LocalTranslator without the
``local`` extra raises ConfigError immediately, and every room's
LocalTranslator shares ONE loaded model + one asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from glosa.clock import Clock, RealClock
from glosa.config import ConfigError
from glosa.models import GlossaryTerm
from glosa.text.translator import Translation

log = logging.getLogger(__name__)

TRANSLATEGEMMA_MODEL_ID = "mlx-community/translategemma-4b-it-4bit"
MAX_TOKENS = 200  # a caption segment's translation; bounds worst-case latency if stopping ever fails

# Blocking; (text, source_lang, target_lang) -> translated text. Always
# called off the event loop (asyncio.to_thread), one call in flight at a
# time (_SharedTranslateModel's lock).
GenerateFn = Callable[[str, str, str], str]

_shared_model: "_SharedTranslateModel | None" = None
_shared_model_lock = asyncio.Lock()


def _check_available() -> None:
    try:
        import mlx_lm  # noqa: F401
    except ImportError as exc:
        raise ConfigError(
            "engine_mode 'local' needs the optional 'local' extra (parakeet-mlx, mlx-lm; "
            "Apple silicon only): install with `uv sync --extra local`"
        ) from exc


def _load_generate_fn() -> GenerateFn:
    """The real generate function, backed by one loaded TranslateGemma
    model. Blocking (model load can take ~1 minute); always called via
    ``asyncio.to_thread``."""
    from mlx_lm import generate as mlx_generate
    from mlx_lm import load as mlx_load

    model, tokenizer = mlx_load(TRANSLATEGEMMA_MODEL_ID)
    end_of_turn_ids = tokenizer.encode("<end_of_turn>", add_special_tokens=False)
    tokenizer.eos_token_ids.update(end_of_turn_ids)  # see module docstring: stop generating well past the answer

    def do_generate(text: str, source_lang: str, target_lang: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "source_lang_code": source_lang,
                        "target_lang_code": target_lang,
                        "text": text,
                    }
                ],
            }
        ]
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        response = mlx_generate(model, tokenizer, prompt=prompt, max_tokens=MAX_TOKENS, verbose=False)
        return response.split("<end_of_turn>", 1)[0].strip()

    return do_generate


class _SharedTranslateModel:
    """Process-wide: the one loaded model (or an injected fake) and the one
    lock serializing every call into it."""

    def __init__(self, generate_fn: GenerateFn | None) -> None:
        self._generate_fn = generate_fn  # None: real model, loaded lazily on first use
        self._call_lock = asyncio.Lock()
        self.calls_in_flight = 0
        self.max_calls_in_flight = 0

    async def generate(self, text: str, source_lang: str, target_lang: str) -> str:
        async with self._call_lock:
            self.calls_in_flight += 1
            self.max_calls_in_flight = max(self.max_calls_in_flight, self.calls_in_flight)
            try:
                if self._generate_fn is None:
                    self._generate_fn = await asyncio.to_thread(_load_generate_fn)
                return await asyncio.to_thread(self._generate_fn, text, source_lang, target_lang)
            finally:
                self.calls_in_flight -= 1


async def _get_shared_model(generate_fn: GenerateFn | None) -> _SharedTranslateModel:
    global _shared_model
    async with _shared_model_lock:
        if _shared_model is None:
            _shared_model = _SharedTranslateModel(generate_fn)
        return _shared_model


def reset_shared_model() -> None:
    """Test-only: drop the process-wide singleton."""
    global _shared_model
    _shared_model = None


class LocalTranslator:
    def __init__(
        self,
        *,
        source_lang: str,
        generate: GenerateFn | None = None,
        clock: Clock | None = None,
    ) -> None:
        # TranslateGemma's chat template needs the SOURCE language too (not just
        # the target): the talk's spoken language, fixed for this Translator's
        # whole run (RoomWorker builds one LocalTranslator's translate() per
        # run -- see glosa/web/app.py's make_local_translate).
        self._source_lang = source_lang
        self._injected_generate = generate
        if generate is None:
            _check_available()  # fail now, synchronously -- not on first real use
        self._clock: Clock = clock if clock is not None else RealClock()
        self._model: _SharedTranslateModel | None = None

    async def translate(
        self,
        segment: str,
        target: str,
        glossary: list[GlossaryTerm],
        context: list[tuple[str, str | None]],
    ) -> Translation:
        # glossary and context are accepted for signature parity with
        # Translator and intentionally unused -- see the module docstring.
        if self._model is None:
            self._model = await _get_shared_model(self._injected_generate)
        start = self._clock.now()
        text = await self._model.generate(segment, self._source_lang, target)
        return Translation(text=text, latency_s=self._clock.now() - start, usd=0.0)

    async def aclose(self) -> None:
        """No HTTP connections to release (mirrors Translator.aclose() so
        glosa/room.py can call it unconditionally on teardown)."""
        return None
