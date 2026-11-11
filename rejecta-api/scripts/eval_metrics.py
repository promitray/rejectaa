"""Scoring-accuracy metrics for the evaluation set (pure functions, no network or model calls).

Each paper in eval/manifest.yaml has a label:
  strong        published and well regarded; it should score well at the journal it appeared in
  weak          retracted, rejected or clearly flawed; it should score clearly lower than strong papers
  out_of_scope  not a research paper (editorial, news, correction); it must not be desk-rejected for
                lacking novelty, and its article type should be recognised

`paper_record` turns the runs of one paper into one record; `summarize` turns all records into the
metrics below, each with its target, so a run can be compared with the previous one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

STRONG = "strong"
WEAK = "weak"
OUT_OF_SCOPE = "out_of_scope"
LABELS = (STRONG, WEAK, OUT_OF_SCOPE)

SPREAD_LIMIT = 5


@dataclass(frozen=True)
class Target:
    key: str
    title: str
    goal: str  # "max" (value must be <= limit) or "min" (value must be >= limit)
    limit: float
    tolerance: float  # how far a later run may move the wrong way before --compare calls it a regression


TARGETS: tuple[Target, ...] = (
    Target("false_desk_reject_rate", "Strong papers given a desk reject", "max", 0.05, 0.0),
    Target("strong_median_score", "Median score of strong papers at their own journal", "min", 70, 2),
    Target("strong_share_at_least_60", "Share of strong papers scoring 60 or more", "min", 0.90, 0.0),
    Target("auc_strong_vs_weak", "Separation of strong and weak papers (AUC)", "min", 0.80, 0.02),
    Target("median_gap_strong_weak", "Median score gap, strong minus weak", "min", 15, 2),
    Target("spread_ok_share", f"Papers whose score moves <= {SPREAD_LIMIT} points between runs", "min", 0.90, 0.0),
    Target("section_name_stability", "Same section names between runs (mean overlap)", "min", 0.90, 0.02),
    Target("reference_recall", "References extracted / references in the paper", "min", 0.90, 0.0),
    Target("article_type_accuracy", "Article type recognised correctly", "min", 0.95, 0.0),
    Target("methods_found_share", "Methods section found where the paper has one", "min", 0.95, 0.0),
    Target("journal_match_accuracy", "Target journal matched to the right OpenAlex source", "min", 0.95, 0.0),
    Target("false_ghosts_strong", "Ghost references reported on strong papers", "max", 0, 0),
    Target("out_of_scope_desk_reject_rate", "Non-research papers given a desk reject", "max", 0.0, 0.0),
)
TARGETS_BY_KEY = {t.key: t for t in TARGETS}


def auc(positives: list[float], negatives: list[float]) -> float | None:
    """Probability that a random positive scores above a random negative (ties count half)."""
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def _share(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _name_overlap(name_sets: list[set[str]]) -> float | None:
    """Mean overlap (Jaccard) of the section names of every pair of runs; None with fewer than two runs."""
    pairs = [(a, b) for i, a in enumerate(name_sets) for b in name_sets[i + 1 :]]
    if not pairs:
        return None
    return statistics.mean(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs)


def parser_checks(paper: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Compare what the parser found with what the manifest says the paper contains.

    `parsed` holds: article_type, references (count), section_roles (list of roles).
    Returns only the checks the manifest has an expectation for.
    """
    expect = paper.get("expect") or {}
    checks: dict[str, Any] = {}
    if expect.get("article_type"):
        # after a full run, the type the report used (file label, user choice or the model's reading)
        found = parsed.get("analysis_type") or parsed.get("article_type")
        checks["article_type_ok"] = found == expect["article_type"]
    if expect.get("references"):
        checks["reference_recall"] = min(1.0, parsed.get("references", 0) / expect["references"])
    if "has_methods" in expect and expect["has_methods"]:
        checks["methods_found"] = "methods" in (parsed.get("section_roles") or [])
    return checks


def paper_record(paper: dict[str, Any], parsed: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One paper's result over all its runs.

    Each run holds: score, desk_reject (bool), criteria {name: score}, ghost, verified, total,
    journal_name. A paper counts as desk-rejected when most of its runs gave a desk reject.
    """
    scores = [r["score"] for r in runs]
    formats = [r.get("paper_format") for r in runs if r.get("paper_format")]
    if formats:
        parsed = {**parsed, "analysis_type": max(set(formats), key=formats.count)}
    desk_votes = [bool(r.get("desk_reject")) for r in runs]
    expect = paper.get("expect") or {}
    record: dict[str, Any] = {
        "id": paper["id"],
        "label": paper.get("label"),
        "group": paper.get("group"),
        "journal": paper.get("journal"),
        "runs": len(runs),
        "scores": scores,
        "median_score": _median(scores),
        "spread": (max(scores) - min(scores)) if scores else None,
        "section_stability": _name_overlap([set(r.get("sections") or []) for r in runs]),
        "desk_reject": sum(desk_votes) > len(desk_votes) / 2 if desk_votes else None,
        **parser_checks(paper, parsed),
    }
    if runs:
        last = runs[-1]
        record["ghost"] = last.get("ghost", 0)
        record["citations"] = f"{last.get('verified', 0)}/{last.get('total', 0)} verified"
        record["journal_name"] = last.get("journal_name")
        criteria = {}
        for name in runs[0].get("criteria", {}):
            values = [r["criteria"][name] for r in runs if name in r.get("criteria", {})]
            criteria[name] = _median(values)
        record["criteria"] = criteria
        if expect.get("journal_match"):
            record["journal_match_ok"] = (last.get("journal_name") or "").lower() == expect["journal_match"].lower()
    return record


def summarize(records: list[dict[str, Any]]) -> dict[str, float | None]:
    """The headline metrics over all papers. A metric with no papers to judge it is None."""
    strong = [r for r in records if r.get("label") == STRONG and r.get("median_score") is not None]
    weak = [r for r in records if r.get("label") == WEAK and r.get("median_score") is not None]
    oos = [r for r in records if r.get("label") == OUT_OF_SCOPE and r.get("desk_reject") is not None]
    strong_scores = [r["median_score"] for r in strong]
    weak_scores = [r["median_score"] for r in weak]
    strong_median, weak_median = _median(strong_scores), _median(weak_scores)

    def collect(key: str) -> list[Any]:
        return [r[key] for r in records if r.get(key) is not None]

    recalls = collect("reference_recall")
    return {
        "false_desk_reject_rate": _share([r["desk_reject"] for r in strong]),
        "strong_median_score": strong_median,
        "strong_share_at_least_60": _share([s >= 60 for s in strong_scores]),
        "auc_strong_vs_weak": auc(strong_scores, weak_scores),
        "median_gap_strong_weak": (
            strong_median - weak_median if strong_median is not None and weak_median is not None else None
        ),
        "spread_ok_share": _share([s <= SPREAD_LIMIT for s in collect("spread")]),
        "section_name_stability": statistics.mean(collect("section_stability")) if collect("section_stability") else None,
        "reference_recall": statistics.mean(recalls) if recalls else None,
        "article_type_accuracy": _share(collect("article_type_ok")),
        "methods_found_share": _share(collect("methods_found")),
        "journal_match_accuracy": _share(collect("journal_match_ok")),
        "false_ghosts_strong": sum(r.get("ghost", 0) for r in strong) if strong else None,
        "out_of_scope_desk_reject_rate": _share([r["desk_reject"] for r in oos]),
    }


def meets(target: Target, value: float | None) -> bool | None:
    if value is None:
        return None
    return value <= target.limit if target.goal == "max" else value >= target.limit


def compare(before: dict[str, float | None], after: dict[str, float | None]) -> list[dict[str, Any]]:
    """Per-metric change from one run to the next; `regressed` is True when a metric moved the wrong way
    by more than its tolerance."""
    rows = []
    for target in TARGETS:
        a, b = before.get(target.key), after.get(target.key)
        regressed = None
        if a is not None and b is not None:
            worse_by = (b - a) if target.goal == "max" else (a - b)
            regressed = worse_by > target.tolerance + 1e-9
        rows.append({"key": target.key, "title": target.title, "before": a, "after": b, "regressed": regressed})
    return rows
