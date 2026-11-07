"""Run the paper analysis on real files, several times each, and show how stable the result is.

Uses the configured LLM provider (it costs API calls) and skips the citation lookup, so the score here has
no citation criterion. Use it to see, for each paper:
  * which article type, structure mode and sections were used;
  * how much the overall score moves between runs (aim for a spread of 5 points or less);
  * what the checks removed or flagged.

    python scripts/check_analysis.py paper1.pdf paper2.pdf --journal "Polymer Chemistry" --runs 3
    python scripts/check_analysis.py review.pdf --journal "Chem. Soc. Rev." --type review
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import paper_profile  # noqa: E402
from services.analysis_guard import AnalysisContext, paper_context  # noqa: E402
from services.llm import get_provider_cached  # noqa: E402
from services.parser import parse_document  # noqa: E402


def build_context(parsed) -> AnalysisContext:
    return AnalysisContext(
        source_text=parsed.full_text,
        article_type=parsed.article_type or paper_profile.UNKNOWN,
        type_source=parsed.article_type_source,
        structure_mode="detected" if parsed.structure == "detected" else "derived",
        section_texts={k: v for k, v in parsed.sections.items() if k in parsed.section_roles},
        section_roles=parsed.section_roles,
        abstract=parsed.abstract,
        body=parsed.body,
        refers_to_si=parsed.refers_to_si,
        numbers_text=f"{parsed.abstract} {parsed.body}",
    )


async def run_once(path: Path, journal: str, article_type: str | None, mode: str):
    parsed = parse_document(path.read_bytes(), path.name, article_type)
    context = build_context(parsed)
    with paper_context(context):
        analysis = await get_provider_cached().analyse(
            abstract=parsed.abstract,
            sections=parsed.sections,
            target_journal=journal,
            citation_summary="Citations were not checked in this run.",
            journal_data={},
            mode=mode,
        )
    return parsed, context, analysis


async def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("files", nargs="+", type=Path)
    cli.add_argument("--journal", required=True)
    cli.add_argument("--runs", type=int, default=3)
    cli.add_argument("--type", dest="article_type", default=None, help="research_article, communication, ...")
    cli.add_argument("--mode", default="a", choices=["a", "b"])
    args = cli.parse_args()

    for path in args.files:
        print(f"\n=== {path.name}")
        scores: list[int] = []
        names: list[set[str]] = []
        for run in range(1, args.runs + 1):
            parsed, context, analysis = await run_once(path, args.journal, args.article_type, args.mode)
            scores.append(analysis.mood_score)
            names.append(set(analysis.sections))
            if run == 1:
                print(f"type={analysis.paper_format} (source={parsed.article_type_source}) structure={context.structure_mode} "
                      f"coverage={parsed.coverage} refs={len(parsed.references)} si={parsed.refers_to_si}")
                print(f"abstract: {parsed.abstract[:160]}...")
            print(f"run {run}: score={analysis.mood_score} mood={analysis.mood} sections={list(analysis.sections)}")
            print("   criteria:", {k: v.score for k, v in analysis.criteria.items()})
            for note in analysis.not_assessed + analysis.grounding_notes:
                print("   note:", note)
        spread = max(scores) - min(scores)
        overlap = (
            statistics.mean(len(a & b) / max(1, len(a | b)) for a in names for b in names) if len(names) > 1 else 1.0
        )
        print(f"-> score spread {spread} ({'OK' if spread <= 5 else 'TOO WIDE'}), section-name overlap {overlap:.0%}")


if __name__ == "__main__":
    asyncio.run(main())
