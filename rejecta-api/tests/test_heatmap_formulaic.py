"""Heatmap v2: formulaic phrasing catalogue, sentence splitting, fewer false positives, paragraph signals."""

from __future__ import annotations

from services.heatmap import HeatmapEngine
from services.heatmap.patterns import find_phrases
from services.heatmap.segmenter import DocumentSegmenter
from services.heatmap.stylometrics import StylometricAnalyzer

AI_STYLE = (
    "In the realm of polymer chemistry, self-reporting systems have emerged as a pivotal tool for monitoring release. "
    "These systems leverage the intricate interplay between fluorophores and quenchers to provide real-time insights. "
    "By harnessing this synergy, researchers can navigate the complex landscape of drug delivery with unprecedented precision. "
    "This approach not only enhances sensitivity but also fosters a deeper understanding of release mechanisms. "
    "Ultimately, this work underscores the transformative potential of self-reporting platforms in shaping the future of materials science."
)
HUMAN_RESULTS = (
    "The copolymer P1 was obtained by free radical copolymerization with a molar mass of 8700 g mol-1. "
    "Oxidation with mCPBA gave P2, whose fluorescence was strongly quenched. "
    "On heating at 90 C in anisole the fluorescence of the solution increased within hours. "
    "The SEC traces show a shift of about 4600 g mol-1 between P2 and P3, which corresponds to 13 released pyrene units. "
    "The DLS diameter of P3 decreased by about 1 nm."
)
HUMAN_METHODS = (
    "The polymer was dissolved in anisole. The solution was heated to 90 C for 50 h. "
    "The fluorescence was recorded every hour. The intensity rose sharply in the first 6 h and then levelled off."
)


def sentences(text: str) -> list[str]:
    return [s.text for p in DocumentSegmenter.segment(text) for s in p.sentences]


# ------------------------------------------------------------------ splitting

def test_a_citation_number_after_the_full_stop_ends_the_sentence_and_stays_with_it() -> None:
    assert sentences("Used in drug delivery.3 In nature, networks repair damage.4 Similarly, it works.") == [
        "Used in drug delivery.3", "In nature, networks repair damage.4", "Similarly, it works.",
    ]


def test_abbreviations_and_decimals_do_not_end_a_sentence_and_text_is_not_garbled() -> None:
    text = "The buffer was at pH 7.4 and the gap 2.5 nm. See Fig. 2 and e.g. Ref. 5 for details. Then it worked."
    assert sentences(text) == [
        "The buffer was at pH 7.4 and the gap 2.5 nm.", "See Fig. 2 and e.g. Ref. 5 for details.", "Then it worked.",
    ]


def test_offsets_still_slice_the_exact_sentence() -> None:
    text = "Used in drug delivery.3 In nature (Fig. 1B), networks repair.4 Done."
    encoded = text.encode("utf-16-le")
    for p in DocumentSegmenter.segment(text):
        for s in p.sentences:
            assert encoded[2 * s.start_utf16 : 2 * s.end_utf16].decode("utf-16-le") == s.text


# ------------------------------------------------------------------ the catalogue

def test_the_catalogue_finds_common_formulaic_phrases_with_their_group() -> None:
    found = {(m.group, m.text.lower()) for m in find_phrases(AI_STYLE)}
    assert ("filler phrase", "in the realm of") in found
    assert ("inflated verb", "leverage") in found
    assert ("hype word", "pivotal") in found
    assert any(g == "formulaic structure" for g, _ in found)
    assert any(g == "empty conclusion" for g, _ in found)


def test_overlapping_matches_count_once() -> None:
    assert len(find_phrases("This has transformative potential.")) == 1


def test_one_phrase_is_moderate_and_several_flag_the_sentence() -> None:
    one = StylometricAnalyzer.compute_lexical_repetition("The method holds great promise for sensing.", [])
    three = StylometricAnalyzer.compute_lexical_repetition("We leverage this pivotal synergy to unlock the full potential.", [])
    assert 0.4 <= one.normalized_score < 0.6 and three.normalized_score >= 0.9
    assert "Formulaic phrasing" in three.diagnostic_summary


# ------------------------------------------------------------------ false positives

def test_sentences_that_start_with_the_are_not_a_repeated_opener() -> None:
    result = StylometricAnalyzer.compute_transition_repetition(
        "The solution was heated.", recent_transitions=[], recent_openers=["the polymer was"], recent_stripped_bodies=[]
    )
    assert not any("Repeated opener" in e for e in result.evidence_spans)
    repeated = StylometricAnalyzer.compute_transition_repetition(
        "Herein we report a release.", recent_transitions=[], recent_openers=["herein we show"], recent_stripped_bodies=[]
    )
    assert any("Repeated opener" in e for e in repeated.evidence_spans)


def test_two_sentences_on_the_same_topic_are_not_a_template() -> None:
    a = "the fluorescence of the polymer increased after heating in anisole"
    b = "heating the polymer in anisole quenched none of the pyrene fluorescence"
    assert StylometricAnalyzer.compute_template_similarity(a, b) < 0.7


def test_thus_and_therefore_are_not_formulaic_transitions() -> None:
    result = StylometricAnalyzer.compute_transition_repetition("Thus, the release is thermal.", [], [], [])
    assert result.normalized_score == 0.0


def test_passive_counts_for_less_in_fields_that_write_in_the_passive() -> None:
    normal = StylometricAnalyzer.compute_passive_construction_ratio("The yield was calculated and the product was obtained.")
    chem = StylometricAnalyzer.compute_passive_construction_ratio("The yield was calculated and the product was obtained.", True)
    assert chem.normalized_score == round(normal.normalized_score * 0.5, 3)


# ------------------------------------------------------------------ whole texts

def test_formulaic_text_is_flagged_everywhere() -> None:
    result = HeatmapEngine.analyze(AI_STYLE)
    assert result.overall_level == "elevated_signals"
    assert all(s.level == "elevated" for s in result.paragraphs[0].sentences)
    assert result.paragraphs[0].paragraph_signals[0].startswith("Dense formulaic phrasing")
    assert "not a judgement of who wrote" in result.executive_assessment


def test_plain_human_results_and_methods_stay_clean() -> None:
    for text, field in ((HUMAN_RESULTS, "Polymer chemistry"), (HUMAN_METHODS, "chemistry"), (HUMAN_METHODS, "")):
        result = HeatmapEngine.analyze(text, "", field)
        assert result.overall_level == "low_signals", (text[:30], field)
        assert result.flagged_sentences_count == 0


def test_remedy_names_the_phrases_to_replace() -> None:
    sentence = HeatmapEngine.analyze(AI_STYLE).paragraphs[0].sentences[1]
    assert sentence.suggested_remedy and "leverage" in sentence.suggested_remedy


def test_one_long_formulaic_paragraph_is_not_averaged_away() -> None:
    result = HeatmapEngine.analyze(AI_STYLE + "\n\n" + HUMAN_METHODS)
    assert result.overall_level in ("mixed_signals", "elevated_signals")
    assert [p.level for p in result.paragraphs] == ["elevated", "clean"]
