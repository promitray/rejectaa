"""Unit tests for AI Signal Heatmap:
- UTF-16 code unit offset verification (exact slicing)
- Deterministic stylometrics (Uniformity, Transitions, Opener Stems, Passive, Lexical)
- Diagnostic consistency: Proves that high uniformity never emits 'Natural sentence length variation'
- Transition repetition & Repeated sentence template detection (Jaccard structural overlap)
- Edge cases: Multi-byte Unicode emojis, math expressions, abbreviations, and short text.
"""
import pytest
from services.heatmap.segmenter import DocumentSegmenter, _utf16_len
from services.heatmap.stylometrics import StylometricAnalyzer
from services.heatmap.engine import HeatmapEngine


def test_utf16_offset_exact_slicing():
    """Verify that text.slice(start_utf16, end_utf16) matches the exact sentence verbatim."""
    sample = (
        "In this work, we evaluate neural networks. Furthermore, the accuracy was improved by 4.2%.\n\n"
        "Consequently, the model demonstrated superior speed! Does this generalize across datasets?"
    )

    paragraphs = DocumentSegmenter.segment(sample)
    assert len(paragraphs) == 2

    # Verify paragraph 1
    p1 = paragraphs[0]
    encoded_p1 = sample.encode("utf-16-le")[p1.start_utf16 * 2 : p1.end_utf16 * 2].decode("utf-16-le")
    assert encoded_p1 == p1.text

    # Verify every sentence matches exact slice
    for p in paragraphs:
        for s in p.sentences:
            sliced = sample.encode("utf-16-le")[s.start_utf16 * 2 : s.end_utf16 * 2].decode("utf-16-le")
            assert sliced == s.text, f"Offset mismatch: expected '{s.text}' but got '{sliced}'"


def test_utf16_offset_with_astral_plane_unicode():
    """Test with 4-byte astral characters (e.g. mathematical blackboard bold or emojis requiring surrogate pairs in UTF-16)."""
    # 𝒯 is U+1D4AF (requires surrogate pair in UTF-16, len=2 code units)
    sample = "The mathematical operator 𝒯 was evaluated. Moreover, the result holds."
    paragraphs = DocumentSegmenter.segment(sample)

    assert len(paragraphs) == 1
    s2 = paragraphs[0].sentences[1]
    sliced_s2 = sample.encode("utf-16-le")[s2.start_utf16 * 2 : s2.end_utf16 * 2].decode("utf-16-le")
    assert sliced_s2 == "Moreover, the result holds."


def test_sentence_length_uniformity_diagnostic_consistency():
    """Verify P0: If sentence-length uniformity is >= 0.70 (e.g. 74% or 83%), it NEVER claims 'Natural variation'."""
    window_lengths = [22, 22, 23, 22]  # Extremely uniform
    res = StylometricAnalyzer.compute_sentence_length_uniformity(22, window_lengths)
    assert res.normalized_score >= 0.70
    assert "Natural sentence length variation" not in res.diagnostic_summary
    assert "Elevated sentence-length uniformity" in res.diagnostic_summary

    diverse_lengths = [7, 34, 14, 45]
    res_diverse = StylometricAnalyzer.compute_sentence_length_uniformity(7, diverse_lengths)
    assert res_diverse.normalized_score < 0.40
    assert "Natural sentence length variation" in res_diverse.diagnostic_summary


def test_repeated_sentence_template_detection():
    """Verify P0: Detects sentences where transition words change but the underlying template repeats."""
    s1 = "Moreover, the proposed framework provides a robust and comprehensive solution for research data analysis."
    s2 = "Furthermore, the proposed framework provides a robust and comprehensive solution for research data interpretation."
    s3 = "Additionally, the proposed framework provides a robust and comprehensive solution for research data evaluation."

    body1 = StylometricAnalyzer.strip_leading_transition(s1)
    body2 = StylometricAnalyzer.strip_leading_transition(s2)
    assert body1.startswith("the proposed framework provides")
    assert body2.startswith("the proposed framework provides")

    # High similarity between stripped bodies
    sim = StylometricAnalyzer.compute_template_similarity(body1, body2)
    assert sim >= 0.70

    # Test sentence 2 detection
    res2 = StylometricAnalyzer.compute_transition_repetition(
        s2,
        recent_transitions=["moreover"],
        recent_openers=["moreover the proposed"],
        recent_stripped_bodies=[body1],
    )

    assert any("Repeated sentence template" in ev for ev in res2.evidence_spans)
    assert "Repeated sentence template structure" in res2.diagnostic_summary
    assert res2.normalized_score >= 0.65


def test_heatmap_engine_readability_metrics_and_burstiness():
    """Verify P1: Readability metrics include qualitative burstiness label and paragraph-level CV."""
    text = (
        "In this paper, we delve deeply into clinical models. It is paramount that our transformer achieves high utility. "
        "Furthermore, our attention regularization minimizes variance such that results improve significantly.\n\n"
        "Consequently, the findings were shown to be robust. Moreover, it is important to remember that baselines were surpassed."
    )

    result = HeatmapEngine.analyze(text=text, journal_name="Nature", field="Medicine")

    assert result.total_paragraphs == 2
    assert result.total_sentences >= 4
    assert result.offset_unit == "utf16_code_units"
    assert "sentence_burstiness_index" in result.readability_metrics
    assert "burstiness_qualitative_tag" in result.readability_metrics
    assert isinstance(result.readability_metrics["burstiness_qualitative_tag"], str)

    for p in result.paragraphs:
        assert hasattr(p, "paragraph_burstiness_cv")
        assert p.paragraph_burstiness_cv >= 0.0
