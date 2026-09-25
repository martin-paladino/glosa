"""Unit tests for the pure parts of the Task 15c bench (bench/json3.py,
bench/terms.py, bench/report.py): json3 parsing/offsets, latency
computation on synthetic timelines, term matching (accents/case), and
results-table rendering. No network.

bench/bench.py's own orchestration (run_one, live_run, dry_run, ...) is not
unit-tested here: it only ever does real I/O (RoomWorker, ffmpeg, the
Gemini API), which the task brief keeps out of the always-green test suite
("No network in tests"). Its pure helpers (``parse_only``, the --only
filter, and ``to_glossary_terms``, the terms.yaml -> Talk.glossary
conversion) are tested directly, network-free.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bench.bench import CLIPS, ENGINES, parse_only, reconstruct_final_text, to_glossary_terms
from bench.json3 import (
    PROGRESS_POINTS,
    Word,
    full_text,
    load_words,
    pct,
    progress_lag,
    screen_curve,
    spoken_curve,
    time_at_fraction,
)
from bench.report import RunStats, render_table
from bench.terms import Term, TermReport, best_count, count_occurrences, load_terms, normalize, overall_pct, score_terms

# --------------------------------------------------------------------- json3


def _json3(path: Path, events: list[tuple[int, list[tuple[str, int | None]]]]) -> Path:
    """events: (tStartMs, [(utf8, tOffsetMs_or_None), ...])."""
    data = {
        "events": [
            {
                "tStartMs": t_start,
                "segs": [{"utf8": text, **({"tOffsetMs": off} if off is not None else {})} for text, off in segs],
            }
            for t_start, segs in events
        ]
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_words_reads_json3_word_offsets_and_sorts_by_time(tmp_path: Path) -> None:
    path = _json3(
        tmp_path / "x.json3",
        [
            (1000, [("hello", None), (" world", 200)]),  # 1.0s, 1.2s
            (0, [("first", None)]),  # 0.0s -- out of file order, must sort
            (500, [("", None)]),  # blank segment: dropped
        ],
    )
    words = load_words(path)
    assert words == [
        Word(text="first", start_s=0.0),
        Word(text="hello", start_s=1.0),
        Word(text="world", start_s=1.2),
    ]


def test_full_text_joins_words_in_speaking_order() -> None:
    assert full_text([Word("a", 0.0), Word("b", 1.0)]) == "a b"


def test_pct_nearest_rank() -> None:
    values = [float(v) for v in range(1, 11)]  # 1..10
    assert pct(values, 0.5) == 5.0
    assert pct(values, 0.9) == 9.0


def test_pct_empty_is_nan() -> None:
    assert math.isnan(pct([], 0.5))


# ------------------------------------------------------------- progress-lag


def test_spoken_curve_is_cumulative_word_count_over_time() -> None:
    words = [Word("a", 0.0), Word("b", 1.0), Word("c", 1.5)]
    assert spoken_curve(words) == [(0.0, 1), (1.0, 2), (1.5, 3)]


def test_screen_curve_append_accumulates_within_a_segment() -> None:
    events = [
        {"t": 1.0, "lang": "en", "type": "append", "seg": 0, "text": "hello "},
        {"t": 2.0, "lang": "en", "type": "append", "seg": 0, "text": "world"},
    ]
    assert screen_curve(events, "en") == [(1.0, 1), (2.0, 2)]


def test_screen_curve_set_replaces_not_appends() -> None:
    # A `set` carries the segment's WHOLE current text (VERBATIM transcribe
    # interim revisions): counting it as a fresh append on top of the
    # previous `set` would double-count words already on screen.
    events = [
        {"t": 1.0, "lang": "en", "type": "set", "seg": 0, "text": "hello"},
        {"t": 2.0, "lang": "en", "type": "set", "seg": 0, "text": "hello world"},
    ]
    assert screen_curve(events, "en") == [(1.0, 1), (2.0, 2)]  # not (2.0, 1 + 2 = 3)


def test_screen_curve_running_max_when_a_set_revises_the_text_shorter() -> None:
    events = [
        {"t": 1.0, "lang": "en", "type": "set", "seg": 0, "text": "hello world"},  # 2 words
        {"t": 2.0, "lang": "en", "type": "set", "seg": 0, "text": "hi"},  # ASR revises down to 1
    ]
    assert screen_curve(events, "en") == [(1.0, 2), (2.0, 2)]  # stays at the running max, not 1


def test_screen_curve_sums_open_segments_and_ignores_close_and_other_langs() -> None:
    events = [
        {"t": 1.0, "lang": "en", "type": "append", "seg": 0, "text": "a b"},
        {"t": 1.2, "lang": "es", "type": "append", "seg": 0, "text": "otro idioma"},  # different lang: ignored
        {"t": 1.5, "lang": "en", "type": "close", "seg": 0},  # no text: no new point
        {"t": 2.0, "lang": "en", "type": "append", "seg": 1, "text": "c"},
    ]
    assert screen_curve(events, "en") == [(1.0, 2), (2.0, 3)]


def test_screen_curve_empty_when_the_track_has_no_events() -> None:
    assert screen_curve([], "en") == []
    assert screen_curve([{"t": 1.0, "lang": "es", "type": "append", "seg": 0, "text": "hola"}], "en") == []


def test_time_at_fraction_earliest_point_reaching_the_threshold() -> None:
    curve = [(0.0, 1), (1.0, 3), (2.0, 5)]  # total = 5
    assert time_at_fraction(curve, 0.5) == 1.0  # needs >= 2.5 -> first point with count >= 2.5
    assert time_at_fraction(curve, 1.0) == 2.0


def test_time_at_fraction_empty_curve_is_none() -> None:
    assert time_at_fraction([], 0.5) is None


def test_progress_points_are_5pct_steps_from_5_to_95() -> None:
    assert PROGRESS_POINTS[0] == pytest.approx(0.05)
    assert PROGRESS_POINTS[-1] == pytest.approx(0.95)
    assert len(PROGRESS_POINTS) == 19


def test_progress_lag_constant_delay_gives_lag_approximately_equal_to_the_delay() -> None:
    # 10 words spoken one per second; each appears on screen, as its own
    # segment, exactly 2.5s after it was spoken.
    words = [Word(str(i), float(i)) for i in range(10)]
    spoken = spoken_curve(words)
    events = [{"t": float(i) + 2.5, "lang": "en", "type": "append", "seg": i, "text": "w"} for i in range(10)]
    screen = screen_curve(events, "en")
    p50, p90, n = progress_lag(spoken, screen)
    assert p50 == pytest.approx(2.5)
    assert p90 == pytest.approx(2.5)
    assert n == len(PROGRESS_POINTS)


def test_progress_lag_none_when_a_track_has_no_events() -> None:
    spoken = spoken_curve([Word("a", 0.0)])
    assert progress_lag(spoken, []) == (None, None, 0)
    assert progress_lag([], []) == (None, None, 0)


# --------------------------------------------------------- text reconstruction


def test_reconstruct_final_text_concatenates_segments_in_order() -> None:
    events = [
        {"t": 2.0, "lang": "es", "type": "append", "seg": 1, "text": "mundo"},
        {"t": 1.0, "lang": "es", "type": "append", "seg": 0, "text": "hola"},
        {"t": 1.5, "lang": "es", "type": "close", "seg": 0},
        {"t": 0.5, "lang": "en", "type": "append", "seg": 0, "text": "other track"},
    ]
    assert reconstruct_final_text(events, "es") == "hola mundo"


def test_reconstruct_final_text_set_keeps_only_the_latest_text() -> None:
    events = [
        {"t": 1.0, "lang": "en", "type": "set", "seg": 0, "text": "So,"},
        {"t": 2.0, "lang": "en", "type": "set", "seg": 0, "text": "So, what"},
    ]
    assert reconstruct_final_text(events, "en") == "So, what"


# --------------------------------------------------------------------- terms


def test_normalize_strips_accents_and_casefolds() -> None:
    assert normalize("Ambigüedad") == normalize("AMBIGUEDAD") == "ambiguedad"


def test_count_occurrences_is_case_accent_insensitive_substring() -> None:
    assert count_occurrences("Namespace namespaces", "namespace") == 2  # "namespaces" contains "namespace"
    assert count_occurrences("ambigüedad total", "ambiguedad") == 1
    assert count_occurrences("nothing here", "namespace") == 0


def test_best_count_takes_the_max_not_the_sum_of_alternate_spellings() -> None:
    text = "we migrated to Asure last year"
    assert best_count(text, ("azure", "asure")) == 1  # not 1 (azure) + 1 (asure) = 2


def test_load_terms_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "terms.yaml"
    path.write_text(
        "clip_a:\n"
        "  - term: agents\n"
        "    source_patterns: [agent]\n"
        "    targets: [agente]\n"
        "  - term: namespaces\n"
        "    targets: [namespace]\n"
        "    keep_in_english: true\n",
        encoding="utf-8",
    )
    terms = load_terms(path, "clip_a")
    assert terms == [
        Term(term="agents", source_patterns=("agent",), targets=("agente",), keep_in_english=False),
        Term(term="namespaces", source_patterns=("namespaces",), targets=("namespace",), keep_in_english=True),
    ]


def test_load_terms_missing_clip_key_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "terms.yaml"
    path.write_text("clip_a: []\n", encoding="utf-8")
    assert load_terms(path, "clip_b") == []


def test_score_terms_counts_occurrences_and_hits() -> None:
    terms = [Term(term="agents", source_patterns=("agent",), targets=("agente",))]
    source_text = "the agent and the agents talked"  # "agent" (1) + "agents" contains "agent" (1) = 2
    translated_text = "el agente y los agentes hablaron"  # "agente" (1) + "agentes" contains "agente" (1) = 2
    reports = score_terms(terms, source_text, translated_text)
    assert reports == {"agents": TermReport(occurrences=2, hits=2)}
    assert overall_pct(reports) == 100.0


def test_score_terms_hits_capped_at_occurrences() -> None:
    terms = [Term(term="loki", source_patterns=("loki",), targets=("loki",))]
    reports = score_terms(terms, "loki appears once", "loki loki loki")
    assert reports["loki"].occurrences == 1
    assert reports["loki"].hits == 1  # capped, not 3


def test_overall_pct_is_nan_with_zero_occurrences() -> None:
    terms = [Term(term="grafana", source_patterns=("grafana",), targets=("grafana",))]
    reports = score_terms(terms, "no mention of that tool", "translation")
    assert math.isnan(overall_pct(reports))


# -------------------------------------------------------------------- report


def _stats(**overrides) -> RunStats:
    base = dict(
        clip="en_clip", engine="fast",
        source_lat_p50=None, source_lat_p90=None, source_lat_n=0,
        trans_lat_p50=1.2, trans_lat_p90=2.4, trans_lat_n=10,
        fidelity=4, fluency=5, justification="reads naturally",
        term_pct=80.0, term_occurrences=10, term_hits=8,
        cost_usd=0.0368, cost_per_hour=2.21,
    )
    base.update(overrides)
    return RunStats(**base)


def test_render_table_marks_a_missing_track_lag_as_em_dash() -> None:
    table = render_table([_stats()])
    assert " — " in table  # "—" == "—"
    assert "| en_clip | fast |" in table


def test_render_table_formats_glossary_row_with_numbers() -> None:
    glossary = _stats(
        engine="glossary", source_lat_p50=0.8, source_lat_p90=1.4, source_lat_n=12, term_pct=95.0,
    )
    table = render_table([glossary])
    assert "0.80 / 1.40 (n=12)" in table
    assert "95%" in table


def test_render_table_pct_nan_renders_as_na() -> None:
    stats = _stats(term_pct=float("nan"))
    table = render_table([stats])
    lines = [line for line in table.splitlines() if line.startswith("| en_clip")]
    assert len(lines) == 1
    assert " n/a " in lines[0]


# -------------------------------------------------------------------- bench.py


def test_parse_only_defaults_to_every_clip_engine_combination() -> None:
    assert parse_only(None) == [(clip["name"], engine) for clip in CLIPS for engine in ENGINES]
    assert parse_only([]) == parse_only(None)  # empty is also "no filter"


def test_parse_only_filters_to_the_given_pairs_preserving_default_order() -> None:
    combos = parse_only(["es_clip:fast", "en_clip:glossary"])
    assert combos == [("en_clip", "glossary"), ("es_clip", "fast")]


def test_to_glossary_terms_keep_in_english_has_no_translation() -> None:
    terms = [Term(term="Grafana", source_patterns=("Grafana",), targets=("Grafana",), keep_in_english=True)]
    [glossary_term] = to_glossary_terms(terms)
    assert glossary_term.keep_in_english is True
    assert glossary_term.translation is None


def test_to_glossary_terms_translated_gets_an_explicit_translation() -> None:
    # Regression: glosa/text/translator.py's _build_system_instruction treats
    # keep_in_english=False *without* a translation the same as
    # keep_in_english=True ("leave it as is, untranslated") -- a term meant
    # to be translated must carry one, or the glossary engine's translator
    # is silently told to keep it in English (the bug this test guards).
    terms = [Term(term="agents", source_patterns=("agent",), targets=("agente", "agentes"), keep_in_english=False)]
    [glossary_term] = to_glossary_terms(terms)
    assert glossary_term.keep_in_english is False
    assert glossary_term.translation == "agente"  # the first accepted target spelling
