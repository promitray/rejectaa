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
from typing import Dict, List, Set, Tuple


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
    r"\bsignificantly\b",
    r"\bimportantly\b",
    r"\bto\s+this\s+end\b",
    r"\bin\s+this\s+regard\b",
    r"\bit\s+is\s+worth\s+noting\b",
    r"\bit\s+is\s+important\s+to\s+(?:note|remember|highlight)\b",
    r"\bas\s+a\s+result\b",
    r"\bhenceforth\b",
    r"\bthus\b",
    r"\btherefore\b",
]

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
        """Calculate structural token overlap (Jaccard similarity on word n-grams / tokens)

        between stripped sentence bodies.
        High similarity (> 0.60) indicates repeated sentence templating despite changing transition words.
        """
        tokens1 = [w.lower() for w in re.findall(r"\b[a-z]{3,}\b", s1_body)]
        tokens2 = [w.lower() for w in re.findall(r"\b[a-z]{3,}\b", s2_body)]

        if not tokens1 or not tokens2:
            return 0.0

        set1 = set(tokens1)
        set2 = set(tokens2)
        intersection = set1.intersection(set2)
        union = set1.union(set2)
        return len(intersection) / max(len(union), 1)

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
                if words_curr and words_past:
                    if words_curr[0] == words_past[0]:
                        stem_repeated = True
                        evidence_spans.append(f"Repeated opener structure '{words_curr[0]}'")
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

        template_repeated = max_template_sim >= 0.58
        if template_repeated:
            evidence_spans.append(f"Repeated sentence template ('{shared_template_excerpt}...')")

        # Dynamic score calculation based on actual evidence
        score = 0.05
        diagnostics: List[str] = []

        if template_repeated:
            # Substantial template repetition adds direct evidence-based score
            score += 0.40
            diagnostics.append(f"Repeated sentence template structure: '{shared_template_excerpt}...' (template similarity {max_template_sim:.2f})")

        if len(matched_transitions) >= 2:
            score += 0.45
            diagnostics.append(f"Clustered transition openers: {', '.join(matched_transitions)}")
        elif len(matched_transitions) == 1:
            score += 0.25
            repeats_recent_transition = any(t in recent_transitions for t in matched_transitions)
            if repeats_recent_transition:
                score += 0.15
                diagnostics.append(f"Repeated transition pattern within window: '{matched_transitions[0]}'")
            else:
                diagnostics.append(f"Formulaic transition opener: '{matched_transitions[0]}'")

        if stem_repeated and not template_repeated:
            score += 0.20
            diagnostics.append("Repeated opening subject/stem across consecutive sentences")

        final_score = round(max(0.05, min(0.95, score)), 3)
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
            score = 0.05
            summary = "Active declarative sentence structure."

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
        """Detect repetition of formulaic qualitative modifiers and low lexical diversity."""
        text_lower = sentence_text.lower()
        words = re.findall(r"\b[a-z]{3,}\b", text_lower)

        # Check for known clichés
        cliches_found: List[str] = []
        for pat in CLICHE_MODIFIERS:
            for m in re.finditer(pat, text_lower):
                cliches_found.append(m.group(0))

        # Check Type-Token Ratio (TTR) in the sentence
        if len(words) >= 8:
            unique_ratio = len(set(words)) / len(words)
        else:
            unique_ratio = 1.0

        score = 0.10
        summary_points: List[str] = []

        if cliches_found:
            score += min(0.60, len(cliches_found) * 0.30)
            summary_points.append(f"Clustered qualitative qualifiers: {', '.join(cliches_found)}")

        if unique_ratio < 0.65 and len(words) >= 12:
            score += 0.25
            summary_points.append(f"Low lexical diversity (Type-Token Ratio {unique_ratio:.2f})")

        normalized_score = max(0.05, min(0.95, score))
        summary = "; ".join(summary_points) if summary_points else "Diverse, non-repetitive vocabulary."

        return FeatureExtractionResult(
            feature_name="lexical_repetition",
            raw_value=round(unique_ratio, 3),
            normalized_score=round(normalized_score, 3),
            evidence_spans=cliches_found,
            diagnostic_summary=summary,
        )
