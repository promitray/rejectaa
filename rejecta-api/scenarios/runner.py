"""CLI runner to execute Rejecta Scenario evaluations on papers from D:\\rejecta test papaer\\test_papers.

Usage:
  python -m scenarios.runner --paper "Radical_ligand"
  python -m scenarios.runner --all
  python -m scenarios.runner --tier published
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Add rejecta-api to sys.path
API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from scenarios.adapters.paper_adapter import PaperAnalysisAdapter

DEFAULT_PAPERS_DIR = Path(r"D:\rejecta test papaer\test_papers")


async def evaluate_single_paper(paper_path: Path, journal: str = "Chemical Science") -> dict:
    """Run full scenario analysis on one paper and verify high-accuracy constraints."""
    print("=" * 80)
    print(f"📄 Testing Paper: {paper_path.name}")
    print(f"📂 Folder: {paper_path.parent.name}")
    print(f"🎯 Target Journal: {journal}")
    print("-" * 80)

    start_time = time.time()
    adapter = PaperAnalysisAdapter(paper_path=paper_path, target_journal=journal, mode="rigorous")

    # Run the adapter
    review_output = await adapter.call()
    duration = time.time() - start_time

    if adapter.execution_error:
        print(f"❌ EXECUTION FAILED: {adapter.execution_error}")
        return {"status": "ERROR", "error": str(adapter.execution_error), "paper": paper_path.name}

    res = adapter.last_result
    analysis = res.analysis
    citations = res.citations
    integrity = res.integrity
    parsed = adapter.parsed
    full_text = parsed.full_text if parsed else ""

    # Check Zero-Hallucination Quote Grounding on rejection reasons and evidence
    total_quotes = 0
    grounded_quotes = 0
    hallucinations = []

    for r in analysis.rejection_reasons:
        if r.evidence:
            total_quotes += 1
            if r.evidence.strip() in full_text:
                grounded_quotes += 1
            else:
                hallucinations.append(r.evidence)

    grounding_rate = (grounded_quotes / total_quotes * 100) if total_quotes > 0 else 100.0

    print(f"⏱️  Duration: {duration:.2f}s")
    print(f"📊 Overall Score: {analysis.mood_score}/100")
    print(f"📌 Verdict: {analysis.verdict.upper()}")
    print(f"🏷️  Detected Article Type: {parsed.article_type if parsed else 'unknown'}")
    print(f"🛡️  Integrity Status: {integrity.status if integrity else 'not_checked'}")
    print(f"📚 Citations: {citations.verified} verified, {citations.ghost} ghost (Total: {citations.total})")
    print(f"🎯 Evidence Quote Grounding: {grounded_quotes}/{total_quotes} ({grounding_rate:.1f}%)")

    if hallucinations:
        print("⚠️  WARNING: Hallucinated quotes detected:")
        for h in hallucinations:
            print(f"   - '{h}'")
    else:
        print("✅ ZERO Hallucinations: All critique quotes verified in source text.")

    print("\n🔍 Key Rejection Reasons / Review Highlights:")
    for r in analysis.rejection_reasons[:3]:
        sev = r.severity.value if hasattr(r.severity, "value") else r.severity
        print(f"   • [{sev}] {r.reason}")
        if r.evidence:
            print(f"     Evidence: \"{r.evidence[:90]}...\"")

    status = "PASS" if not hallucinations and (citations.ghost == 0) else "WARN"
    print(f"\nResult: [{'🟢 PASS' if status == 'PASS' else '🟡 WARN'}]")
    print("=" * 80 + "\n")

    return {
        "status": status,
        "paper": paper_path.name,
        "folder": paper_path.parent.name,
        "score": analysis.mood_score,
        "verdict": analysis.verdict,
        "grounding_rate": grounding_rate,
        "duration": duration,
        "ghost_citations": citations.ghost,
    }


async def main():
    parser = argparse.ArgumentParser(description="Rejecta Scenario Test Runner")
    parser.add_argument("--paper", type=str, help="Substring of paper filename to test", default=None)
    parser.add_argument("--all", action="store_true", help="Run tests across all available papers")
    parser.add_argument("--tier", type=str, choices=["published", "retracted", "preprint_unpublished"], default=None)
    parser.add_argument("--papers-dir", type=str, default=str(DEFAULT_PAPERS_DIR))
    args = parser.parse_args()

    papers_dir = Path(args.papers_dir)
    if not papers_dir.exists():
        print(f"❌ Error: Papers directory not found at {papers_dir}")
        sys.exit(1)

    # Collect candidate PDFs
    if args.tier:
        pdf_files = list((papers_dir / args.tier).glob("*.pdf"))
    else:
        pdf_files = list(papers_dir.rglob("*.pdf"))

    # Exclude nested duplicate folders like "New folder"
    pdf_files = [p for p in pdf_files if "New folder" not in str(p)]

    if args.paper:
        pdf_files = [p for p in pdf_files if args.paper.lower() in p.name.lower()]

    if not pdf_files:
        print(f"❌ No matching PDF files found in {papers_dir}")
        sys.exit(1)

    print(f"\n🚀 Running Rejecta Scenario Evaluation on {len(pdf_files)} paper(s)...")

    results = []
    for pdf in pdf_files:
        folder = pdf.parent.name
        if folder == "retracted":
            journal = "Scientific Reports"
        elif folder == "preprint_unpublished":
            journal = "Journal of Chemical Information and Modeling"
        else:
            journal = "Chemical Science"

        res = await evaluate_single_paper(pdf, journal=journal)
        results.append(res)

    print("\n" + "=" * 80)
    print("📈 FINAL SCENARIO EVALUATION SUMMARY")
    print("=" * 80)
    print(f"{'Paper Name':<45} | {'Score':<6} | {'Verdict':<14} | {'Grounding':<9} | {'Status'}")
    print("-" * 80)
    for r in results:
        paper_short = (r["paper"][:42] + "...") if len(r["paper"]) > 45 else r["paper"]
        print(f"{paper_short:<45} | {r.get('score', 0):<6} | {r.get('verdict', 'N/A'):<14} | {r.get('grounding_rate', 0):.0f}%      | {r['status']}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
