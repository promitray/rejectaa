"""Multi-Run Hallucination and Consistency Prober for Rejecta Paper Analysis.

Runs the same research paper 3 consecutive times to detect:
1. Verbatim Quote Hallucinations (Does any run invent text not in the paper?)
2. Score Stability / Drift (Does the score fluctuate > 5 points between runs?)
3. Rejection Reason Divergence (Are the critiques grounded and consistent?)
"""

from __future__ import annotations

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


async def run_multi_iteration_probe(
    paper_path: Path,
    journal: str = "Chemical Science",
    iterations: int = 3,
):
    print("=" * 85)
    print(f"🔬 MULTI-RUN HALLUCINATION & CONSISTENCY PROBE")
    print(f"📄 Paper: {paper_path.name}")
    print(f"🎯 Target Journal: {journal}")
    print(f"🔄 Total Iterations: {iterations}")
    print("=" * 85)

    runs_data = []

    for i in range(1, iterations + 1):
        print(f"\n▶️ Starting Run {i}/{iterations}...")
        start_time = time.time()
        
        adapter = PaperAnalysisAdapter(paper_path=paper_path, target_journal=journal, mode="rigorous")
        await adapter.call()
        elapsed = time.time() - start_time

        if adapter.execution_error:
            print(f"  ❌ Run {i} failed: {adapter.execution_error}")
            continue

        res = adapter.last_result
        analysis = res.analysis
        citations = res.citations
        full_text = adapter.parsed.full_text if adapter.parsed else ""

        # Grounding check: verify every piece of evidence against full text
        total_quotes = 0
        grounded_quotes = 0
        hallucinated_quotes = []

        reasons_list = []
        for r in analysis.rejection_reasons:
            reasons_list.append(r.reason)
            if r.evidence:
                total_quotes += 1
                evidence_clean = r.evidence.strip()
                if evidence_clean in full_text:
                    grounded_quotes += 1
                else:
                    hallucinated_quotes.append(evidence_clean)

        grounding_rate = (grounded_quotes / total_quotes * 100) if total_quotes > 0 else 100.0

        run_info = {
            "run": i,
            "duration": elapsed,
            "score": analysis.mood_score,
            "verdict": analysis.verdict,
            "citations_verified": citations.verified,
            "citations_ghost": citations.ghost,
            "total_quotes": total_quotes,
            "grounded_quotes": grounded_quotes,
            "grounding_rate": grounding_rate,
            "hallucinations": hallucinated_quotes,
            "reasons": reasons_list,
        }
        runs_data.append(run_info)

        print(f"  ⏱️ Time: {elapsed:.2f}s | Score: {analysis.mood_score}/100 | Grounding: {grounding_rate:.1f}%")
        if hallucinated_quotes:
            print(f"  🚨 HALLUCINATION DETECTED in Run {i}:")
            for h in hallucinated_quotes:
                print(f"     - \"{h}\"")
        else:
            print(f"  ✅ Grounding Clean: All quotes verified in original text.")

    # Cross-run Comparative Analysis
    print("\n" + "=" * 85)
    print("📊 CROSS-RUN COMPARATIVE EVALUATION & DRIFT ANALYSIS")
    print("=" * 85)

    scores = [r["score"] for r in runs_data]
    min_score = min(scores) if scores else 0
    max_score = max(scores) if scores else 0
    score_spread = max_score - min_score

    print(f"\n1️⃣ Score Stability:")
    for r in runs_data:
        print(f"   • Run {r['run']}: {r['score']}/100 ({r['duration']:.1f}s)")
    print(f"   Score Spread (Max - Min): {score_spread} points")
    if score_spread <= 5:
        print(f"   ✅ PASS: Score spread is within the strict <= 5 points stability threshold.")
    else:
        print(f"   ⚠️ WARNING: Score spread > 5 points indicates non-deterministic drift.")

    print(f"\n2️⃣ Hallucination & Grounding Check:")
    any_hallucination = any(len(r["hallucinations"]) > 0 for r in runs_data)
    if not any_hallucination:
        print(f"   ✅ PASS: 0 Hallucinations across all {iterations} runs (100% Verbatim Grounded).")
    else:
        print(f"   🚨 FAILED: One or more runs generated ungrounded quotes.")

    print(f"\n3️⃣ Critique & Weakness Stability:")
    for r in runs_data:
        print(f"   • Run {r['run']} identified {len(r['reasons'])} critique point(s):")
        for reason in r["reasons"][:2]:
            print(f"       - {reason[:80]}...")

    print("=" * 85 + "\n")


if __name__ == "__main__":
    test_pdf = DEFAULT_PAPERS_DIR / "published" / "Radical_ligand_transfer_a_general_strategy_for_radical_funct.pdf"
    if not test_pdf.exists():
        # Fallback to any pdf
        candidates = list(DEFAULT_PAPERS_DIR.rglob("*.pdf"))
        test_pdf = candidates[0] if candidates else None

    if test_pdf:
        asyncio.run(run_multi_iteration_probe(test_pdf, journal="Chemical Science", iterations=3))
    else:
        print(f"No test PDF found in {DEFAULT_PAPERS_DIR}")

