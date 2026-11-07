"""Tests for the Rebuttal Writer "Part A" fixes.

Covers: deterministic fabrication guard, evidence gate, LLM batch classification with
regex fallback, field-neutral templates, dynamic export letter, repetition and score checks.
"""
import json

import pytest

from services.rebuttal.classifier import PostureClassifier
from services.rebuttal.engine import RebuttalEngine
from services.rebuttal.grounding import check_claims
from services.rebuttal.tone_auditor import ToneAuditor
from test_rebuttal import MockRebuttalLLM

CHEM_REPORT = (
    "Reviewer 1:\n"
    "1. The novelty is insufficient for a journal of this scope; this work would be better suited to a "
    "specialized polymer journal.\n"
    "2. The synthesis procedures lack sufficient detail on the characterization of the final products, "
    "which raises reproducibility concerns.\n\n"
    "Reviewer 2:\n"
    "1. The authors claim fast photoreduction but there is no comparison with existing systems.\n"
)

CHEM_EVIDENCE = (
    "Photoreduction of benzophenone within the repeat unit is considerably faster than coumarin and "
    "ortho-benzyl alcohol-based systems. Dual responsiveness to heat (Diels-Alder) and light (365 nm) is demonstrated."
)


class CapturingLLM(MockRebuttalLLM):
    """Records every drafting prompt and returns a configurable drafted response."""

    def __init__(self, drafted: str = "We thank the reviewer for the comment.") -> None:
        self.drafted = drafted
        self.user_prompts: list[str] = []
        self.system_prompts: list[str] = []
        self.consistency_response = json.dumps({"issues": []})
        self.consistency_prompts: list[str] = []
        # Default: the repair step returns nothing, so unsupported sentences are removed.
        self.repair_response = json.dumps({"sentences": {}})
        self.repair_prompts: list[str] = []

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.15) -> str:
        if "You classify peer-review comments" in system_prompt:
            return await super().generate(system_prompt, user_prompt, temperature)
        if "You check a draft rebuttal letter" in system_prompt:
            self.consistency_prompts.append(user_prompt)
            return self.consistency_response
        if "You repair sentences from a rebuttal draft" in system_prompt:
            self.repair_prompts.append(user_prompt)
            return self.repair_response
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        return json.dumps({
            "core_critique_summary": "Summary.",
            "drafted_response": self.drafted,
            "action_in_manuscript": "Propose to revise the relevant section.",
        })


# ---------------------------------------------------------------------------
# Grounding guard
# ---------------------------------------------------------------------------
def test_grounding_strips_priority_claim_when_no_evidence():
    response = "We thank the reviewer. This combination has not been previously reported. We propose to clarify the scope."
    result = check_claims(response, None, "novelty concern")
    assert result.stripped is True
    assert "not been previously reported" not in result.text
    assert "We propose to clarify the scope." in result.text
    assert any(f.startswith("unsupported_claim") for f in result.flags)


def test_grounding_strips_application_claim_when_no_evidence():
    result = check_claims("The system offers potential applications in controlled release.", "", "")
    assert result.stripped is True
    assert result.text == ""


def test_grounding_keeps_claim_that_the_author_evidence_supports():
    sentence = "Photoreduction is considerably faster than coumarin-based systems."
    result = check_claims(sentence, CHEM_EVIDENCE, "")
    assert result.stripped is False
    assert result.text == sentence


def test_grounding_priority_claim_needs_author_to_say_it_even_with_evidence():
    result = check_claims("This is the first demonstration of dual responsiveness.", CHEM_EVIDENCE, "")
    assert result.stripped is True


def test_grounding_does_not_split_on_abbreviations():
    result = check_claims("We will add controls (e.g. a blank run). We thank the reviewer.", None, "")
    assert result.stripped is False
    assert "e.g. a blank run" in result.text


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def test_evidence_gate_downgrades_contest_without_evidence():
    contest = PostureClassifier.classify(
        "This statement is incorrect and violates the standard literature consensus established by previous benchmarks."
    )
    assert contest.category == "contest_with_evidence"
    gated = PostureClassifier.apply_evidence_gate(contest, has_evidence=False)
    assert gated.category == "partial_agree_bounded"
    assert PostureClassifier.apply_evidence_gate(contest, has_evidence=True).category == "contest_with_evidence"


def test_regex_classifier_handles_novelty_venue_and_detail_concerns():
    novelty = PostureClassifier.classify(
        "The novelty is insufficient and this would be better suited to a specialized polymer journal."
    )
    assert novelty.category == "partial_agree_bounded"
    detail = PostureClassifier.classify("The synthesis lacks sufficient detail on the characterization of the products.")
    assert detail.category == "agree_and_amend"


@pytest.mark.asyncio
async def test_batch_llm_classification_is_used_and_carries_reviewer_requests():
    llm = CapturingLLM()
    llm.batch_response = json.dumps({"classifications": [
        {"comment_id": "R1C1", "category": "partial_agree_bounded", "confidence": 0.8,
         "reviewer_requests": ["Consider submitting to a specialized polymer journal"]},
        {"comment_id": "R1C2", "category": "agree_and_amend", "confidence": 0.9,
         "reviewer_requests": ["Add characterization detail"]},
        {"comment_id": "R2C1", "category": "contest_with_evidence", "confidence": 0.8, "reviewer_requests": []},
    ]})
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, CHEM_EVIDENCE, "Chemical Communications")

    cats = {c.comment_id: c.category for c in result.comments}
    assert cats == {"R1C1": "partial_agree_bounded", "R1C2": "agree_and_amend", "R2C1": "contest_with_evidence"}
    assert result.category_breakdown["clarify_misconception"] == 0
    # The venue/transfer request reached the drafting prompt instead of being dropped.
    assert any("specialized polymer journal" in p for p in llm.user_prompts)
    assert all("Chemical Communications" in p for p in llm.system_prompts)


@pytest.mark.asyncio
async def test_batch_classification_contest_is_downgraded_without_evidence():
    llm = CapturingLLM()
    llm.batch_response = json.dumps({"classifications": [
        {"comment_id": "R1C1", "category": "contest_with_evidence", "confidence": 0.9, "reviewer_requests": []},
        {"comment_id": "R1C2", "category": "agree_and_amend", "confidence": 0.9, "reviewer_requests": []},
        {"comment_id": "R2C1", "category": "agree_and_amend", "confidence": 0.9, "reviewer_requests": []},
    ]})
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, None, None)
    assert result.category_breakdown["contest_with_evidence"] == 0
    assert result.comments[0].category == "partial_agree_bounded"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_regex_and_still_varies_categories():
    llm = CapturingLLM()
    llm.fail_batch = True
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, None, None)
    cats = [c.category for c in result.comments]
    assert len(result.comments) == 3
    assert cats.count("clarify_misconception") < 3  # not the old "everything is clarify"
    assert "partial_agree_bounded" in cats  # the venue/novelty point
    assert "agree_and_amend" in cats


# ---------------------------------------------------------------------------
# End to end behaviour
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invented_priority_claim_is_removed_and_flagged_end_to_end():
    llm = CapturingLLM(
        "We thank the reviewer. This combination has not been previously reported. "
        "The system offers potential applications in controlled release. We propose to sharpen the contribution statement."
    )
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, None, "Chemical Communications")
    for comment in result.comments:
        assert "not been previously reported" not in comment.drafted_response
        assert "potential applications" not in comment.drafted_response
        # The author is told what was removed...
        assert any(f.startswith("unsupported_claim") for f in comment.tone_audit.flags)
        # ...but the final text no longer contains it, so it is not marked as a faulty draft
        # (unless something else, like a venue decision, still needs the author).
        if not any(f.startswith("author_decision_needed") for f in comment.tone_audit.flags):
            assert comment.tone_audit.is_clean is True
    assert "no author evidence" in llm.system_prompts[0].lower()
    # The unsupported sentences were offered for repair before being removed.
    assert llm.repair_prompts and "previously reported" in llm.repair_prompts[0]


@pytest.mark.asyncio
async def test_export_has_no_default_journal_no_editor_and_no_implemented():
    llm = CapturingLLM()
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, CHEM_EVIDENCE, None)
    md = result.export_document_md
    assert "Nature Machine Intelligence" not in md
    assert "not specified" in md
    assert "Editor" not in md
    assert "implemented" not in md
    assert "empirical evaluations" not in md
    assert md.count("Dear Reviewers,") == 1


@pytest.mark.asyncio
async def test_export_addresses_editor_only_when_editor_comment_exists():
    llm = CapturingLLM()
    report = "EDITOR: Major revision required. Please submit a point-by-point response.\n\n" + CHEM_REPORT
    result = await RebuttalEngine(llm_service=llm).analyze(report, CHEM_EVIDENCE, "Chemical Communications")
    assert "Dear Editor and Reviewers," in result.export_document_md


@pytest.mark.asyncio
async def test_identical_openings_are_flagged_and_score_is_not_a_flat_baseline():
    llm = CapturingLLM("We apologize if our original phrasing was not sufficiently clear. We propose to revise this section.")
    result = await RebuttalEngine(llm_service=llm).analyze(CHEM_REPORT, CHEM_EVIDENCE, None)
    assert all(any(f.startswith("repetitive_opening") for f in c.tone_audit.flags) for c in result.comments)
    assert all(any(f.startswith("boilerplate_phrase") for f in c.tone_audit.flags) for c in result.comments)
    assert all(c.tone_audit.diplomatic_score != 85 for c in result.comments)


def test_tone_auditor_rewards_engaging_with_the_actual_request():
    generic = ToneAuditor.audit("We appreciate this point and will revise the manuscript accordingly in due course.")
    specific = ToneAuditor.audit(
        "We appreciate this point. We propose to add characterization details for the final products in the Methods section.",
        ["Add characterization details for the final products"],
    )
    assert specific.diplomatic_score > generic.diplomatic_score


@pytest.mark.asyncio
async def test_social_science_sample_is_not_forced_into_ml_templates():
    llm = CapturingLLM()
    report = (
        "Reviewer 1:\n1. The sampling frame is not described clearly enough.\n"
        "2. Consider whether this paper is better suited to a specialized sociology journal.\n"
    )
    result = await RebuttalEngine(llm_service=llm).analyze(report, "We interviewed 24 participants.", None)
    joined = " ".join(c.drafted_response + c.action_in_manuscript for c in result.comments).lower()
    for ml_term in ("warmup", "intercept", "baseline", "clinical trial", "gpu", "compute"):
        assert ml_term not in joined
    assert result.comments[1].category == "partial_agree_bounded"
