"""Measure scoring accuracy on the labelled papers in eval/manifest.yaml.

Runs the same pipeline as POST /analyse-paper (services/pipeline.py) on every paper, several times, and
reports how far the results are from the targets in scripts/eval_metrics.py. Citation and journal
lookups are cached in eval/cache/, so two runs differ only by the code and the model.

    # Parser only: no model, no network, a few seconds. Safe for CI.
    python -m scripts.eval_scoring --parser-only

    # Full run (costs model calls): 3 runs per paper, results in eval/runs/<time>-<label>/
    python -m scripts.eval_scoring --label baseline

    # One paper, refresh the cached lookups, and compare with an earlier run (exit 1 on a regression)
    python -m scripts.eval_scoring --only chemcomm-2026-iacs-editorial --live --compare eval/runs/<dir>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from models import CitationResult, JournalMatch, Severity  # noqa: E402
from scripts import eval_metrics  # noqa: E402
from services.citations import verify_citations  # noqa: E402
from services.journals import match_journal  # noqa: E402
from services.parser import parse_document  # noqa: E402

EVAL_DIR = API_ROOT / "eval"
MANIFEST = EVAL_DIR / "manifest.yaml"
PAPERS_DIR = EVAL_DIR / "papers"
CACHE_DIR = EVAL_DIR / "cache"
RUNS_DIR = EVAL_DIR / "runs"
# Files whose content decides the score; their hash names the code version a run measured.
FINGERPRINT_FILES = (
    "services/parser.py", "services/paper_profile.py", "services/analysis_guard.py",
    "services/llm/base.py", "services/citations.py", "services/journals.py", "services/pipeline.py",
)


def load_manifest(path: Path = MANIFEST) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    papers = data.get("papers") or []
    for paper in papers:
        if paper.get("label") not in eval_metrics.LABELS:
            raise SystemExit(f"{paper.get('id')}: label must be one of {eval_metrics.LABELS}")
    return papers


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in FINGERPRINT_FILES:
        file = API_ROOT / name
        if file.exists():
            digest.update(file.read_bytes())
    return digest.hexdigest()[:8]


# --- cached lookups --------------------------------------------------------------------------

def _cache_file(kind: str, key: str) -> Path:
    return CACHE_DIR / kind / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]}.json"


def cached_lookups(live: bool):
    """verify/match functions that answer from eval/cache/ and fill it on a miss (or always, with live)."""

    async def verify(references: list[str]) -> list[CitationResult]:
        file = _cache_file("citations", "\n".join(references))
        if file.exists() and not live:
            return [CitationResult.model_validate(item) for item in json.loads(file.read_text(encoding="utf-8"))]
        results = await verify_citations(references)
        if not any(r.check_incomplete for r in results):  # never cache an answer from a busy service
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps([r.model_dump(mode="json") for r in results], indent=1), encoding="utf-8")
        return results

    async def match(abstract: str, journal: str) -> JournalMatch:
        file = _cache_file("journals", journal.strip().lower())
        if file.exists() and not live:
            return JournalMatch.model_validate_json(file.read_text(encoding="utf-8"))
        result = await match_journal(abstract, journal)
        if result.found:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(result.model_dump_json(indent=1), encoding="utf-8")
        return result

    return verify, match


# --- running ---------------------------------------------------------------------------------

def parse_paper(paper: dict[str, Any]):
    path = PAPERS_DIR / paper["file"]
    if not path.exists():
        raise FileNotFoundError(f"{paper['id']}: {path} not found (papers are not committed; see eval/README.md)")
    parsed = parse_document(path.read_bytes(), path.name, paper.get("upload_type"))
    info = {
        "article_type": parsed.article_type,
        "article_type_source": parsed.article_type_source,
        "references": len(parsed.references),
        "section_roles": sorted(set(parsed.section_roles.values())),
        "structure": parsed.structure,
        "word_count": parsed.word_count,
        "abstract_start": parsed.abstract[:160],
    }
    return parsed, info


def section_summary(result) -> dict[str, Any]:
    """What the section analysis gave, for the section metrics: scores, which sections the review reached, how
    many of its quotations are really in the paper (checked again here, not trusted), and the checklists."""
    from services.analysis_guard import alnum

    corpus = result.context.corpus
    sections = result.analysis.sections
    points = [q for s in sections.values() for q in [*(f.quote for f in s.findings), *(t.quote for t in s.strengths)]]
    return {
        "section_scores": {name: s.score for name, s in sections.items()},
        "section_reviewed": {name: s.llm_score is not None for name, s in sections.items()},
        "section_findings": {name: len(s.findings) for name, s in sections.items()},
        "section_points": len(points),
        "section_points_grounded": sum(1 for quote in points if len(alnum(quote)) >= 20 and alnum(quote) in corpus),
        "section_checks": {name: sorted(f"{c.key}:{c.status}" for c in s.checks) for name, s in sections.items()},
    }


def run_summary(result) -> dict[str, Any]:
    analysis = result.analysis
    return {
        **section_summary(result),
        "score": analysis.mood_score,
        "mood": analysis.mood.value if hasattr(analysis.mood, "value") else str(analysis.mood),
        "desk_reject": any(r.severity == Severity.desk_reject for r in analysis.rejection_reasons),
        "criteria": {name: c.score for name, c in analysis.criteria.items()},
        "sections": list(analysis.sections),
        "reasons": [{"severity": r.severity.value, "problem": r.problem or r.reason} for r in analysis.rejection_reasons],
        "paper_format": analysis.paper_format,
        "provider": analysis.provider_used,
        "verified": result.citations.verified,
        "ghost": result.citations.ghost,
        "total": result.citations.total,
        "journal_name": result.journal.openalex_name if result.journal.found else None,
        "verdict": analysis.verdict,
        "integrity_status": result.integrity.status if result.integrity else None,
    }


async def evaluate_paper(paper, runs: int, mode: str, lookups, gate: asyncio.Semaphore) -> dict[str, Any]:
    from services.pipeline import run_analysis  # imports the model provider only for full runs

    parsed, info = parse_paper(paper)
    verify, match = lookups
    run_rows: list[dict[str, Any]] = []
    for _ in range(runs):
        async with gate:
            started = time.perf_counter()
            result = await run_analysis(parsed, paper["journal"], mode, verify=verify, match=match)
            row = run_summary(result)
            row["seconds"] = round(time.perf_counter() - started, 1)
            run_rows.append(row)
    record = eval_metrics.paper_record(paper, info, run_rows)
    record["parser"] = info
    record["run_details"] = run_rows
    return record


async def parser_only_record(paper: dict[str, Any], integrity: bool = False) -> dict[str, Any]:
    parsed, info = parse_paper(paper)
    if integrity:  # the publication-status gate: Crossref and OpenAlex, no model
        from services.integrity import check_publication

        status = await check_publication(parsed)
        info["integrity_status"] = status.status
        info["integrity_doi"] = status.doi
    record = eval_metrics.paper_record(paper, info, [])
    record["parser"] = info
    return record


# --- reporting -------------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}" if value <= 1 else f"{value:.1f}"
    return str(value)


def summary_markdown(meta: dict[str, Any], metrics: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        f"# Scoring evaluation: {meta['label']}",
        "",
        f"{meta['time']} · code {meta['fingerprint']} · {meta['papers']} papers · "
        f"{meta['runs']} runs each · {'parser only' if meta['parser_only'] else meta.get('provider') or ''}",
        "",
        "| Metric | Value | Target | Met |",
        "| --- | --- | --- | --- |",
    ]
    for target in eval_metrics.TARGETS:
        value = metrics.get(target.key)
        met = eval_metrics.meets(target, value)
        sign = "≤" if target.goal == "max" else "≥"
        lines.append(
            f"| {target.title} | {_fmt(value)} | {sign} {target.limit:g} | "
            f"{'–' if met is None else 'yes' if met else '**no**'} |"
        )
    lines += ["", "## Papers", "", "| Paper | Label | Score (runs) | Desk reject | Type found | Refs | Journal matched |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in records:
        p = r["parser"]
        lines.append(
            f"| {r['id']} | {r['label']} | {_fmt(r.get('median_score'))} {r.get('scores') or ''} | "
            f"{_fmt(r.get('desk_reject'))} | {p['article_type'] or 'none'} | {p['references']} | "
            f"{_fmt(r.get('journal_name'))} |"
        )
    return "\n".join(lines) + "\n"


def load_summary(path: Path) -> dict[str, Any]:
    file = path / "summary.json" if path.is_dir() else path
    return json.loads(file.read_text(encoding="utf-8"))


def print_comparison(before: dict[str, Any], after: dict[str, Any]) -> bool:
    rows = eval_metrics.compare(before["metrics"], after["metrics"])
    print(f"\nCompared with {before['meta']['label']} ({before['meta']['time']}, code {before['meta']['fingerprint']}):")
    regressed = False
    for row in rows:
        flag = "REGRESSED" if row["regressed"] else ""
        regressed = regressed or bool(row["regressed"])
        print(f"  {row['title']:<58} {_fmt(row['before']):>7} -> {_fmt(row['after']):<7} {flag}")
    return regressed


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the Windows console defaults to cp1252
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("--parser-only", action="store_true", help="check parsing only: no model, no network")
    cli.add_argument("--integrity", action="store_true",
                     help="with --parser-only: also check the publication status (Crossref/OpenAlex, no model)")
    cli.add_argument("--runs", type=int, default=3, help="analyses per paper (default 3)")
    cli.add_argument("--concurrency", type=int, default=4, help="analyses in flight at once (default 4)")
    cli.add_argument("--mode", default="a", choices=["a", "b"])
    cli.add_argument("--label", default="run", help="a short name for this run, e.g. baseline or parser-fix")
    cli.add_argument("--only", nargs="*", help="paper ids to run (default: all)")
    cli.add_argument("--split", choices=["dev", "heldout", "all"], default="dev",
                     help="which papers to use (default dev; use heldout only for the final check)")
    cli.add_argument("--live", action="store_true", help="refresh cached citation and journal lookups")
    cli.add_argument("--compare", type=Path, help="earlier run dir or summary.json; exit 1 on a regression")
    cli.add_argument("--manifest", type=Path, default=MANIFEST)
    args = cli.parse_args()

    papers = load_manifest(args.manifest)
    if args.split != "all":
        papers = [p for p in papers if p.get("split", "dev") == args.split]
    if args.only:
        papers = [p for p in papers if p["id"] in set(args.only)]
    if not papers:
        print("No papers selected.")
        return 1

    if args.parser_only:
        records = [await parser_only_record(p, args.integrity) for p in papers]
        runs = 0
    else:
        gate = asyncio.Semaphore(args.concurrency)
        lookups = cached_lookups(args.live)
        records = await asyncio.gather(*(evaluate_paper(p, args.runs, args.mode, lookups, gate) for p in papers))
        runs = args.runs

    metrics = eval_metrics.summarize(records)
    providers = sorted({row["provider"] for r in records for row in r.get("run_details", [])})
    meta = {
        "label": args.label + ("-parser" if args.parser_only else ""),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fingerprint": code_fingerprint(),
        "papers": len(records),
        "runs": runs,
        "parser_only": args.parser_only,
        "provider": ", ".join(providers),
        "split": args.split,
    }

    out = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{meta['label']}-{meta['fingerprint']}"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    (out / "summary.json").write_text(json.dumps({"meta": meta, "metrics": metrics}, indent=2), encoding="utf-8")
    report = summary_markdown(meta, metrics, records)
    (out / "summary.md").write_text(report, encoding="utf-8")

    print(report)
    print(f"Saved to {out}")
    if args.compare:
        if print_comparison(load_summary(args.compare), {"meta": meta, "metrics": metrics}):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
