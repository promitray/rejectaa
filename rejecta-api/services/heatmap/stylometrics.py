"""services/heatmap/stylometrics.py
Deterministic stylometric feature extractors:
1. Sentence-Length Uniformity (Rhythm Monotony)
2. Transition Repetition, Structural Opener Tracking & Repeated Sentence Template Detection
3. Passive Construction Ratio (Impersonal Agent Clustering)
4. Lexical Repetition (Low Lexical Entropy / Predictable Modifiers)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Set, Tuple

from services.heatmap.patterns import find_phrases


@dataclass(frozen=True)
class FeatureExtractionResult:
    feature_name: str
    raw_value: float
    normalized_score: float  # 0.0 to 1.0 (higher = elevated stylistic consistency / repetition)
    evidence_spans: List[str]
    diagnostic_summary: str


# Formulaic transition patterns frequently over-represented in synthetic drafts
TRANSITION_PATTERNS = [
    r"\bfurthermore\b",
    r"\bmoreover\b",
    r"\bin\s+addition\b",
    r"\badditionally\b",
    r"\bconsequently\b",
    r"\bnotably\b",
    r"\bimportantly\b",
    r"\bto\s+this\s+end\b",
    r"\bin\s+this\s+regard\b",
    r"\bit\s+is\s+worth\s+noting\b",
    r"\bit\s+is\s+important\s+to\s+(?:note|remember|highlight)\b",
    r"\bhenceforth\b",
]
# Words that start many ordinary sentences; a repeated one of these is not a pattern.
COMMON_OPENERS = frozenset(
    "the a an this these that those it its we our in for to of on at by with as after before here there".split()
)

# Common passive voice indicator patterns (auxiliary + past participle)
PASSIVE_VOICE_PATTERNS = [
    r"\b(?:is|are|was|were|be|been|being)\s+(?:shown|demonstrated|observed|evaluated|conducted|performed|calculated|analyzed|investigated|established|developed|achieved|obtained|identified|reported)\b",
    r"\b(?:can|could|may|might|should|must)\s+be\s+(?:seen|noted|observed|concluded|deduced|inferred|achieved)\b",
]

# Generic hyperbolic/formulaic modifiers frequently clustered in low-variance drafts
CLICHE_MODIFIERS = [
    r"\bparamount\b",
    r"\bpivotal\b",
    r"\bcrucial\b",
    r"\binvaluable\b",
    r"\bdelve\b",
    r"\btestament\b",
    r"\btapestry\b",
    r"\bunderscores\b",
    r"\bgroundbreaking\b",
    r"\bunprecedented\b",
    r"\bcomprehensive\b",
]


class StylometricAnalyzer:
    """Computes transparent, deterministic stylometric features over sentences and paragraphs."""

    @classmethod
    def compute_sentence_length_uniformity(
        cls,
        current_len: int,
        window_lengths: List[int],
    ) -> FeatureExtractionResult:
        """Measure how uniform sentence lengths are within a rolling context window.

        Higher score = consecutive sentences have almost identical length (low burstiness / rhythm monotony).
        Lower score = dynamic cadence (varied long and short sentences).

        Zero arbitrary word length gating: The diagnostic message reflects the EXACT normalized score!
        """
        if not window_lengths or len(window_lengths) < 2:
            return FeatureExtractionResult(
                feature_name="sentence_length_uniformity",
                raw_value=0.0,
                normalized_score=0.20,
                evidence_spans=[],
                diagnostic_summary="Cadence baseline (single sentence context).",
            )

        mean_len = sum(window_lengths) / len(window_lengths)
        variance = sum((l - mean_len) ** 2 for l in window_lengths) / len(window_lengths)
        std_dev = math.sqrt(variance)

        # Coefficient of variation (CV = std_dev / mean)
        cv = std_dev / max(mean_len, 1.0)

        # When CV is very low (< 0.15), lengths are stubbornly identical -> uniformity is near 1.0
        # When CV is high (> 0.45), cadence is diverse -> uniformity is low (< 0.3)
        uniformity = max(0.0, min(1.0, 1.0 - (cv / 0.50)))
        uniformity_rounded = round(uniformity, 3)

        # Strict alignment: diagnostic text strictly matches score tier
        evidence = [f"{l} words" for l in window_lengths]
        if uniformity_rounded >= 0.70:
            summary = f"Elevated sentence-length uniformity ({current_len} words, window CV {cv:.2f} — repetitive cadence)."
        elif uniformity_rounded >= 0.40:
            summary = f"Moderate sentence-length consistency ({current_len} words, window CV {cv:.2f})."
        else:
            summary = f"Natural sentence length variation (diverse cadence, window CV {cv:.2f})."

        return FeatureExtractionResult(
            feature_name="sentence_length_uniformity",
            raw_value=round(cv, 3),
            normalized_score=uniformity_rounded,
            evidence_spans=evidence if uniformity_rounded >= 0.60 else [],
            diagnostic_summary=summary,
        )

    @classmethod
    def extract_sentence_opener_stem(cls, sentence_text: str) -> str:
        """Extract the grammatical opening 2-3 words (stem) to detect structural repetition across sentences.

        e.g., 'We investigated whether...', 'We analyzed whether...', 'In this paper...'
        """
        clean = re.sub(r"^[^\w\s]+", "", sentence_text.strip())
        words = clean.split()
        if len(words) >= 3:
            return " ".join(words[:3]).lower()
        elif len(words) >= 2:
            return " ".join(words[:2]).lower()
        elif len(words) == 1:
            return words[0].lower()
        return ""

    @classmethod
    def strip_leading_transition(cls, sentence_text: str) -> str:
        """Strip leading transition words/phrases and punctuation to reveal the underlying core sentence skeleton.

        e.g., 'Moreover, the proposed framework provides...' -> 'the proposed framework provides...'
        """
        stripped = sentence_text.strip()
        for pattern in TRANSITION_PATTERNS:
            # Check if sentence starts with this transition pattern followed by comma or space
            m = re.match(rf"^{pattern}\s*[,;:]?\s*", stripped, flags=re.IGNORECASE)
            if m:
                stripped = stripped[m.end() :].strip()
                break
        return stripped

    @classmethod
    def compute_template_similarity(cls, s1_body: str, s2_body: str) -> float:
        """How alike two sentence bodies are as ordered word sequences (0..1).

        Word order matters, so two sentences that share topic words in a different structure score low,
        while "the proposed framework provides a robust ... X" written twice scores high.
        """
        tokens1 = re.findall(r"[a-z0-9]+", s1_body.lower())
        tokens2 = re.findall(r"[a-z0-9]+", s2_body.lower())
        if len(tokens1) < 5 or len(tokens2) < 5:
            return 0.0
        return SequenceMatcher(None, tokens1, tokens2, autojunk=False).ratio()

    @classmethod
    def compute_transition_repetition(
        cls,
        sentence_text: str,
        recent_transitions: List[str],
        recent_openers: List[str] | None = None,
        recent_stripped_bodies: List[str] | None = None,
    ) -> FeatureExtractionResult:
        """Detect formulaic transitions, opener stem repetition, AND repeated sentence templates."""
        text_lower = sentence_text.lower().strip()
        matched_transitions: List[str] = []
        evidence_spans: List[str] = []

        # 1. Check explicit transition words
        for pattern in TRANSITION_PATTERNS:
            match = re.search(pattern, text_lower)
            if match:
                matched_transitions.append(match.group(0))
                evidence_spans.append(match.group(0))

        # 2. Check structural opener stem repetition (e.g. consecutive 'we ...' or 'in ...')
        current_stem = cls.extract_sentence_opener_stem(sentence_text)
        stem_repeated = False
        if recent_openers and current_stem:
            words_curr = current_stem.split()
            for past_stem in recent_openers[-3:]:
                words_past = past_stem.split()
                if not words_curr or not words_past:
                    continue
                same_two = len(words_curr) >= 2 and len(words_past) >= 2 and words_curr[:2] == words_past[:2]
                same_rare_first = words_curr[0] == words_past[0] and words_curr[0] not in COMMON_OPENERS
                if same_two or same_rare_first:
                    stem_repeated = True
                    opener = " ".join(words_curr[:2]) if same_two else words_curr[0]
                    evidence_spans.append(f"Repeated opener structure '{opener}'")
                    break

        # 3. Check for repeated sentence templates (e.g. changing 'Moreover' -> 'Furthermore' with identical body)
        current_stripped = cls.strip_leading_transition(sentence_text)
        max_template_sim = 0.0
        shared_template_excerpt = ""
        if recent_stripped_bodies and current_stripped:
            for past_body in recent_stripped_bodies[-3:]:
                sim = cls.compute_template_similarity(current_stripped, past_body)
                if sim > max_template_sim:
                    max_template_sim = sim
                    # Grab common opening words
                    w_curr = current_stripped.split()[:5]
                    shared_template_excerpt = " ".join(w_curr)

        template_repeated = max_template_sim >= 0.70
        if template_repeated:
            evidence_spans.append(f"Repeated sentence template ('{shared_template_excerpt}...')")

        # Dynamic score calculation based on actual evidence
        score = 0.0
        diagnostics: List[str] = []

        if template_repeated:
            # Substantial template repetition adds direct evidence-based score
            score += 0.50
            diagnostics.append(f"Repeated sentence template structure: '{shared_template_excerpt}...' (template similarity {max_template_sim:.2f})")

        if len(matched_transitions) >= 2:
            score += 0.45
            diagnostics.append(f"Clustered transition openers: {', '.join(matched_transitions)}")
        elif len(matched_transitions) == 1:
            score += 0.15
            repeats_recent_transition = any(t in recent_transitions for t in matched_transitions)
            if repeats_recent_transition:
                score += 0.15
                diagnostics.append(f"Repeated transition pattern within window: '{matched_transitions[0]}'")
            else:
                diagnostics.append(f"Formulaic transition opener: '{matched_transitions[0]}'")

        if stem_repeated and not template_repeated:
            score += 0.20
            diagnostics.append("Repeated opening subject/stem across consecutive sentences")

        final_score = round(max(0.0, min(0.95, score)), 3)
        summary = "; ".join(diagnostics) if diagnostics else "Direct sentence opening without formulaic transition repetition."

        return FeatureExtractionResult(
            feature_name="transition_repetition",
            raw_value=round(max(float(len(matched_transitions)), max_template_sim), 3),
            normalized_score=final_score,
            evidence_spans=evidence_spans,
            diagnostic_summary=summary,
        )

    @classmethod
    def compute_passive_construction_ratio(
        cls,
        sentence_text: str,
        passive_is_normal: bool = False,
    ) -> FeatureExtractionResult:
        """Detect passive voice auxiliary chains without clear empirical agency."""
        text_lower = sentence_text.lower()
        passives_found: List[str] = []

        for pat in PASSIVE_VOICE_PATTERNS:
            for match in re.finditer(pat, text_lower):
                passives_found.append(match.group(0))

        # Check for active first-person or explicit algorithm agency ('We evaluated', 'The framework achieved')
        has_active_agency = bool(
            re.search(r"\b(?:we|our|the\s+model|the\s+algorithm|the\s+framework|the\s+architecture)\s+(?:demonstrated|showed|achieved|reduced|predicted|surpassed|optimized|measured)\b", text_lower)
        )

        passive_count = len(passives_found)
        if passive_count >= 2 and not has_active_agency:
            score = 0.75
            summary = f"Stacked passive constructions ({', '.join(passives_found)}) without explicit scientific agency."
        elif passive_count >= 1 and not has_active_agency:
            score = 0.50
            summary = f"Impersonal passive clause: '{passives_found[0]}'."
        elif passive_count >= 1 and has_active_agency:
            score = 0.25
            summary = "Balanced passive clause anchored by active empirical findings."
        else:
            score = 0.0
            summary = "Active declarative sentence structure."
        if passive_is_normal and passive_count:
            score = round(score * 0.5, 3)
            summary += " (passive is usual in this field, so it counts for less)"

        return FeatureExtractionResult(
            feature_name="passive_construction_ratio",
            raw_value=float(passive_count),
            normalized_score=score,
            evidence_spans=passives_found,
            diagnostic_summary=summary,
        )

    @classmethod
    def compute_lexical_repetition(
        cls,
        sentence_text: str,
        paragraph_tokens: List[str],
    ) -> FeatureExtractionResult:
        """Formulaic phrasing: stock phrases, inflated verbs, hype words, template structures, empty conclusions.

        One match is common in human writing; two or more in one sentence is what reads as formulaic.
        """
        matches = find_phrases(sentence_text)
        count = len(matches)
        score = 0.0 if count == 0 else 0.45 if count == 1 else 0.70 if count == 2 else 0.90

        words = re.findall(r"\b[a-z]{3,}\b", sentence_text.lower())
        unique_ratio = len(set(words)) / len(words) if len(words) >= 12 else 1.0
        summary_points: List[str] = []
        if matches:
            groups: Dict[str, List[str]] = {}
            for m in matches:
                groups.setdefault(m.group, []).append(m.text)
            summary_points.append(
                "Formulaic phrasing: " + "; ".join(f"{g} ({', '.join(t)})" for g, t in groups.items())
            )
        if unique_ratio < 0.60:
            score = min(0.95, score + 0.10)
            summary_points.append(f"Repeated words within the sentence (type-token ratio {unique_ratio:.2f})")

        return FeatureExtractionResult(
            feature_name="lexical_repetition",
            raw_value=float(count),
            normalized_score=round(score, 3),
            evidence_spans=[m.text for m in matches],
            diagnostic_summary="; ".join(summary_points) if summary_points else "No formulaic phrasing found.",
        )
