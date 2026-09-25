"""bench/terms.py: glossary-term occurrence matching for the bench's
"quality (glossary terms)" metric, and the loader for bench/terms.yaml.

Matching is case- and accent-insensitive SUBSTRING containment, not a
stemmed/tokenized NLP match. This is a deliberate choice, not a shortcut:
YouTube's json3 auto-captions sometimes run two words together at an event
boundary with no space (e.g. a real example from samples/reference/
es_clip.es.json3: "no entiende de" + "namespaces" -> "denamespaces"), which
a strict \\b-bounded word match would miss; a plain substring still finds
"namespace" inside it. terms.yaml can (and does) lean on the same property
on purpose -- e.g. a stem like "agent" or "capabilit" as the matched form,
so it counts both the singular and the plural without listing each.

Each term carries a list of acceptable spellings on each side
(source_patterns for counting occurrences in the source reference text,
targets for counting hits in an engine's translated output); when a term
has more than one, the reported count is the single best-matching
spelling's count, not their sum -- alternate spellings of the very same
word (e.g. "Loki"/"Loky", "Azure"/"Asure", a caption ASR typo) should not
be double-counted as if they were separate occurrences.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def normalize(text: str) -> str:
    """casefold + strip diacritics (so "ambigüedad"/"ambiguedad", "SRE"/
    "sre" compare equal)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def count_occurrences(text: str, pattern: str) -> int:
    """Non-overlapping, case/accent-insensitive substring count of
    ``pattern`` in ``text``."""
    norm_text = normalize(text)
    norm_pat = normalize(pattern)
    if not norm_pat:
        return 0
    count = 0
    start = 0
    while True:
        idx = norm_text.find(norm_pat, start)
        if idx == -1:
            return count
        count += 1
        start = idx + len(norm_pat)


def best_count(text: str, patterns: tuple[str, ...]) -> int:
    """The highest single pattern's count among ``patterns`` (see module
    docstring: alternate spellings of one word, not summed)."""
    return max((count_occurrences(text, p) for p in patterns), default=0)


@dataclass(frozen=True)
class Term:
    term: str  # canonical form: the talk glossary's vocabulary entry
    source_patterns: tuple[str, ...]  # spellings to count in the source reference text
    targets: tuple[str, ...]  # accepted spellings in the translated output
    keep_in_english: bool = False


@dataclass(frozen=True)
class TermReport:
    occurrences: int
    hits: int


def load_terms(path: str | Path, clip: str) -> list[Term]:
    doc: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = doc.get(clip) or []
    terms: list[Term] = []
    for raw in entries:
        term = raw["term"]
        source_patterns = tuple(raw.get("source_patterns") or [term])
        targets = tuple(raw["targets"])
        terms.append(
            Term(
                term=term,
                source_patterns=source_patterns,
                targets=targets,
                keep_in_english=bool(raw.get("keep_in_english", False)),
            )
        )
    return terms


def score_terms(terms: list[Term], source_text: str, translated_text: str) -> dict[str, TermReport]:
    """Per-term occurrence count (in the source reference) and hit count
    (in the engine's translated output, capped at the occurrence count)."""
    out: dict[str, TermReport] = {}
    for term in terms:
        occurrences = best_count(source_text, term.source_patterns)
        hits = min(best_count(translated_text, term.targets), occurrences) if occurrences else 0
        out[term.term] = TermReport(occurrences=occurrences, hits=hits)
    return out


def overall_pct(reports: dict[str, TermReport]) -> float:
    """% of all terms' source occurrences whose translation contains an
    accepted form; NaN when the term list has zero occurrences (should not
    happen with a well-chosen terms.yaml, but guards a div by zero)."""
    occurrences = sum(r.occurrences for r in reports.values())
    hits = sum(r.hits for r in reports.values())
    return (hits / occurrences * 100.0) if occurrences else float("nan")
