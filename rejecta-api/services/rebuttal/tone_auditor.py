"""services/rebuttal/tone_auditor.py
Tone auditing engine for academic rebuttal responses:
- Flags aggressive, adversarial, or condescending phrasing.
- Flags submissive over-concession ("we completely agree with all demands").
- Verifies respectful diplomatic opening and closure.
- Computes calibrated diplomatic score (0-100).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class ToneAudit:
    is_clean: bool
    diplomatic_score: int
    flags: List[str]
    positive_cues: List[str]


class ToneAuditor:
    """Audits generated or authored rebuttal responses for diplomatic compliance."""

    # Aggressive / Confrontational phrases (high penalty: -25 pts each)
    _AGGRESSIVE_PHRASES = [
        r"\bthe\s+reviewer\s+(?:clearly\s+)?failed\s+to\s+(?:read|understand|see|notice)\b",
        r"\bclearly\s+did\s+not\s+(?:read|examine|review)\b",
        r"\b(?:simply|obviously|patently)\s+(?:wrong|incorrect|false)\b",
        r"\bcompletely\s+(?:incorrect|baseless|unfounded)\b",
        r"\birrelevant\s+(?:comment|point|concern|critique)\b",
        r"\bthis\s+criticism\s+is\s+(?:unfair|unreasonable|absurd)\b",
        r"\btrivial\s+(?:concern|issue|matter)\b",
        r"\bthe\s+reviewer\s+missed\s+the\s+point\b",
        r"\bas\s+any\s+(?:expert|practitioner)\s+knows\b",
        r"\bwe\s+categorically\s+reject\b",
    ]

    # Submissive / Over-concession phrases (moderate penalty: -12 pts each)
    _SUBMISSIVE_PHRASES = [
        r"\bwe\s+completely\s+agree\s+with\s+all\s+(?:points|criticisms|demands)\b",
        r"\bwe\s+will\s+redo\s+the\s+entire\s+(?:analysis|study|paper)\b",
        r"\bwe\s+apologize\s+profusely\b",
        r"\bour\s+original\s+method\s+was\s+flawed\b",
        r"\bwe\s+unreservedly\s+accept\s+all\b",
    ]

    # Diplomatic, constructive openers (+6 pts each, max +12)
    _DIPLOMATIC_OPENERS = [
        r"\bwe\s+thank\s+the\s+reviewer\b",
        r"\bwe\s+greatly\s+appreciate\b",
        r"\bwe\s+are\s+grateful\s+for\b",
        r"\bthis\s+is\s+an\s+insightful\s+(?:point|observation|comment|suggestion)\b",
        r"\bwe\s+appreciate\s+the\s+opportunity\s+to\s+clarify\b",
        r"\bwe\s+agree\s+that\s+(?:clarifying|strengthening|expanding)\b",
    ]

    # Stock phrases that make every response read the same (-5 pts each).
    _BOILERPLATE_PHRASES = [
        r"\bapologi[sz]e\s+if\s+our\s+original\s+phrasing\b",
        r"\brefining\s+our\s+empirical\s+evaluations?\b",
        r"\bwas\s+not\s+sufficiently\s+clear\b",
    ]

    @staticmethod
    def _content_words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z][a-z\-]{4,}", text.lower())}

    @classmethod
    def audit(cls, response_text: str, reviewer_requests: List[str] | None = None) -> ToneAudit:
        """Audit drafted response text for diplomatic posture and academic decorum.

        Args:
            response_text: The drafted author response.
            reviewer_requests: Concrete asks the reviewer made. When given, a response that
                actually engages with them scores slightly higher than a generic one.
        """
        text = response_text.strip()
        if not text:
            return ToneAudit(
                is_clean=False,
                diplomatic_score=0,
                flags=["Empty response text"],
                positive_cues=[],
            )

        text_lower = text.lower()
        flags: List[str] = []
        positive_cues: List[str] = []
        score = 85  # Standard baseline for respectful scientific prose

        # 1. Check aggressive phrases
        for pat in cls._AGGRESSIVE_PHRASES:
            match = re.search(pat, text_lower)
            if match:
                snippet = match.group(0)
                flags.append(f"aggressive_language: '{snippet}'")
                score -= 25

        # 2. Check submissive phrases
        for pat in cls._SUBMISSIVE_PHRASES:
            match = re.search(pat, text_lower)
            if match:
                snippet = match.group(0)
                flags.append(f"submissive_overconcession: '{snippet}'")
                score -= 12

        # 3. Check abrupt confrontation without acknowledgment
        if re.match(r"^\s*(?:we\s+disagree|this\s+is\s+incorrect|no,\s+the)", text_lower):
            flags.append("abrupt_opening: Direct disagreement without prior acknowledgment phrase")
            score -= 15

        # 4. Check positive diplomatic framing
        diplomatic_hits = 0
        for pat in cls._DIPLOMATIC_OPENERS:
            if re.search(pat, text_lower):
                positive_cues.append(pat)
                diplomatic_hits += 1

        if diplomatic_hits > 0:
            score += min(12, diplomatic_hits * 6)

        # 4b. Stock phrases that make responses interchangeable
        for pat in cls._BOILERPLATE_PHRASES:
            match = re.search(pat, text_lower)
            if match:
                flags.append(f"boilerplate_phrase: '{match.group(0)}'")
                score -= 5

        # 4c. Reward engaging with what the reviewer actually asked for
        if reviewer_requests:
            response_words = cls._content_words(text)
            for request in reviewer_requests:
                request_words = cls._content_words(request)
                if request_words and len(request_words & response_words) / len(request_words) >= 0.4:
                    positive_cues.append(f"addresses_request: {request[:40]}")
                    score += 5
                    break

        # 5. Length sanity check
        words = len(text.split())
        if words < 12:
            flags.append("underdeveloped_response: Response contains fewer than 12 words")
            score -= 15

        final_score = max(0, min(100, score))
        is_clean = final_score >= 70 and not any(f.startswith("aggressive_language") for f in flags)

        return ToneAudit(
            is_clean=is_clean,
            diplomatic_score=final_score,
            flags=flags,
            positive_cues=positive_cues,
        )
