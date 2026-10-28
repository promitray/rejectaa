"""services/rebuttal/classifier.py
Four-category diplomatic posture classifier for peer review comments:
1. AGREE_AND_AMEND: Valid critique requiring manuscript addition or change.
2. CLARIFY_MISCONCEPTION: Reviewer misunderstood or missed existing content; responsibility-first clarification.
3. PARTIAL_AGREE_BOUNDED: Critique has valid direction, but demanded experiment is infeasible or out-of-scope; counter-propose bounded alternative.
4. CONTEST_WITH_EVIDENCE: Reviewer makes a factually incorrect claim or invalid assumption; respectful evidence-backed defense.

Features conservative prior: defaults toward CLARIFY unless factual-error signals reach high confidence (>= 0.75).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Literal

PostureType = Literal[
    "agree_and_amend",
    "clarify_misconception",
    "partial_agree_bounded",
    "contest_with_evidence",
]


@dataclass
class PostureClassification:
    category: PostureType
    confidence: float
    diplomatic_posture_label: str
    matched_signals: List[str]
    suggested_strategy_notes: str


class PostureClassifier:
    """Classifies peer review critique into the optimal diplomatic posture."""

    # Factual error signals (Contest with evidence)
    _FACTUAL_ERROR_PATTERNS = [
        r"\b(?:this|the)\s+(?:claim|assumption|assertion|statement|equation|formula|definition)\b.*?\bis\s+(?:incorrect|wrong|false|unsupported|invalid)\b",
        r"\bformula\b.*?\bis\s+(?:wrong|incorrect|invalid|flawed)\b",
        r"\b(?:mathematically|statistically|theoretically|conceptually)\s+(?:invalid|incorrect|flawed|unsupported)\b",
        r"\b(?:there\s+is\s+no|lacks|without\s+any)\s+(?:evidence|empirical\s+basis|support)\b",
        r"\b(?:standard|established|well-known)\s+(?:practice|literature|consensus|protocol)\b",
        r"\bthe\s+authors?\s+(?:incorrectly|erroneously|mistakenly)\b",
        r"\bviolates\s+(?:the\s+laws?|principles?|assumptions?|standard)\b",
        r"\bcontradicts?\s+(?:established|prior|standard|literature)\b",
        r"\bcannot\s+(?:include|be|have|assume)\b.*?\b(?:linear|intercept|standard|definition)\b",
        r"\bshould\s+never\s+be\s+considered\b.*?\b(?:formula|score|metric|evaluation)\b",
    ]

    # Scope / Infeasibility / Disproportionate demands (Partial agree bounded)
    _SCOPE_INFEASIBILITY_PATTERNS = [
        r"\b(?:out\s+of\s+scope|beyond\s+(?:the\s+)?scope|outside\s+(?:the\s+)?scope)\b",
        r"\b(?:not\s+feasible|infeasible|computationally\s+(?:infeasible|prohibitive))\b",
        r"\b(?:would\s+require|requires|may\s+require)\s+(?:years|months|proprietary\s+data|excessive\s+compute|an\s+entirely\s+new|substantial\s+(?:additional\s+)?resources?)\b",
        r"\b(?:substantial|excessive|significant)\s+additional\s+resources?\b",
        r"\b(?:large[-\s]scale|external)\s+evaluation\b.*?\b(?:require|resources?|beyond|cost)\b",
        r"\b(?:unreasonable|disproportionate)\s+(?:request|demand|burden)\b",
        r"\bnot\s+(?:practical|achievable)\s+within\s+the\s+timeframe\b",
        r"\b(?:five|multi|several|\d+)[-\s]year\s+(?:clinical\s+)?trial\b",
        r"\b(?:clinical\s+trial|patients?|in\s+vivo|human\s+subjects?)\b.*?\b(?:50,?000|\d{4,})\b",
        r"\bconduct\s+(?:a\s+)?(?:five|multi|several|\d+)[-\s]year\b",
        r"\bevaluate\s+(?:at\s+least\s+)?(?:50,?000|\d{4,})\s+(?:functions?|repositories|repos?|classes?|files?)\b",
        r"\b(?:50,?000|\d{4,})\s+(?:functions?|repositories|repos?|projects?)\b",
    ]
    # Misconception / Clarification signals
    _CLARIFICATION_PATTERNS = [
        r"\b(?:unclear|not\s+clear|ambiguous|confusing|difficult\s+to\s+follow|hard\s+to\s+tell)\b",
        r"\b(?:seems|appears|it\s+is\s+not\s+obvious)\b.*?\b(?:whether|how|what|why|where)\b",
        r"\b(?:already|existing|presented|described)\s+in\s+(?:section|table|figure|appendix|supplement)\b",
        r"\bthe\s+manuscript\s+does\s+not\s+(?:clearly\s+)?(?:state|explain|define|specify)\b",
        r"\bwhat\s+(?:is\s+meant\s+by|does\s+this\s+mean)\b",
    ]

    # Direct agreement signals
    _AGREEMENT_PATTERNS = [
        r"\b(?:should|must|need\s+to|please)\s+(?:add|include|provide|compare|show|report|incorporate|benchmark)\b",
        r"\b(?:missing|absent|omit|lack|no)\s+(?:baseline|ablation|experiment|comparison|error\s+bars?|standard\s+deviation)\b",
        r"\b(?:sample\s+size|dataset\s+size|cohort)\s+(?:is\s+)?(?:small|insufficient|limited)\b",
        r"\bthe\s+authors?\s+should\s+(?:revisit|expand|clarify\s+in\s+the\s+main\s+text)\b",
        r"\bstatistical\s+(?:power|significance|tests?)\s+should\s+be\b",
    ]

    _POSTURE_LABELS: dict[PostureType, str] = {
        "agree_and_amend": "Humble Agreement & Revision Commitment",
        "clarify_misconception": "Responsibility-First Clarification",
        "partial_agree_bounded": "Bounded Agreement with Counter-Proposal",
        "contest_with_evidence": "Respectful Evidence-Backed Defence",
    }

    @classmethod
    def classify(cls, comment_text: str) -> PostureClassification:
        """Classify critique text into a diplomatic posture with conservative priors."""
        text_lower = comment_text.lower()
        signals: List[str] = []

        # 1. Scope / Infeasibility check (highest priority: protect authors from scope creep)
        scope_score = 0.0
        for pat in cls._SCOPE_INFEASIBILITY_PATTERNS:
            if re.search(pat, text_lower):
                signals.append(f"Scope constraint: {pat}")
                scope_score += 0.45

        if scope_score >= 0.40:
            return PostureClassification(
                category="partial_agree_bounded",
                confidence=min(0.85, 0.50 + scope_score),
                diplomatic_posture_label=cls._POSTURE_LABELS["partial_agree_bounded"],
                matched_signals=signals,
                suggested_strategy_notes="Acknowledge scientific merit of concern, respectfully delineate scope bounds, and offer concrete bounded alternative.",
            )

        # 2. Check Factual Error signals
        factual_score = 0.0
        for pat in cls._FACTUAL_ERROR_PATTERNS:
            if re.search(pat, text_lower):
                signals.append(f"Factual error cue: {pat}")
                factual_score += 0.40

        # CONTEST requires confidence >= 0.70 (at least two strong signals)
        if factual_score >= 0.70:
            return PostureClassification(
                category="contest_with_evidence",
                confidence=min(0.95, factual_score),
                diplomatic_posture_label=cls._POSTURE_LABELS["contest_with_evidence"],
                matched_signals=signals,
                suggested_strategy_notes="Validate reviewer's perspective first, then provide firm mathematical or empirical refutation with [CITATION NEEDED] placeholder hooks.",
            )

        # 3. Check Clarification / Misconception signals
        clarify_score = 0.0
        for pat in cls._CLARIFICATION_PATTERNS:
            if re.search(pat, text_lower):
                signals.append(f"Ambiguity/Misconception cue: {pat}")
                clarify_score += 0.35

        # 4. Check Agreement signals
        agree_score = 0.0
        for pat in cls._AGREEMENT_PATTERNS:
            if re.search(pat, text_lower):
                signals.append(f"Revision demand cue: {pat}")
                agree_score += 0.40

        # Decision with conservative priors
        if agree_score >= 0.40 and agree_score >= clarify_score:
            return PostureClassification(
                category="agree_and_amend",
                confidence=min(0.92, 0.50 + agree_score),
                diplomatic_posture_label=cls._POSTURE_LABELS["agree_and_amend"],
                matched_signals=signals,
                suggested_strategy_notes="Humbly accept the valid critique, thank the reviewer, and specify exact revision locations in the revised manuscript.",
            )
        elif clarify_score >= 0.35:
            return PostureClassification(
                category="clarify_misconception",
                confidence=min(0.90, 0.50 + clarify_score),
                diplomatic_posture_label=cls._POSTURE_LABELS["clarify_misconception"],
                matched_signals=signals,
                suggested_strategy_notes="Assume full author responsibility for the phrasing ambiguity, explain the nuance clearly, and pinpoint text modifications.",
            )
        else:
            # Conservative default: treat as clarification to remain diplomatic
            return PostureClassification(
                category="clarify_misconception",
                confidence=0.60,
                diplomatic_posture_label=cls._POSTURE_LABELS["clarify_misconception"],
                matched_signals=["Default conservative prior applied (no aggressive cues detected)"],
                suggested_strategy_notes="Politely address the point, provide clarifying explanation, and indicate corresponding manuscript refinement.",
            )
