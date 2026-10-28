"""Comprehensive regression test suite for Voice Calibrator Hardened Engine.
Verifies Sample 1 (Mixed compound citations & TeX), Sample 2 (Clinical intervals & metrics),
Sample 3 (Reviewer critique classification & Hard output blocking).
"""
import pytest
from services.calibrator.vault import ScientificVault
from services.calibrator.classifier import InputTypeClassifier
from services.calibrator.verifier import CalibrationAuditor
from services.calibrator.engine import VoiceCalibratorService
from services.llm.base import AbstractAnalysisProvider


class MockLLMProvider(AbstractAnalysisProvider):
    """Mock provider for deterministic testing of Voice Calibrator pipeline."""

    def __init__(self, output_generator=None):
        self.output_generator = output_generator

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        if self.output_generator:
            return self.output_generator(system_prompt, user_prompt)
        return '{"calibrated_masked_text": "mock text", "modifications": []}'

    async def analyse(self, *args, **kwargs):
        raise NotImplementedError


# ==============================================================================
# Sample 1 Regression: Mixed Citation & Complex TeX Mathematical Formulations
# ==============================================================================
def test_sample_1_mixed_citation_and_math_integrity():
    """Ensure compound citation `(Author et al., 2024; [17, 21–23])` and LaTeX formulas are fully preserved."""
    raw_sample_1 = (
        "In this paper, we basically investigate whether our SpikeQ quantization method "
        "can help spiking neural networks reduce energy consumption without causing a meaningful loss in predictive accuracy. "
        "We evaluated the method across 6 datasets and compared it with conventional post-training quantization (PTQ) baselines. "
        "Across these datasets, mean energy consumption per inference decreased from 1.84 mJ to 1.21 mJ, "
        "whereas mean top-1 accuracy changed minimally from 92.6% to 92.4%. "
        "The average accuracy difference was -0.2 percentage points (95% CI, -0.5 to 0.1; p = 0.18; paired test). "
        "Our optimization objective is defined as $\\mathcal{L}_{\\text{total}} = \\mathcal{L}_{\\text{CE}} + 0.05\\mathcal{L}_{\\text{spike}}$. "
        "The normalized energy reduction was consistent with prior observations (Author et al., 2024; [17, 21–23])."
    )

    bundle = ScientificVault.mask(raw_sample_1)

    # 1. Verify token count
    assert bundle.token_count >= 6
    # 2. Verify compound citation is preserved atomically
    compound_tokens = [r for r in bundle.records if "Author et al., 2024; [17, 21–23]" in r.original_value]
    assert len(compound_tokens) == 1, "Compound citation must be extracted as a single atomic invariant!"

    # 3. Verify LaTeX math is extracted atomically
    latex_tokens = [r for r in bundle.records if "\\mathcal{L}_{\\text{total}}" in r.original_value]
    assert len(latex_tokens) == 1

    # 4. Simulate rewrite preserving all tokens
    simulated_calibrated_masked = bundle.masked_text.replace(
        "In this paper, we basically investigate whether our SpikeQ quantization method can help",
        "We evaluated the SpikeQ quantization framework for"
    )

    restored, report = ScientificVault.verify_and_unmask(
        candidate_masked_text=simulated_calibrated_masked,
        vault=bundle.vault,
        records=bundle.records,
    )

    assert report.is_valid is True
    assert report.total_restored == bundle.token_count
    assert "(Author et al., 2024; [17, 21–23])" in restored
    assert "$\\mathcal{L}_{\\text{total}} = \\mathcal{L}_{\\text{CE}} + 0.05\\mathcal{L}_{\\text{spike}}$" in restored
    assert "1.84 mJ" in restored
    assert "1.21 mJ" in restored
    assert "p = 0.18" in restored


# ==============================================================================
# Sample 2 Regression: Clinical Trials, Risk Differences & Mortality Bounds
# ==============================================================================
def test_sample_2_clinical_intervals_and_mortality_bounds():
    """Ensure clinical cohort metrics, confidence intervals, and p-values are 100% matched."""
    raw_sample_2 = (
        "In this study, we wanted to see whether an automated triage support model "
        "could help clinicians assess emergency department patients more quickly. We evaluated the model in a prospective cohort of 890 emergency-department admissions. "
        "The median time to clinician assessment was 18 min (IQR, 12–27) for patients managed with model-assisted triage and 24 min (IQR, 16–35) under usual triage. "
        "The adjusted median difference was -5.1 min (95% CI, -7.8 to -2.4; p = 0.001). "
        "At the prespecified decision threshold of 0.62, sensitivity was 91.2% (95% CI, 88.7–93.3) and specificity was 78.4% (95% CI, 74.9–81.6). "
        "The difference in 30-day mortality was 0.4 percentage points (95% CI, -1.1 to 1.9; p = 0.61). "
        "Further external validation is needed before making broad claims about clinical effectiveness. (Author et al., 2025; [8, 11–13])."
    )

    bundle = ScientificVault.mask(raw_sample_2)

    # Check key clinical invariants are captured
    p_values = [r for r in bundle.records if "p = 0.001" in r.original_value or "p = 0.61" in r.original_value]
    assert len(p_values) >= 2

    # Check compound clinical citation is captured
    citation = [r for r in bundle.records if "(Author et al., 2025; [8, 11–13])" in r.original_value]
    assert len(citation) == 1

    # Simulate verified restoration
    restored, report = ScientificVault.verify_and_unmask(bundle.masked_text, bundle.vault, bundle.records)
    assert report.is_valid is True
    assert report.total_restored == bundle.token_count
    assert "890" in restored
    assert "-5.1 min" in restored
    assert "91.2%" in restored
    assert "78.4%" in restored
    assert "0.4 percentage points" in restored


# ==============================================================================
# Sample 3 Regression: Reviewer Feedback Classification & Zero Fabrication
# ==============================================================================
def test_sample_3_reviewer_feedback_classification_and_zero_fabrication():
    """Ensure reviewer comments are classified as critiques and never rewritten as fabricated manuscripts."""
    raw_sample_3 = (
        "[Reviewer Comment — Revision Required]\n\n"
        "The manuscript does not clearly establish how the proposed adaptive quantization method differs from existing post-training quantization methods "
        "for spiking neural networks. The authors should explain whether the approach uses temporal spike statistics, activation-range calibration, or another mechanism. "
        "The current draft also lacks a direct comparison of energy consumption, predictive accuracy, and robustness under distribution shift.\n\n"
        "Revision objective:\n"
        "Clarify the technical novelty of the method, distinguish it from established baselines, and support all claims of improved performance with appropriate experimental results. "
        "Do not claim superiority unless supported by ablation data."
    )

    classification = InputTypeClassifier.classify(raw_sample_3)
    assert classification.is_reviewer_feedback is True
    assert classification.detected_intent == "reviewer_critique"
    assert classification.confidence >= 0.70


# ==============================================================================
# P0 Hard Output Blocker Regression: Quarantine Corrupted or Missing Tokens
# ==============================================================================
@pytest.mark.asyncio
async def test_hard_output_blocker_quarantines_unrestored_text():
    """When an invariant token is dropped or orphan token is left by an LLM, the output MUST be blocked (None)."""
    raw_text = "The baseline AUROC = 0.891 was surpassed by our method with p < 0.001 across n = 500 subjects."

    # Mock an LLM that dropped the p-value placeholder (__REJ_INV_0001__) and corrupted __REJ_INV_0000__
    def corrupting_llm_generator(sys, usr):
        return '{"calibrated_masked_text": "The method achieved higher accuracy across __REJ_INV_0002__ but dropped other tokens.", "modifications": []}'

    mock_llm = MockLLMProvider(output_generator=corrupting_llm_generator)
    service = VoiceCalibratorService(llm_service=mock_llm)

    result = await service.calibrate(
        text=raw_text,
        journal_name="Nature Machine Intelligence",
        intensity="rewrite",
    )

    # Strict Assertions: Output MUST BE BLOCKED
    assert result.status == "blocked"
    assert result.calibrated_text is None, "Raw corrupted text MUST NEVER be released when verification fails!"
    assert result.invariants_verified is False
    assert len(result.block_reasons) > 0
    assert any("Missing protected" in r for r in result.block_reasons)
