"""services/heatmap/engine.py
Heatmap analysis engine: Orchestrates UTF-16 code unit segmentation, deterministic stylometrics fusion,
and hierarchical aggregation (Sentence -> Paragraph -> Overall Document).
Enforces mathematically aligned diagnostics and transparent burstiness metrics.
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Literal, Tuple
from pydantic import BaseModel, Field

from services.heatmap.segmenter import DocumentSegmenter, ParagraphSpan, SentenceSpan
from services.heatmap.patterns import EM_DASH, TRICOLON, find_phrases
from services.heatmap.stylometrics import COMMON_OPENERS, StylometricAnalyzer, FeatureExtractionResult

ANALYSIS_VERSION = "2.0.0-formulaic-style"
OFFSET_UNIT = "utf16_code_units"


class SentenceHeatDetail(BaseModel):
    sentence_index: int
    text: str
    start_utf16: int
    end_utf16: int
    signal_score: float = Field(ge=0.0, le=1.0)
    level: Literal["clean", "moderate", "elevated"]
    feature_scores: Dict[str, float]
    diagnostics: List[str]
    evidence_spans: List[str]
    suggested_remedy: str | None = None


class ParagraphHeatDetail(BaseModel):
    paragraph_index: int
    text: str
    start_utf16: int
    end_utf16: int
    paragraph_heat_score: float = Field(ge=0.0, le=1.0)
    level: Literal["clean", "moderate", "elevated"]
    sentence_count: int
    paragraph_burstiness_cv: float
    sentences: List[SentenceHeatDetail]
    primary_issue: str | None = None
    paragraph_signals: List[str] = Field(default_factory=list)  # patterns seen across the paragraph, not one sentence


class HeatmapAnalysisResult(BaseModel):
    analysis_version: str = ANALYSIS_VERSION
    offset_unit: str = OFFSET_UNIT
    source_text_hash: str
    overall_cadence_score: float = Field(ge=0.0, le=1.0)
    overall_level: Literal["low_signals", "mixed_signals", "elevated_signals"]
    total_paragraphs: int
    total_sentences: int
    total_words: int
    flagged_sentences_count: int
    paragraphs: List[ParagraphHeatDetail]
    executive_assessment: str
    readability_metrics: Dict[str, float | str]
    warnings: List[str] = Field(default_factory=list)


def _first_content_word(sentence: str) -> str:
    for word in sentence.lower().split():
        word = word.strip(",;:()[]\"'")
        if word and word not in COMMON_OPENERS:
            return word
    return ""


class HeatmapEngine:
    """Core analysis engine for explainable stylometric cadence heatmaps."""

    # Fusion weights across the four deterministic dimensions
    _WEIGHT_UNIFORMITY = 0.15
    _WEIGHT_TRANSITIONS = 0.30
    _WEIGHT_PASSIVE = 0.10
    _WEIGHT_LEXICAL = 0.45  # formulaic phrasing is the strongest single signal
    _PHRASING_FLOOR = 0.85  # a sentence with several formulaic phrases is flagged whatever else it does
    _MIN_SENTENCES_FOR_RHYTHM = 4  # sentence-length variation means little in fewer sentences
    # Fields that write methods and results in the passive: there it is the norm, not a style problem.
    _PASSIVE_FIELDS = ("chem", "polym", "material", "bio", "physic", "medic", "clinic", "pharm", "engineer", "geo", "ecolog")

    @staticmethod
    def _paragraph_signals(text: str, sentences: List[str]) -> Tuple[float, List[str]]:
        """Score and reasons for patterns that show only across a paragraph."""
        words = max(1, len(text.split()))
        signals: List[str] = []
        score = 0.0
        phrases = find_phrases(text)
        density = 100 * len(phrases) / words
        if len(phrases) >= 3 and density >= 3.0:
            score = 0.70
            signals.append(f"Dense formulaic phrasing: {len(phrases)} stock phrases in {words} words")
        elif len(phrases) >= 2 and density >= 1.5:
            score = 0.45
            signals.append(f"Formulaic phrasing recurs: {len(phrases)} stock phrases in {words} words")
        if len(sentences) >= 4:
            firsts = [_first_content_word(x) for x in sentences]
            firsts = [w for w in firsts if w]
            if firsts and len(set(firsts)) / len(firsts) <= 0.5:
                score = min(0.95, score + 0.15)
                signals.append("Sentences keep opening the same way")
        tricolons = len(TRICOLON.findall(text))
        if tricolons >= 3:
            score = min(0.95, score + 0.10)
            signals.append(f"Repeated three-item lists ({tricolons})")
        dashes = len(EM_DASH.findall(text))
        if dashes >= 3:
            score = min(0.95, score + 0.10)
            signals.append(f"Frequent em dashes ({dashes})")
        return score, signals

    @classmethod
    def analyze(cls, text: str, journal_name: str = "", field: str = "") -> HeatmapAnalysisResult:
        """Run complete explainable stylometric analysis with exact UTF-16 spans."""
        venue = f"{journal_name} {field}".lower()
        passive_is_normal = any(w in venue for w in cls._PASSIVE_FIELDS)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        warnings: List[str] = []

        total_words = len(text.split())
        if total_words < 15:
            warnings.append("Insufficient text: Excerpt contains fewer than 15 words; stylometric variance requires broader context.")

        paragraphs = DocumentSegmenter.segment(text)

        all_sentence_lengths: List[int] = []
        for p in paragraphs:
            for s in p.sentences:
                all_sentence_lengths.append(s.word_count)

        processed_paragraphs: List[ParagraphHeatDetail] = []
        recent_transitions_window: List[str] = []
        recent_openers_window: List[str] = []
        recent_stripped_bodies_window: List[str] = []
        all_sentence_scores: List[float] = []
        flagged_count = 0

        # Process each paragraph
        for p_idx, p in enumerate(paragraphs):
            processed_sentences: List[SentenceHeatDetail] = []
            p_sentence_scores: List[float] = []
            p_lens = [s.word_count for s in p.sentences]

            # Calculate paragraph-level burstiness (CV)
            if len(p_lens) >= 2:
                p_mean = sum(p_lens) / len(p_lens)
                p_var = sum((l - p_mean) ** 2 for l in p_lens) / len(p_lens)
                p_cv = round(math.sqrt(p_var) / max(p_mean, 1.0), 3)
            else:
                p_cv = 0.50

            for s_idx, s in enumerate(p.sentences):
                # 1. Feature 1: Sentence-Length Uniformity
                if len(p.sentences) >= cls._MIN_SENTENCES_FOR_RHYTHM:
                    window_lens = [
                        p.sentences[k].word_count
                        for k in range(max(0, s_idx - 2), min(len(p.sentences), s_idx + 3))
                    ]
                    f_uniformity = StylometricAnalyzer.compute_sentence_length_uniformity(s.word_count, window_lens)
                else:
                    f_uniformity = FeatureExtractionResult(
                        "sentence_length_uniformity", 0.0, 0.0, [],
                        "Too few sentences in this paragraph to judge rhythm.",
                    )

                # 2. Feature 2: Transition Repetition & Structural Opener / Template Tracking
                f_transitions = StylometricAnalyzer.compute_transition_repetition(
                    s.text,
                    recent_transitions=recent_transitions_window,
                    recent_openers=recent_openers_window,
                    recent_stripped_bodies=recent_stripped_bodies_window,
                )

                # Update rolling memory of transitions, openers, and stripped sentence skeletons
                current_stem = StylometricAnalyzer.extract_sentence_opener_stem(s.text)
                if current_stem:
                    recent_openers_window.append(current_stem)
                    recent_openers_window = recent_openers_window[-4:]

                current_stripped_body = StylometricAnalyzer.strip_leading_transition(s.text)
                if current_stripped_body:
                    recent_stripped_bodies_window.append(current_stripped_body)
                    recent_stripped_bodies_window = recent_stripped_bodies_window[-4:]

                if f_transitions.evidence_spans:
                    for ev in f_transitions.evidence_spans:
                        if not ev.startswith("Repeated opener") and not ev.startswith("Repeated sentence template"):
                            recent_transitions_window.append(ev)
                    recent_transitions_window = recent_transitions_window[-4:]
                # 3. Feature 3: Passive Construction Ratio
                f_passive = StylometricAnalyzer.compute_passive_construction_ratio(s.text, passive_is_normal)

                # 4. Feature 4: Lexical Repetition / Qualifiers
                f_lexical = StylometricAnalyzer.compute_lexical_repetition(s.text, p.text.split())

                # Weighted Linear Fusion (0.0 to 1.0)
                combined_score = (
                    cls._WEIGHT_UNIFORMITY * f_uniformity.normalized_score
                    + cls._WEIGHT_TRANSITIONS * f_transitions.normalized_score
                    + cls._WEIGHT_PASSIVE * f_passive.normalized_score
                    + cls._WEIGHT_LEXICAL * f_lexical.normalized_score
                )
                combined_score = max(combined_score, cls._PHRASING_FLOOR * f_lexical.normalized_score)
                combined_score = round(max(0.0, min(0.95, combined_score)), 3)
                p_sentence_scores.append(combined_score)
                all_sentence_scores.append(combined_score)

                # Determine sentence level
                if combined_score > 0.58:
                    level: Literal["clean", "moderate", "elevated"] = "elevated"
                    flagged_count += 1
                elif combined_score >= 0.35:
                    level = "moderate"
                else:
                    level = "clean"

                # Aggregate diagnostics:
                # Key fix: Align threshold to 0.35 so that notable metrics (>35% in bar chart)
                # are always accompanied by their explainable diagnostic message!
                diagnostics: List[str] = []
                evidence_spans: List[str] = []

                for f in [f_uniformity, f_transitions, f_passive, f_lexical]:
                    if f.normalized_score >= 0.35:
                        diagnostics.append(f.diagnostic_summary)
                    if f.evidence_spans:
                        evidence_spans.extend(f.evidence_spans)

                remedy = None
                if level == "elevated":
                    if f_lexical.normalized_score >= 0.70 and f_lexical.evidence_spans:
                        remedy = (
                            "Replace the stock phrasing ("
                            + ", ".join(f"'{e}'" for e in f_lexical.evidence_spans[:3])
                            + ") with the specific result or method it stands for."
                        )
                    elif f_transitions.normalized_score >= 0.55:
                        remedy = "Vary the sentence opening; break consecutive repeated stems or transitions."
                    elif f_uniformity.normalized_score >= 0.65:
                        remedy = "Vary cadence: break this clause or fuse with adjacent findings to avoid repetitive sentence length."
                    elif f_passive.normalized_score >= 0.55:
                        remedy = "State scientific agency actively ('We measured...', 'The algorithm achieved...')."
                    elif f_lexical.evidence_spans:
                        remedy = (
                            "Replace the stock phrasing ("
                            + ", ".join(f"'{e}'" for e in f_lexical.evidence_spans[:3])
                            + ") with the specific result or method it stands for."
                        )
                    else:
                        remedy = "Prune decorative modifiers and foreground the quantitative metric."

                processed_sentences.append(
                    SentenceHeatDetail(
                        sentence_index=s.index,
                        text=s.text,
                        start_utf16=s.start_utf16,
                        end_utf16=s.end_utf16,
                        signal_score=combined_score,
                        level=level,
                        feature_scores={
                            "sentence_length_uniformity": f_uniformity.normalized_score,
                            "transition_repetition": f_transitions.normalized_score,
                            "passive_construction_ratio": f_passive.normalized_score,
                            "lexical_repetition": f_lexical.normalized_score,
                        },
                        diagnostics=diagnostics if diagnostics else ["Natural scholarly cadence."],
                        evidence_spans=list(set(evidence_spans)),
                        suggested_remedy=remedy,
                    )
                )

            # Paragraph Aggregation: sentence scores plus patterns that only show across the paragraph
            if p_sentence_scores:
                mean_p_score = sum(p_sentence_scores) / len(p_sentence_scores)
                max_p_score = max(p_sentence_scores)
                p_score = 0.65 * mean_p_score + 0.35 * max_p_score
            else:
                p_score = 0.0
            signal_score, paragraph_signals = cls._paragraph_signals(p.text, [x.text for x in p.sentences])
            p_score = round(min(0.95, max(p_score, signal_score)), 3)

            if p_score > 0.58:
                p_level: Literal["clean", "moderate", "elevated"] = "elevated"
                primary_issue = paragraph_signals[0] if paragraph_signals else "Clustered formulaic phrasing and repetitive structure"
            elif p_score >= 0.35:
                p_level = "moderate"
                primary_issue = paragraph_signals[0] if paragraph_signals else "Some formulaic phrasing or repeated structure"
            else:
                p_level = "clean"
                primary_issue = None

            processed_paragraphs.append(
                ParagraphHeatDetail(
                    paragraph_index=p.index,
                    text=p.text,
                    start_utf16=p.start_utf16,
                    end_utf16=p.end_utf16,
                    paragraph_heat_score=p_score,
                    level=p_level,
                    sentence_count=len(p.sentences),
                    paragraph_burstiness_cv=p_cv,
                    sentences=processed_sentences,
                    primary_issue=primary_issue,
                    paragraph_signals=paragraph_signals,
                )
            )

        # Overall Document Cadence Aggregation
        words_total = sum(max(1, len(p.text.split())) for p in processed_paragraphs)
        if processed_paragraphs:
            overall_score = round(
                sum(p.paragraph_heat_score * max(1, len(p.text.split())) for p in processed_paragraphs) / words_total, 3
            )
        else:
            overall_score = 0.0

        if overall_score > 0.55:
            overall_level: Literal["low_signals", "mixed_signals", "elevated_signals"] = "elevated_signals"
            assessment = "Formulaic style: stock phrases, inflated wording and repeated sentence structures cluster in several places. This describes style only; it is not a judgement of who wrote the text."
        elif overall_score >= 0.35:
            overall_level = "mixed_signals"
            assessment = "Some formulaic phrasing or repeated structure in places; most of the text reads as specific and varied."
        else:
            overall_level = "low_signals"
            assessment = "Little formulaic phrasing: the wording is specific and the sentence structure varies."

        # Compute document-level readability metrics & burstiness
        avg_len = sum(all_sentence_lengths) / max(len(all_sentence_lengths), 1)
        variance = sum((l - avg_len) ** 2 for l in all_sentence_lengths) / max(len(all_sentence_lengths), 1)
        burstiness_cv = round(math.sqrt(variance) / max(avg_len, 1.0), 2)

        # Qualitative burstiness tag
        if burstiness_cv >= 0.35:
            burstiness_label = "Dynamic Human Cadence (High Length Variance)"
        elif burstiness_cv >= 0.22:
            burstiness_label = "Moderate Cadence Variation"
        else:
            burstiness_label = "Monotonous Rhythm (Low Length Variance)"

        return HeatmapAnalysisResult(
            analysis_version=ANALYSIS_VERSION,
            offset_unit=OFFSET_UNIT,
            source_text_hash=text_hash,
            overall_cadence_score=overall_score,
            overall_level=overall_level,
            total_paragraphs=len(processed_paragraphs),
            total_sentences=len(all_sentence_scores),
            total_words=total_words,
            flagged_sentences_count=flagged_count,
            paragraphs=processed_paragraphs,
            executive_assessment=assessment,
            readability_metrics={
                "average_sentence_length": round(avg_len, 1),
                "sentence_burstiness_index": burstiness_cv,
                "burstiness_qualitative_tag": burstiness_label,
                "flagged_sentence_ratio": round(flagged_count / max(len(all_sentence_scores), 1), 2),
            },
            warnings=warnings,
        )
