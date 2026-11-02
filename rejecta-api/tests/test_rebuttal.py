"""Unit & Integration tests for Rebuttal Writer Engine v1.0.
Verifies:
1. CommentDissector properly splits multi-reviewer numbered reports with accurate attribution.
2. PostureClassifier conservative prior (defaults to clarify; gates contest behind high confidence).
3. ToneAuditor correctly penalizes aggressive phrases and awards diplomatic openers.
4. Citation hook extraction guarantees safe [CITATION NEEDED] placeholders for CONTEST items.
5. End-to-end integration test with MockLLMProvider generating valid RebuttalAnalyzeResponse.
"""
import json
import pytest
from services.rebuttal.dissector import CommentDissector, RawCommentItem
from services.rebuttal.classifier import PostureClassifier, PostureClassification
from services.rebuttal.tone_auditor import ToneAuditor, ToneAudit
from services.rebuttal.engine import RebuttalEngine, RebuttalAnalyzeResponse
from services.llm.base import AbstractAnalysisProvider


class MockRebuttalLLM(AbstractAnalysisProvider):
    """Deterministic mock provider for Rebuttal Writer pipeline tests."""

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.15) -> str:
        # Check system prompt posture to return relevant response
        if "AGREE AND AMEND" in system_prompt:
            return json.dumps({
                "core_critique_summary": "Reviewer requested comparative evaluation against additional baseline models.",
                "drafted_response": "We thank the reviewer for this insightful observation. We fully agree that including these comparisons strengthens our evaluation. We have added Table 4 in Section 3.2 reflecting these metrics.",
                "action_in_manuscript": "Added Table 4 in Section 3.2 comparing against Baseline X.",
            })
        elif "CLARIFY MISCONCEPTION" in system_prompt:
            return json.dumps({
                "core_critique_summary": "Reviewer raised uncertainty regarding the convergence criteria used in training.",
                "drafted_response": "We appreciate this constructive question and apologize if our original phrasing was ambiguous. To clarify, convergence is reached when delta loss is under 1e-4, as now explicitly detailed in Section 2.1.",
                "action_in_manuscript": "Revised Section 2.1 (lines 142–150) to explicitly define convergence criteria.",
            })
        elif "PARTIAL AGREE BOUNDED" in system_prompt:
            return json.dumps({
                "core_critique_summary": "Reviewer suggested testing across all combinations of hyperparameter grid.",
                "drafted_response": "We appreciate the reviewer's suggestion. While an exhaustive multi-dimensional grid search across all configurations is computationally infeasible within the present scope, we have expanded our ablation discussion in Section 4.2 to delineate these boundary constraints.",
                "action_in_manuscript": "Added boundary ablation discussion in Section 4.2.",
            })
        else:  # CONTEST WITH EVIDENCE
            return json.dumps({
                "core_critique_summary": "Reviewer claimed that our asymptotic complexity bound of O(N log N) is theoretically incorrect.",
                "drafted_response": "We appreciate the reviewer's attention to theoretical bounds. However, in accordance with established computational complexity literature [CITATION NEEDED: standard sorting lower bound proof], the bound holds under condition C. We have revised Section 1.3 to make this theoretical justification explicit.",
                "action_in_manuscript": "Clarified complexity derivation and added literature proof citations in Section 1.3.",
            })

    async def analyse(self, *args, **kwargs):
        raise NotImplementedError


# ==============================================================================
# Test 1: Deterministic Multi-Reviewer Dissection
# ==============================================================================
def test_dissector_splits_multireviewer_report():
    """Verify CommentDissector accurately parses 2 reviewers and assigns correct IDs and attribution."""
    sample_report = (
        "Reviewer 1:\n"
        "1. The baseline comparison in Table 2 is missing recent work by Smith et al. (2023).\n"
        "2. The sample size in Section 3 appears limited. Please provide power analysis.\n"
        "3. It is unclear whether data augmentation was used during the fine-tuning phase.\n\n"
        "Reviewer 2:\n"
        "1. This claim regarding universal superiority is unsupported by the empirical evidence.\n"
        "2. An exhaustive 5-year longitudinal clinical trial would be required to validate claim 4, which seems out of scope.\n"
    )

    items = CommentDissector.dissect(sample_report)

    # 1. Assert exactly 5 comments extracted
    assert len(items) == 5, f"Expected 5 comments, got {len(items)}"

    # 2. Assert reviewer attribution
    r1_items = [i for i in items if i.reviewer_id == "Reviewer 1"]
    r2_items = [i for i in items if i.reviewer_id == "Reviewer 2"]

    assert len(r1_items) == 3
    assert len(r2_items) == 2

    # 3. Assert comment ID format
    assert r1_items[0].comment_id == "R1C1"
    assert r1_items[1].comment_id == "R1C2"
    assert r1_items[2].comment_id == "R1C3"
    assert r2_items[0].comment_id == "R2C1"
    assert r2_items[1].comment_id == "R2C2"

    # 4. Assert cleaned content (no leading numbering)
    assert not r1_items[0].raw_text.startswith("1.")
    assert "Smith et al." in r1_items[0].raw_text


# ==============================================================================
# Test 2: Conservative Posture Classification
# ==============================================================================
def test_classifier_conservative_prior():
    """Verify conservative priors: defaults to clarify; high confidence needed for contest."""
    # A. Ambiguous comment with no strong cues -> defaults to clarify_misconception
    ambiguous = "The discussion on page 6 could be slightly more detailed regarding future directions."
    c_ambig = PostureClassifier.classify(ambiguous)
    assert c_ambig.category == "clarify_misconception"

    # B. Clear agreement cue -> agree_and_amend
    agree_text = "The authors must add a comparison with baseline PTQ algorithms and report standard deviation."
    c_agree = PostureClassifier.classify(agree_text)
    assert c_agree.category == "agree_and_amend"
    assert "Humble Agreement" in c_agree.diplomatic_posture_label

    # C. Scope limitation cue -> partial_agree_bounded
    scope_text = "Testing this on proprietary hospital EHR datasets is out of scope and would require months of ethics approvals."
    c_scope = PostureClassifier.classify(scope_text)
    assert c_scope.category == "partial_agree_bounded"

    # D. Factual error cue -> contest_with_evidence
    contest_text = "This statement is incorrect and violates the standard literature consensus established by previous benchmarks."
    c_contest = PostureClassifier.classify(contest_text)
    assert c_contest.category == "contest_with_evidence"
    assert c_contest.confidence >= 0.75


# ==============================================================================
# Test 3: Tone Auditor Aggressive Phrase Detection
# ==============================================================================
def test_tone_auditor_catches_aggressive_and_submissive_phrases():
    """Verify ToneAuditor penalizes condescending and submissive phrasing."""
    # A. Aggressive phrasing -> penalized heavily
    aggressive_text = "The reviewer clearly failed to read Section 3 where this is obviously stated. This criticism is unfair."
    audit_agg = ToneAuditor.audit(aggressive_text)
    assert audit_agg.is_clean is False
    assert audit_agg.diplomatic_score <= 45
    assert any("aggressive_language" in f for f in audit_agg.flags)

    # B. Submissive over-concession -> penalized
    submissive_text = "We completely agree with all criticisms and we will redo the entire study from scratch as requested."
    audit_sub = ToneAuditor.audit(submissive_text)
    assert any("submissive_overconcession" in f for f in audit_sub.flags)

    # C. Exemplary diplomatic scientific response -> clean with high score
    clean_text = (
        "We thank the reviewer for this constructive observation. We agree that clarifying our baseline "
        "conditions strengthens the evaluation, and we have updated Section 3.2 accordingly."
    )
    audit_clean = ToneAuditor.audit(clean_text)
    assert audit_clean.is_clean is True
    assert audit_clean.diplomatic_score >= 85
    assert len(audit_clean.flags) == 0


# ==============================================================================
# Test 4: Citation Hook Injection for Contest Items
# ==============================================================================
def test_citation_hook_injection_for_contest_items():
    """Verify safe [CITATION NEEDED] placeholder extraction without hallucinating real papers."""
    contest_resp = (
        "We appreciate the reviewer's comment. However, prior research establishes that under bounded memory, "
        "algorithm A converges monotonically [CITATION NEEDED: empirical convergence proof in distributed systems]."
    )
    audit = ToneAuditor.audit(contest_resp)
    assert audit.is_clean is True

    # Check regex extraction
    import re
    hooks = re.findall(r"\[\s*CITATION\s+NEEDED\s*:\s*([^\]]+)\]", contest_resp)
    assert len(hooks) == 1
    assert "empirical convergence proof" in hooks[0]


# ==============================================================================
# Test 5: End-to-End Orchestration Integration with MockLLM
# ==============================================================================
@pytest.mark.asyncio
async def test_full_rebuttal_pipeline_with_mock_llm():
    """Verify full end-to-end pipeline produces a valid RebuttalAnalyzeResponse."""
    mock_llm = MockRebuttalLLM()
    engine = RebuttalEngine(llm_service=mock_llm)

    sample_report = (
        "Reviewer 1:\n"
        "1. Please add a comparison against standard BERT baselines in Table 2.\n"
        "2. It is unclear how learning rate warmup was configured.\n\n"
        "Reviewer 2:\n"
        "1. Running clinical trials on 50,000 patients is computationally infeasible and outside the scope of this preprint.\n"
        "2. This statement is incorrect and lacks empirical support according to standard literature.\n"
    )

    result = await engine.analyze(
        text=sample_report,
        manuscript_context="Our model SpikeQ achieves 92.4% accuracy with 1.21 mJ energy per inference.",
        journal="Nature Machine Intelligence",
        author_tone="collaborative",
    )

    # 1. Assert response schema integrity
    assert isinstance(result, RebuttalAnalyzeResponse)
    assert result.total_comments_identified == 4
    assert len(result.reviewer_blocks) == 2
    assert len(result.comments) == 4

    # 2. Assert category distribution covers all 4 postures
    assert result.category_breakdown["agree_and_amend"] >= 1
    assert result.category_breakdown["clarify_misconception"] >= 1
    assert result.category_breakdown["partial_agree_bounded"] >= 1
    assert result.category_breakdown["contest_with_evidence"] >= 1

    # 3. Assert manuscript revision checklist generated from AGREE and PARTIAL items
    assert len(result.manuscript_revision_checklist) >= 2
    assert any("[R1C1]" in item for item in result.manuscript_revision_checklist)

    # 4. Assert export markdown contains formal letter conventions
    assert "# Point-by-Point Response to Reviewers" in result.export_document_md
    assert "## Response to Reviewer 1" in result.export_document_md
    assert "## Response to Reviewer 2" in result.export_document_md
    assert "**Author Response:**" in result.export_document_md
    assert "**Action in Revised Manuscript:**" in result.export_document_md


# ==============================================================================
# Test 6: Sample 2 Exact Classification & Proposed Action Grounding
# ==============================================================================
def test_sample_2_exact_classification_and_grounding():
    """Verify exact user-reported Sample 2 distribution: 1 Agree, 1 Clarify, 1 Bounded, 1 Contest."""
    r1c1 = "The manuscript lacks a clear comparison with the logistic regression baseline. Please include the comparative results."
    r1c2 = "The warmup schedule is not described clearly enough. Please clarify how it was configured."
    r2c1 = "The authors should conduct a five-year clinical trial involving 50,000 patients before making broader claims about the method."
    r2c2 = "The equation y = beta_0 + beta_1*x is mathematically invalid because a linear model cannot include an intercept. The authors must replace it."

    c1 = PostureClassifier.classify(r1c1)
    c2 = PostureClassifier.classify(r1c2)
    c3 = PostureClassifier.classify(r2c1)
    c4 = PostureClassifier.classify(r2c2)

    assert c1.category == "agree_and_amend", f"Expected agree_and_amend, got {c1.category}"
    assert c2.category == "clarify_misconception", f"Expected clarify_misconception, got {c2.category}"
    assert c3.category == "partial_agree_bounded", f"Expected partial_agree_bounded (5-year 50k trial), got {c3.category}"
    assert c4.category == "contest_with_evidence", f"Expected contest_with_evidence (intercept invalid), got {c4.category}"


# ==============================================================================
# Test 7: Sample 3 Dissection, Author Note Sanitization & Scope Classification
# ==============================================================================
def test_sample_3_author_note_strip_and_scope_classification():
    """Verify Sample 3: Author Note is stripped; R2C1 is Bounded; R2C3 is Contest; IDs are unique."""
    sample_3_text = (
        "EDITOR: Major revision required. Please submit a point-by-point response.\n\n"
        "Reviewer 1 — First, Table 2 reports 68% mutation score, why are other approaches not discussed? "
        "Please clarify the comparison.\n"
        "Another issue is that the paper claims practical superiority, yet RAG takes longer. Discuss the trade-off.\n\n"
        "Reviewer 2:\n"
        "(1) The authors should evaluate at least 50,000 functions across 100 repositories. Otherwise, findings are not useful.\n"
        "(2) The manuscript does not clearly explain the evaluation sample.\n"
        "(3) The formula for mutation score is wrong because equivalent mutants should never be considered. Replace the method.\n\n"
        "Reviewer 3 — The response from previous round was defensive. Please provide a respectful response.\n"
        "Reviewer 3, further comment: I disagree with the claim that higher mutation score means fewer production defects.\n\n"
        "IMPORTANT AUTHOR NOTE:\n"
        "We have not yet changed the manuscript. Do not say that a table has been added or line numbers invented."
    )

    items = CommentDissector.dissect(sample_3_text)

    # 1. Assert author note was stripped (does NOT appear as a comment)
    assert not any("IMPORTANT AUTHOR NOTE" in i.raw_text for i in items)

    # 2. Assert unique sequential IDs
    all_ids = [i.comment_id for i in items]
    assert len(all_ids) == len(set(all_ids)), f"Duplicate IDs detected: {all_ids}"

    # 3. Assert R2C1 is Bounded Proposal
    r2c1 = next(i for i in items if i.comment_id == "R2C1")
    c_r2c1 = PostureClassifier.classify(r2c1.raw_text)
    assert c_r2c1.category == "partial_agree_bounded"

    # 4. Assert R2C3 is Contest with Evidence
    r2c3 = next(i for i in items if i.comment_id == "R2C3")
    c_r2c3 = PostureClassifier.classify(r2c3.raw_text)
    assert c_r2c3.category == "contest_with_evidence"


# ==============================================================================
# Test 8: Sample 4 Resource Constraint & Large-Scale Evaluation Scope Bounding
# ==============================================================================
def test_sample_4_resource_constraint_bounded_proposal():
    """Verify Sample 4 R2C3: 'substantial additional resources' triggers Bounded Proposal."""
    r2c3_text = "A large-scale external evaluation would strengthen the findings, although I recognize that it may require substantial additional resources."
    classification = PostureClassifier.classify(r2c3_text)
    assert classification.category == "partial_agree_bounded"
    assert "Bounded Agreement" in classification.diplomatic_posture_label
