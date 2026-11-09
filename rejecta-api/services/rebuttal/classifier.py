"""services/rebuttal/classifier.py
Four-category diplomatic posture classifier for peer review comments:
1. AGREE_AND_AMEND: Valid critique requiring manuscript addition or change.
2. CLARIFY_MISCONCEPTION: Reviewer misunderstood or missed existing content; responsibility-first clarification.
3. PARTIAL_AGREE_BOUNDED: Critique has valid direction, but demanded experiment is infeasible or out-of-scope; counter-propose bounded alternative.
4. CONTEST_WITH_EVIDENCE: Reviewer makes a factually incorrect claim or invalid assumption; respectful evidence-backed defense.

Features conservative prior: defaults toward CLARIFY unless factual-error signals reach high confidence (>= 0.75).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.llm.base import AbstractAnalysisProvider
    from services.rebuttal.dissector import RawCommentItem

logger = logging.getLogger(__name__)

PostureType = Literal[
    "agree_and_amend",
    "clarify_misconception",
    "partial_agree_bounded",
    "contest_with_evidence",
]

_VALID_CATEGORIES = (
    "agree_and_amend",
    "clarify_misconception",
    "partial_agree_bounded",
    "contest_with_evidence",
)

MAX_COMMENT_CHARS_FOR_CLASSIFIER = 1500


@dataclass
class PostureClassification:
    category: PostureType
    confidence: float
    diplomatic_posture_label: str
    matched_signals: List[str]
    suggested_strategy_notes: str
    # What the reviewer asks the authors to do or address. Filled by the LLM
    # classifier and passed to the drafting prompt so no request is reduced to a
    # generic apology.
    reviewer_requests: List[str] = field(default_factory=list)


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
        # Missing detail / characterization / reproducibility (any experimental field)
        r"\b(?:without|lacks?|lacking|insufficient|limited|inadequate)\s+(?:sufficient\s+)?(?:detail|characteri[sz]ation|validation|description)\b",
        r"\b(?:not|insufficiently)\s+(?:well\s+|sufficiently\s+|fully\s+)?(?:characteri[sz]ed|validated)\b",
        r"\breproducib",
    ]

    # Novelty / significance concerns: the author cannot simply agree (it is a judgement
    # about the contribution) and cannot invent evidence, so the response is bounded:
    # acknowledge, then position the contribution using only supplied facts.
    _NOVELTY_PATTERNS = [
        r"\b(?:insufficient|limited|little|marginal|lacks?|lacking)\s+(?:novelty|originality|conceptual\s+advance|significance)\b",
        r"\bnovelty\s+(?:is|are|seems?|appears?)\s+(?:\w+\s+)?(?:insufficient|limited|unclear|lacking|marginal)\b",
        r"\b(?:incremental|not\s+(?:sufficiently\s+|particularly\s+)?novel)\b",
        r"\bdoes\s+not\s+(?:clearly\s+)?(?:advance|differentiate)\b",
    ]

    # Venue / scope-of-journal concerns (transfer suggestions, fit).
    _VENUE_PATTERNS = [
        r"\b(?:better|more)\s+suited\s+(?:to|for)\b",
        r"\bmore\s+appropriate\s+for\b",
        r"\b(?:specialized|specialised|specialist|field[-\s]specific|discipline[-\s]specific)\s+(?:\w+\s+){0,2}(?:journal|venue|outlet)\b",
        r"\bconsider\s+(?:submitting|transferring)\b",
        r"\b(?:outside|beyond)\s+the\s+(?:aims\s+and\s+)?scope\s+of\s+(?:this|the)\s+journal\b",
        r"\bdesk\s+reject",
        # Indirect: doubts about THIS journal without naming an alternative.
        r"\breservations?\s+about\s+(?:its\s+|the\s+|a\s+)?publication\s+in\b",
        r"\bnot\s+(?:suitable|appropriate|suited|a\s+good\s+fit|the\s+right\s+(?:venue|journal))\s+for\s+(?:publication\s+in\b|this\s+journal\b)",
        r"\b(?:fit|match)\s+(?:for|with)\s+(?:this|the)\s+journal\b",
        r"\b(?:does\s+not|doesn'?t)\s+(?:meet|reach)\s+the\s+(?:bar|standard|threshold)\s+(?:for|of)\b",
    ]

    _POSTURE_LABELS: dict[PostureType, str] = {
        "agree_and_amend": "Humble Agreement & Revision Commitment",
        "clarify_misconception": "Responsibility-First Clarification",
        "partial_agree_bounded": "Bounded Agreement with Counter-Proposal",
        "contest_with_evidence": "Respectful Evidence-Backed Defence",
    }

    _STRATEGY_NOTES: dict[PostureType, str] = {
        "agree_and_amend": "Accept the valid critique, thank the reviewer, and name the revision the authors will make.",
        "clarify_misconception": "Take responsibility for any ambiguity, explain the point using only supplied facts, and name the clarifying change.",
        "partial_agree_bounded": "Acknowledge the merit of the concern, state what can and cannot be addressed in this work, and offer a concrete bounded alternative using only supplied facts.",
        "contest_with_evidence": "Validate the reviewer's perspective, then respond firmly using only evidence the authors supplied, with [CITATION NEEDED] placeholders for anything external.",
    }

    _CLASSIFIER_SYSTEM_PROMPT = (
        "You classify peer-review comments for an author drafting a rebuttal. The manuscript can be from ANY "
        "field (chemistry, biology, medicine, engineering, computer science, social science, humanities). "
        "Do not assume a field.\n\n"
        "Assign each comment exactly one category:\n"
        "- agree_and_amend: the critique is valid and the authors can fix it by adding, correcting or "
        "documenting something (missing detail, characterization, controls, comparisons, statistics, references).\n"
        "- clarify_misconception: the reviewer appears to have misread or missed something already in the "
        "manuscript, or the text is genuinely unclear. Use this ONLY when the answer is already available or "
        "the point is purely about wording.\n"
        "- partial_agree_bounded: the concern has merit but cannot be fully met: out-of-scope or infeasible "
        "requests, subjective novelty or significance judgements, journal-fit or transfer suggestions, or "
        "requests that need evidence the authors have not supplied.\n"
        "- contest_with_evidence: the reviewer states something factually or logically wrong AND the author "
        "evidence supplied below can rebut it. If the author evidence is empty, never choose this category.\n\n"
        "Also list `reviewer_requests`: each concrete thing the reviewer asks the authors to do, change, "
        "justify or consider (including suggestions such as submitting to a different journal). Keep each "
        "under 20 words.\n\n"
        "Return ONLY a JSON object: "
        '{"classifications": [{"comment_id": str, "category": str, "confidence": number 0-1, '
        '"reviewer_requests": [str]}]} with one entry per comment_id. No prose, no markdown fences.'
    )

    @classmethod
    def _build_classification(
        cls,
        category: PostureType,
        confidence: float,
        signals: List[str],
        reviewer_requests: List[str],
    ) -> PostureClassification:
        return PostureClassification(
            category=category,
            confidence=confidence,
            diplomatic_posture_label=cls._POSTURE_LABELS[category],
            matched_signals=signals,
            suggested_strategy_notes=cls._STRATEGY_NOTES[category],
            reviewer_requests=reviewer_requests,
        )

    @classmethod
    async def classify_batch_llm(
        cls,
        llm: "AbstractAnalysisProvider",
        items: "List[RawCommentItem]",
        manuscript_context: str | None,
    ) -> "List[PostureClassification | None]":
        """Classify all comments with ONE LLM call.

        Returns a list aligned with `items`. An entry is None when the model gave no valid
        answer for that comment; the caller then falls back to the regex classifier. If the
        whole call fails, every entry is None.
        """
        if not items:
            return []

        evidence = (manuscript_context or "").strip()
        payload = [
            {"comment_id": item.comment_id, "text": item.raw_text[:MAX_COMMENT_CHARS_FOR_CLASSIFIER]}
            for item in items
        ]
        user_prompt = (
            f"Author evidence supplied (may be empty):\n{evidence or '[EMPTY]'}\n\n"
            f"Comments to classify (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return the JSON object now."
        )

        try:
            raw = await llm.generate(
                system_prompt=cls._CLASSIFIER_SYSTEM_PROMPT, user_prompt=user_prompt, temperature=0.0
            )
            data = cls._parse_json(raw)
        except Exception as exc:  # network, provider or JSON failure: regex fallback takes over
            logger.warning("LLM classification failed (%s); using regex classifier.", exc)
            return [None] * len(items)

        entries = data.get("classifications") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            logger.warning("LLM classification returned an unexpected shape; using regex classifier.")
            return [None] * len(items)

        by_id: dict[str, dict] = {
            str(entry.get("comment_id")): entry for entry in entries if isinstance(entry, dict)
        }
        results: "List[PostureClassification | None]" = []
        for item in items:
            entry = by_id.get(item.comment_id)
            category = entry.get("category") if entry else None
            if category not in _VALID_CATEGORIES:
                results.append(None)
                continue
            try:
                confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.7))))
            except (TypeError, ValueError):
                confidence = 0.7
            requests_raw = entry.get("reviewer_requests") or []
            requests = [str(r).strip() for r in requests_raw if str(r).strip()][:6] if isinstance(requests_raw, list) else []
            results.append(cls._build_classification(category, confidence, ["LLM classification"], requests))
        return results

    @staticmethod
    def _parse_json(raw: str):
        """Parse JSON, tolerating markdown fences around it."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:] if lines[0].startswith("```") else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)

    @classmethod
    def apply_evidence_gate(
        cls, classification: PostureClassification, has_evidence: bool
    ) -> PostureClassification:
        """You cannot contest a point without evidence: downgrade to a bounded response."""
        if classification.category != "contest_with_evidence" or has_evidence:
            return classification
        return cls._build_classification(
            "partial_agree_bounded",
            min(classification.confidence, 0.70),
            [*classification.matched_signals, "Evidence gate: no author evidence supplied, contest downgraded"],
            classification.reviewer_requests,
        )

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

        # 1b. Novelty / venue-fit concerns: bounded, never "contest" and never a bare apology
        concern_signals = [
            f"Novelty concern: {pat}" for pat in cls._NOVELTY_PATTERNS if re.search(pat, text_lower)
        ] + [
            f"Venue-fit concern: {pat}" for pat in cls._VENUE_PATTERNS if re.search(pat, text_lower)
        ]
        if concern_signals:
            signals.extend(concern_signals)
            return PostureClassification(
                category="partial_agree_bounded",
                confidence=0.70,
                diplomatic_posture_label=cls._POSTURE_LABELS["partial_agree_bounded"],
                matched_signals=signals,
                suggested_strategy_notes=cls._STRATEGY_NOTES["partial_agree_bounded"],
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
