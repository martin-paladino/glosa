"""bench/report.py: the per-run stat record and the results table renderer
-- pure, network-free, so it is directly unit-testable (Task 15c's "results
table rendering" requirement)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunStats:
    clip: str
    engine: str  # "fast" | "glossary"
    # progress-lag p50/p90 (bench/json3.py's progress_lag), seconds; None
    # only when the track published no events at all (not "fast has no
    # source track" -- it turns out fast DOES publish one, see
    # task-15c-fix1.md and bench/results.md's Method section).
    source_lat_p50: float | None
    source_lat_p90: float | None
    source_lat_n: int  # number of PROGRESS_POINTS matched (0 if no events)
    trans_lat_p50: float | None
    trans_lat_p90: float | None
    trans_lat_n: int
    fidelity: int
    fluency: int
    justification: str
    term_pct: float  # NaN if the clip's term list has zero occurrences
    term_occurrences: int
    term_hits: int
    cost_usd: float
    cost_per_hour: float


def _fmt_lat(p50: float | None, p90: float | None, n: int) -> str:
    if p50 is None:
        return "—"  # em dash: "this track has no events" (task-15c-fix1.md)
    return f"{p50:.2f} / {p90:.2f} (n={n})"


def _fmt_pct(pct: float) -> str:
    return "n/a" if pct != pct else f"{pct:.0f}%"  # pct != pct <=> NaN


def render_table(stats: list[RunStats]) -> str:
    """A GitHub-flavoured Markdown table, one row per run."""
    header = (
        "| clip | engine | source latency p50/p90 (s) | translation latency p50/p90 (s) "
        "| fidelity | fluency | % terms | US$/run | US$/h |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for s in stats:
        rows.append(
            "| {clip} | {engine} | {src} | {trans} | {fid} | {flu} | {pct} | {cost:.4f} | {hourly:.2f} |".format(
                clip=s.clip,
                engine=s.engine,
                src=_fmt_lat(s.source_lat_p50, s.source_lat_p90, s.source_lat_n),
                trans=_fmt_lat(s.trans_lat_p50, s.trans_lat_p90, s.trans_lat_n),
                fid=s.fidelity,
                flu=s.fluency,
                pct=_fmt_pct(s.term_pct),
                cost=s.cost_usd,
                hourly=s.cost_per_hour,
            )
        )
    return header + "\n".join(rows) + "\n"
