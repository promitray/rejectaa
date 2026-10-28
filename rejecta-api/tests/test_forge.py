"""Unit tests for Abstract Forge synthesis engine v2.0:
Verifies:
1. Venue- and Discipline-Aware Indexing Targets (IEEE Xplore for Software Engineering, PubMed for Clinical).
2. Mandatory Paired Statistical Comparison Retention (e.g. 17 percentage points, 95% CI 9-25, p=0.018 in Clarity mode).
3. Invariant Counter Transparency: Tests category-level breakdown (spans across stats, units, citations).
4. Trade-off Retention (32% additional time penalty).
5. Anti-overclaim ClaimAuditDetail checks.
"""
import json
import pytest
from services.forge.engine import AbstractForgeEngine
from services.llm.base import AbstractAnalysisProvider


class MockForgeLLMv20(AbstractAnalysisProvider):
    """Deterministic mock provider simulating venue-aware structured abstract variants."""

    def __init__(self, output_json_str: str | None = None):
        self.output_json_str = output_json_str

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        if self.output_json_str:
            return self.output_json_str

        return json.dumps({
            "comparative_summary": "Synthesized 3 distinct architectures covering SEO discoverability, empirical clarity, and editorial impact.",
            "variants": [
                {
                    "variant_type": "discoverability_seo",
                    "title_label": "Discoverability (SEO & Indexing)",
                    "masked_text": "SpikeQ provides automated test suite minimization for concurrent software. Evaluated across test suites, branch coverage improved by __REJ_INV_0000__ (__REJ_INV_0001__; __REJ_INV_0002__), at the cost of __REJ_INV_0003__.",
                    "readiness_breakdown": {
                        "overall": 88,
                        "empirical_anchoring": 90,
                        "comparison_grounding": 88,
                        "tradeoff_retention": 88,
                        "narrative_discipline": 86,
                    },
                    "key_highlights": ["Natural declarative opening", "High search indexability"],
                    "seo_keywords": ["test suite minimization", "branch coverage", "paired bootstrap test", "software testing"],
                },
                {
                    "variant_type": "clarity_direct",
                    "title_label": "Clarity (Direct Empirical)",
                    "masked_text": "We evaluate SpikeQ on concurrent test suites, improving upon random testing baselines. Across benchmarks, branch coverage increased by __REJ_INV_0000__ (__REJ_INV_0001__; __REJ_INV_0002__), at the cost of __REJ_INV_0003__.",
                    "readiness_breakdown": {
                        "overall": 93,
                        "empirical_anchoring": 95,
                        "comparison_grounding": 94,
                        "tradeoff_retention": 92,
                        "narrative_discipline": 93,
                    },
                    "key_highlights": ["Paired bootstrap comparison retained", "Specific baseline fidelity"],
                },
                {
                    "variant_type": "high_impact_narrative",
                    "title_label": "High-Impact (Editorial Narrative)",
                    "masked_text": "Ensuring coverage in concurrent software requires scalable minimization algorithms. Here, we demonstrate that SpikeQ improves branch coverage by __REJ_INV_0000__ (__REJ_INV_0001__; __REJ_INV_0002__), advancing software reliability at a cost of __REJ_INV_0003__.",
                    "readiness_breakdown": {
                        "overall": 91,
                        "empirical_anchoring": 90,
                        "comparison_grounding": 90,
                        "tradeoff_retention": 90,
                        "narrative_discipline": 92,
                    },
                    "key_highlights": ["Reliability paradigm framing", "Grounded empirical honesty"],
                },
            ]
        })

    async def analyse(self, *args, **kwargs):
        raise NotImplementedError


def test_venue_and_discipline_aware_targeting_resolution():
    """Verify P1: IEEE Transactions on Software Engineering targets IEEE Xplore, NOT PubMed."""
    se_targets = AbstractForgeEngine.resolve_venue_targets(
        journal_name="IEEE Transactions on Software Engineering",
        field="Software Engineering",
    )
    assert "IEEE Xplore" in se_targets["seo_target"]
    assert "ACM Digital Library" in se_targets["seo_target"]
    assert "PubMed" not in se_targets["seo_target"], "PubMed must never be targeted for a Software Engineering venue!"
    assert "IEEE Transactions on Software Engineering" in se_targets["narrative_target"]

    # Clinical venue test
    clinical_targets = AbstractForgeEngine.resolve_venue_targets(
        journal_name="The Lancet Digital Health",
        field="Clinical Medicine",
    )
    assert "PubMed / MEDLINE" in clinical_targets["seo_target"]


@pytest.mark.asyncio
async def test_paired_bootstrap_comparison_and_category_telemetry():
    """Verify P0 & P1: Paired bootstrap comparison (17 percentage points, 95% CI 9-25, p = 0.018) is retained and invariant categories are transparently counted."""
    raw_sample = (
        "In this work, we propose SpikeQ for automated test suite minimization. "
        "Across software benchmarks, branch coverage increased by 17 percentage points (95% CI, 9–25; p = 0.018), "
        "improving upon random testing baselines, at the cost of 32% additional generation time."
    )

    mock_llm = MockForgeLLMv20()
    engine = AbstractForgeEngine(llm_service=mock_llm)

    result = await engine.forge(
        abstract_draft=raw_sample,
        journal_name="IEEE Transactions on Software Engineering",
        field="Software Engineering",
    )

    # 1. Invariant Telemetry Breakdown
    assert result.invariants_preserved_count >= 3
    assert result.invariant_telemetry.total_spans >= 3
    assert "17 percentage points" in result.invariant_telemetry.summary_label or "statistical" in result.invariant_telemetry.summary_label
    assert result.invariant_telemetry.by_category.get("stat", 0) >= 1 or result.invariant_telemetry.by_category.get("unit", 0) >= 1

    # 2. Fact Inventory extracted the mandatory paired comparison and trade-off
    assert result.fact_inventory["has_paired_stats"] is True
    assert any("17 percentage points" in ps for ps in result.fact_inventory["mandatory_paired_stats"])
    assert result.fact_inventory["has_tradeoffs"] is True
    assert any("32%" in to for to in result.fact_inventory["explicit_tradeoffs"])

    # 3. Check Clarity variant preserves the primary paired delta and the 32% cost
    v_clarity = next(v for v in result.variants if v.variant_type == "clarity_direct")
    assert "17 percentage points" in v_clarity.abstract_text
    assert "95% CI, 9–25" in v_clarity.abstract_text or "p = 0.018" in v_clarity.abstract_text
    assert "32%" in v_clarity.abstract_text
    assert v_clarity.claim_audit.is_faithful is True
    assert "IEEE Xplore" in result.variants[0].target_persona
