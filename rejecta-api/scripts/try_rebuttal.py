"""Run the Rebuttal Writer locally against the real LLM, without login or database.

Useful for checking what the engine produces before testing through the app.
Uses the provider from .env (LLM_PROVIDER / OPENAI_API_KEY), so each run costs a
few cents of API usage.

From rejecta-api/:

    # evidence box EMPTY (should make no novelty / superiority / application claims)
    .venv/Scripts/python.exe scripts/try_rebuttal.py --report scripts/rebuttal_samples/chem_report.txt --journal "Chemical Communications"

    # evidence box FILLED
    .venv/Scripts/python.exe scripts/try_rebuttal.py --report scripts/rebuttal_samples/chem_report.txt --evidence scripts/rebuttal_samples/chem_evidence.txt --journal "Chemical Communications"

The full export letter is written next to the report as <report>.rebuttal.md.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.llm import get_provider  # noqa: E402
from services.rebuttal.engine import RebuttalEngine  # noqa: E402

# Things the tester flagged in the old output. Any hit is printed as a warning.
RED_FLAGS = {
    "default journal": r"Nature Machine Intelligence",
    "priority claim": r"not (?:been )?previously reported|for the first time|first demonstration|unprecedented",
    "completed-action claim": r"\bmodifications implemented\b|\bwe have (?:added|revised|performed|conducted)\b",
    "stock phrase": r"refining our empirical evaluations",
}


def _read(path: str | None) -> str | None:
    return Path(path).read_text(encoding="utf-8").strip() if path else None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", required=True, help="text file with the reviewer comments")
    parser.add_argument("--evidence", help="text file with the author's key findings (leave out to test an empty box)")
    parser.add_argument("--journal", default=None)
    parser.add_argument("--tone", default="collaborative", choices=["collaborative", "diplomatic_firm", "formal_rigorous"])
    args = parser.parse_args()

    report = _read(args.report) or ""
    evidence = _read(args.evidence)
    engine = RebuttalEngine(llm_service=get_provider())
    result = await engine.analyze(text=report, manuscript_context=evidence, journal=args.journal, author_tone=args.tone)

    print(f"evidence: {'FILLED' if evidence else 'EMPTY'} | journal: {args.journal or 'not specified'}")
    print(f"categories: {result.category_breakdown}")
    print(f"strategy: {result.overall_negotiation_strategy}\n")
    for c in result.comments:
        print(f"--- {c.comment_id} [{c.category}] score {c.tone_audit.diplomatic_score} clean={c.tone_audit.is_clean}")
        print(f"reviewer: {c.original_quote[:140]}{'...' if len(c.original_quote) > 140 else ''}")
        print(f"response: {c.drafted_response}")
        print(f"action:   {c.action_in_manuscript}")
        if c.tone_audit.flags:
            print("flags:    " + " | ".join(c.tone_audit.flags))
        print()

    md = result.export_document_md
    warnings = [name for name, pattern in RED_FLAGS.items() if re.search(pattern, md, re.IGNORECASE)]
    if len(set(c.category for c in result.comments)) == 1 and len(result.comments) > 2:
        warnings.append(f"every comment got the same category ({result.comments[0].category})")
    print("CHECKS: " + ("no red flags found" if not warnings else "WARN -> " + ", ".join(warnings)))

    out = Path(args.report).with_suffix(".rebuttal.md")
    out.write_text(md, encoding="utf-8")
    print(f"export letter: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
