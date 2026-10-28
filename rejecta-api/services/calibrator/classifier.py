"""services/calibrator/classifier.py
Input classification engine to distinguish manuscript draft prose from reviewer critiques or editorial revision directives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ClassificationResult:
    is_reviewer_feedback: bool
    confidence: float
    matched_signals: List[str]
    detected_intent: str  # 'manuscript_draft' or 'reviewer_critique'


class InputTypeClassifier:
    """Classifies incoming text to determine whether it is manuscript prose or reviewer feedback.

    Prevents scientific hallucination when instructions or reviewer comments are submitted.
    """

    # Strong explicit headers / tokens
    _EXPLICIT_HEADER_PATTERNS = [
        r"\[\s*reviewer\s+(?:comment|report|#\d+|note).*?\]",
        r"\[\s*revision\s+(?:required|objective|goal|instructions?).*?\]",
        r"\[\s*editor\s+(?:comment|note|decision).*?\]",
        r"reviewer\s+#?\d+\s*:",
        r"comment\s+#?\d+\s*:",
    ]

    # Reviewer directive phrases instructing the authors
    _REVIEWER_DIRECTIVE_PATTERNS = [
        r"\bthe\s+manuscript\s+does\s+not\s+(?:clearly\s+)?(?:establish|show|explain|demonstrate)",
        r"\bthe\s+authors?\s+should\s+(?:explain|clarify|demonstrate|provide|compare|revisit)",
        r"\bthe\s+current\s+draft\s+(?:also\s+)?lacks\b",
        r"\bplease\s+(?:clarify|provide|explain|expand\s+on)\b",
        r"\bdo\s+not\s+claim\s+(?:superiority|significance)\s+unless\b",
        r"\bmajor\s+revision\s+is\s+required\b",
        r"\bthis\s+paper\s+fails\s+to\s+compare\b",
        r"\bthe\s+methodology\s+section\s+needs\s+to\s+address\b",
    ]

    @classmethod
    def classify(cls, text: str) -> ClassificationResult:
        """Classify input into 'manuscript_draft' or 'reviewer_critique'."""
        signals: List[str] = []
        score = 0.0
        text_lower = text.lower()

        # Check explicit headers (highest weight)
        for pattern in cls._EXPLICIT_HEADER_PATTERNS:
            if re.search(pattern, text_lower):
                signals.append(f"Header match: {pattern}")
                score += 0.70

        # Check reviewer directive phrasing
        for pattern in cls._REVIEWER_DIRECTIVE_PATTERNS:
            if re.search(pattern, text_lower):
                signals.append(f"Directive phrase: {pattern}")
                score += 0.40

        is_reviewer = score >= 0.50
        confidence = min(score, 1.0)
        detected_intent = "reviewer_critique" if is_reviewer else "manuscript_draft"

        return ClassificationResult(
            is_reviewer_feedback=is_reviewer,
            confidence=confidence,
            matched_signals=signals,
            detected_intent=detected_intent,
        )
