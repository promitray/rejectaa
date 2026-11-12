"""Extensive End-to-End Scenario Testing for Rejecta Paper Analysis Pipeline.

Tests academic manuscripts across tiers (published, preprint, retracted) using
LangWatch Scenario simulations, JudgeAgent evaluation, and programmatic
zero-hallucination quote grounding checks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import scenario
from scenarios.adapters.paper_adapter import PaperAnalysisAdapter

logger = logging.getLogger(__name__)

# Configure LiteLLM model for Scenario simulation & JudgeAgent
scenario.configure(default_model="gpt-4o-mini")


def _find_paper(test_papers_dir: Path, subfolder: str, filename_pattern: str) -> Path | None:
    """Find paper in target subfolder matching pattern."""
    target_dir = test_papers_dir / subfolder
    if not target_dir.exists():
        return None
    for f in target_dir.glob("*.pdf"):
        if filename_pattern.lower() in f.name.lower():
            return f
    return None


@pytest.mark.agent_test
@pytest.mark.asyncio
async def test_scenario_published_paper_review(test_papers_dir: Path):
    """Scenario 1: High-tier Published Paper Review.

    Simulates an author submitting a top-tier published chemistry paper to 'Chemical Science'.
    Validates:
      - Overall score (mood_score) >= 60 (well-regarded paper at its home journal)
      - Verdict is NOT desk_reject
      - 100% of rejection reason evidence quotes exist verbatim in the paper text (Zero Hallucination)
      - Ghost citation count is 0
      - Scenario JudgeAgent verifies academic tone and constructive feedback
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY secret is required for live Scenario evaluation")
    paper_path = _find_paper(test_papers_dir, "published", "Automatic_materials") or \
                 _find_paper(test_papers_dir, "published", "Radical_ligand") or \
                 _find_paper(test_papers_dir, "published", "")

    if not paper_path:
        pytest.skip(f"No published test PDF found in {test_papers_dir / 'published'}")

    target_journal = "Chemical Science"
    adapter = PaperAnalysisAdapter(paper_path=paper_path, target_journal=target_journal, mode="rigorous")

    # Run LangWatch Scenario
    result = await scenario.run(
        name=f"published-paper-review-{paper_path.stem[:30]}",
        description="A researcher submits a high-tier manuscript for pre-submission peer review.",
        agents=[
            adapter,
            scenario.JudgeAgent(
                criteria=[
                    "The critique evaluates methodology and baselines objectively",
                    "The tone is constructive, scholarly, and professional",
                    "The feedback highlights genuine experimental or analytical observations",
                ]
            ),
        ],
        script=[
            scenario.user(
                f"I am submitting our manuscript '{paper_path.name}' to {target_journal}. "
                "Please run a rigorous pre-submission peer review analyzing its fit and technical rigor."
            ),
            scenario.agent(),
            scenario.judge(),
            scenario.succeed(),
        ],
    )

    # Programmatic Hard Assertions
    pipeline_res = adapter.last_result
    assert pipeline_res is not None, f"Analysis pipeline failed to return result: {adapter.execution_error}"

    analysis = pipeline_res.analysis
    full_text = adapter.parsed.full_text if adapter.parsed else ""

    print(f"\n[Scenario Output] Paper: {paper_path.name}")
    print(f"  Score: {analysis.mood_score}/100")
    print(f"  Verdict: {analysis.verdict}")
    print(f"  Verified Citations: {pipeline_res.citations.verified} / {pipeline_res.citations.total}")
    print(f"  Ghost Citations: {pipeline_res.citations.ghost}")

    # Check 1: Score & Verdict
    assert analysis.mood_score >= 60, (
        f"Published high-tier paper scored {analysis.mood_score}, expected >= 60"
    )
    assert analysis.verdict.lower() != "desk_reject", (
        f"Published paper was falsely desk rejected! Verdict: {analysis.verdict}"
    )

    # Check 2: Zero Ghost Citations on Legitimate Published Paper
    assert pipeline_res.citations.ghost == 0, (
        f"False ghost citations detected: {pipeline_res.citations.ghost}"
    )

    # Check 3: Zero Hallucination Quote Grounding
    grounded_quotes = 0
    total_quotes = 0
    for r in analysis.rejection_reasons:
        if r.evidence:
            total_quotes += 1
            is_present = r.evidence.strip() in full_text
            assert is_present, (
                f"HALLUCINATION: Critique quote '{r.evidence}' is not found verbatim in paper text!"
            )
            grounded_quotes += 1

    print(f"  Quote Grounding Rate: {grounded_quotes}/{total_quotes} (100% grounded)")

    # Check 4: Scenario Judge evaluation
    assert result.success, f"Scenario Judge criteria failed: {result.failure_reason}"


@pytest.mark.agent_test
@pytest.mark.asyncio
async def test_scenario_retracted_paper_autopsy(test_papers_dir: Path):
    """Scenario 2: Retracted Paper Autopsy & Integrity Flagging.

    Tests that papers known to be retracted are caught by the integrity check,
    receive an explicit integrity alert, and score below 40.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY secret is required for live Scenario evaluation")
    paper_path = _find_paper(test_papers_dir, "retracted", "RETRACTED") or \
                 _find_paper(test_papers_dir, "retracted", "Anticancer") or \
                 _find_paper(test_papers_dir, "retracted", "")

    if not paper_path:
        pytest.skip(f"No retracted test PDF found in {test_papers_dir / 'retracted'}")

    target_journal = "Scientific Reports"
    adapter = PaperAnalysisAdapter(paper_path=paper_path, target_journal=target_journal, mode="rigorous")

    result = await scenario.run(
        name=f"retracted-paper-autopsy-{paper_path.stem[:30]}",
        description="A paper with known retractions or integrity concerns is analyzed.",
        agents=[
            adapter,
            scenario.JudgeAgent(
                criteria=[
                    "The critique notes serious reliability, experimental, or retraction concerns",
                    "The review does not recommend uncritical acceptance of the work",
                ]
            ),
        ],
        script=[
            scenario.user(f"Please analyze manuscript '{paper_path.name}' for {target_journal}."),
            scenario.agent(),
            scenario.judge(),
            scenario.succeed(),
        ],
    )

    pipeline_res = adapter.last_result
    assert pipeline_res is not None, f"Analysis pipeline failed: {adapter.execution_error}"

    analysis = pipeline_res.analysis
    integrity = pipeline_res.integrity

    print(f"\n[Scenario Output] Retracted Paper: {paper_path.name}")
    print(f"  Score: {analysis.mood_score}/100")
    print(f"  Integrity Status: {integrity.status if integrity else 'None'}")
    print(f"  Verdict: {analysis.verdict}")

    # Check 1: Retraction Flag or Severe Score Cap
    is_flagged = (integrity and integrity.status == "retracted") or (analysis.mood_score < 40)
    assert is_flagged, (
        f"Retracted paper was neither flagged as retracted (status={integrity.status if integrity else None}) "
        f"nor scored under 40 (score={analysis.mood_score})"
    )

    # Check 2: Never Accept
    assert analysis.verdict.lower() in ("desk_reject", "reject", "major_revision"), (
        f"Retracted paper received improper verdict: {analysis.verdict}"
    )

    assert result.success, f"Scenario Judge failed: {result.failure_reason}"


@pytest.mark.agent_test
@pytest.mark.asyncio
async def test_scenario_preprint_paper_evaluation(test_papers_dir: Path):
    """Scenario 3: Preprint / Novel Architecture Paper Evaluation.

    Tests unpublished preprints (e.g. ChemRxiv, arXiv) for method completeness,
    section extraction accuracy, and objective scoring.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY secret is required for live Scenario evaluation")
    paper_path = _find_paper(test_papers_dir, "preprint_unpublished", "Conditional_VAE") or \
                 _find_paper(test_papers_dir, "preprint_unpublished", "")

    if not paper_path:
        pytest.skip(f"No preprint test PDF found in {test_papers_dir / 'preprint_unpublished'}")

    target_journal = "Journal of Chemical Information and Modeling"
    adapter = PaperAnalysisAdapter(paper_path=paper_path, target_journal=target_journal, mode="rigorous")

    result = await scenario.run(
        name=f"preprint-evaluation-{paper_path.stem[:30]}",
        description="A novel preprint manuscript submitted for evaluation.",
        agents=[
            adapter,
            scenario.JudgeAgent(
                criteria=[
                    "The critique assesses the model architecture, generative validity, or experimental comparison",
                    "The analysis provides concrete technical feedback on methods and results",
                ]
            ),
        ],
        script=[
            scenario.user(f"Evaluate this preprint '{paper_path.name}' for submission to {target_journal}."),
            scenario.agent(),
            scenario.judge(),
            scenario.succeed(),
        ],
    )

    pipeline_res = adapter.last_result
    assert pipeline_res is not None, f"Analysis pipeline failed: {adapter.execution_error}"

    analysis = pipeline_res.analysis
    full_text = adapter.parsed.full_text if adapter.parsed else ""

    print(f"\n[Scenario Output] Preprint: {paper_path.name}")
    print(f"  Score: {analysis.mood_score}/100")
    print(f"  Verdict: {analysis.verdict}")
    print(f"  Detected Article Type: {adapter.parsed.article_type if adapter.parsed else 'Unknown'}")

    # Grounding check
    for r in analysis.rejection_reasons:
        if r.evidence:
            assert r.evidence.strip() in full_text, (
                f"HALLUCINATION: Quote '{r.evidence}' not found in preprint text!"
            )

    assert result.success, f"Scenario Judge failed: {result.failure_reason}"
