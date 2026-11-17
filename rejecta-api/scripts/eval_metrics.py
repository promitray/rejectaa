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
SECTION_SPREAD_LIMIT = 5


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
    Target("publication_status_accuracy", "Publication status (published / retracted / ...) found correctly", "min", 1.0, 0.0),
    Target("retracted_without_warning", "Retracted papers not flagged, or scored 40 or more", "max", 0, 0),
    Target("section_findings_grounded", "Section findings and strengths whose quotation is in the paper", "min", 1.0, 0.0),
    Target("section_score_spread_ok_share", f"Papers whose section scores move <= {SECTION_SPREAD_LIMIT} points between runs", "min", 0.90, 0.0),
    Target("checklist_determinism", "Papers whose section checklists are identical between runs", "min", 1.0, 0.0),
    Target("section_review_coverage", "Sections the section review reached", "min", 0.95, 0.0),
)
RECOMMENDATION_SCORE = 40  # a retracted paper must score below this
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


def section_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One paper's section results over its runs. Keys are left out when the runs carry no section data."""
    rows = [r for r in runs if r.get("section_scores") is not None]
    if not rows:
        return {}
    out: dict[str, Any] = {}
    names = set.intersection(*(set(r["section_scores"]) for r in rows))
    if len(rows) > 1 and names:  # the sections every run had: how far each score moved between runs
        out["section_spread"] = max(
            max(r["section_scores"][n] for r in rows) - min(r["section_scores"][n] for r in rows) for n in names
        )
        out["checklist_stable"] = all(
            len({tuple(r["section_checks"].get(n, [])) for r in rows}) == 1 for n in names
        )
    reviewed = [flag for r in rows for flag in (r.get("section_reviewed") or {}).values()]
    if reviewed:
        out["section_review_coverage"] = sum(reviewed) / len(reviewed)
    points = sum(r.get("section_points", 0) for r in rows)
    if points:
        out["section_grounded"] = sum(r.get("section_points_grounded", 0) for r in rows) / points
        out["section_points"] = points
    findings = [n for r in rows for n in (r.get("section_findings") or {}).values()]
    if findings:
        out["findings_per_section"] = statistics.mean(findings)
    return out


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
    if expect.get("publication_status") and parsed.get("integrity_status"):
        checks["publication_status_ok"] = parsed["integrity_status"] == expect["publication_status"]
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
    statuses = [r.get("integrity_status") for r in runs if r.get("integrity_status")]
    if statuses:
        parsed = {**parsed, "integrity_status": statuses[-1]}
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
        "integrity_status": parsed.get("integrity_status"),
        "expected_status": expect.get("publication_status"),
        **parser_checks(paper, parsed),
        **section_metrics(runs),
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
        "publication_status_accuracy": _share(collect("publication_status_ok")),
        "retracted_without_warning": _retracted_without_warning(records),
        "section_findings_grounded": _weighted_grounded(records),
        "section_score_spread_ok_share": _share([s <= SECTION_SPREAD_LIMIT for s in collect("section_spread")]),
        "checklist_determinism": _share(collect("checklist_stable")),
        "section_review_coverage": (
            statistics.mean(collect("section_review_coverage")) if collect("section_review_coverage") else None
        ),
    }


def _weighted_grounded(records: list[dict[str, Any]]) -> float | None:
    """Share of all section findings and strengths (over every paper) whose quotation is in the paper."""
    have = [r for r in records if r.get("section_grounded") is not None]
    total = sum(r["section_points"] for r in have)
    return sum(r["section_grounded"] * r["section_points"] for r in have) / total if total else None


def _retracted_without_warning(records: list[dict[str, Any]]) -> int | None:
    """Papers known to be retracted that the report did not flag, or still scored as if publishable."""
    retracted = [r for r in records if r.get("expected_status") == "retracted" and r.get("integrity_status")]
    if not retracted:
        return None
    return sum(
        1 for r in retracted
        if r["integrity_status"] != "retracted"
        or (r.get("median_score") is not None and r["median_score"] >= RECOMMENDATION_SCORE)
    )


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
