"""Article-type profiles: which sections a paper of each type should have, and how it is scored.

The table comes from the chemistry editors who reviewed the product. A section that a type is not
expected to have (for example an Experimental section in a Communication, where it lives in the
Supporting Information) is "not applicable", never a weakness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNKNOWN = "unknown"
RESEARCH_ARTICLE = "research_article"
COMMUNICATION = "communication"
PERSPECTIVE = "perspective"
REVIEW = "review"
MINI_REVIEW = "mini_review"
ARTICLE_TYPES = (RESEARCH_ARTICLE, COMMUNICATION, PERSPECTIVE, REVIEW, MINI_REVIEW)
THEMATIC_TYPES = (PERSPECTIVE, REVIEW, MINI_REVIEW)  # body is split by topic, not Results/Methods

# Section roles the analysis understands. A "thematic" section is any topic heading of a review.
ROLE_INTRODUCTION = "introduction"
ROLE_METHODS = "methods"
ROLE_RESULTS = "results"
ROLE_DISCUSSION = "discussion"  # Conclusion / Outlook
ROLE_THEMATIC = "thematic"
CANONICAL_ROLES = (ROLE_INTRODUCTION, ROLE_METHODS, ROLE_RESULTS, ROLE_DISCUSSION)
ALL_ROLES = (*CANONICAL_ROLES, ROLE_THEMATIC, "other")

# Scored criteria. citation_integrity is computed from the citation check, never by the model.
CITATION_CRITERION = "citation_integrity"

_RESEARCH_CRITERIA: tuple[tuple[str, float], ...] = (
    ("novelty", 25),
    ("rigor", 25),
    ("evidence_support", 20),
    ("clarity", 10),
    ("journal_fit", 10),
)
_REVIEW_CRITERIA: tuple[tuple[str, float], ...] = (
    ("coverage_balance", 25),
    ("critical_synthesis", 25),
    ("outlook", 15),
    ("clarity", 15),
    ("journal_fit", 10),
)
CITATION_WEIGHT = 10.0

CRITERION_HELP = {
    "novelty": "what is new compared with the work the paper itself cites",
    "rigor": "whether the methods and controls support the claims",
    "evidence_support": "whether the data shown actually back each conclusion",
    "clarity": "organisation and readability",
    "journal_fit": "fit with the target journal's scope and audience",
    "coverage_balance": "breadth and balance of the literature covered",
    "critical_synthesis": "whether the paper compares and judges work instead of listing it",
    "outlook": "whether open questions and future directions are concrete",
}


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    expected: tuple[str, ...]  # roles the paper should contain (as content; headings may be absent)
    not_expected: tuple[str, ...]  # roles that are normal to be absent
    criteria: tuple[tuple[str, float], ...]


PROFILES: dict[str, Profile] = {
    RESEARCH_ARTICLE: Profile(
        RESEARCH_ARTICLE, "Full paper / research article",
        expected=(ROLE_INTRODUCTION, ROLE_RESULTS, ROLE_METHODS, ROLE_DISCUSSION),
        not_expected=(ROLE_THEMATIC,),
        criteria=_RESEARCH_CRITERIA,
    ),
    COMMUNICATION: Profile(
        COMMUNICATION, "Communication / letter",
        # Introduction, results and conclusion are present but usually without headings;
        # the Experimental section is in the Supporting Information.
        expected=(ROLE_INTRODUCTION, ROLE_RESULTS, ROLE_DISCUSSION),
        not_expected=(ROLE_METHODS, ROLE_THEMATIC),
        criteria=_RESEARCH_CRITERIA,
    ),
    PERSPECTIVE: Profile(
        PERSPECTIVE, "Perspective",
        expected=(ROLE_INTRODUCTION, ROLE_THEMATIC, ROLE_DISCUSSION),
        not_expected=(ROLE_RESULTS, ROLE_METHODS),
        criteria=_REVIEW_CRITERIA,
    ),
    REVIEW: Profile(
        REVIEW, "Review article",
        expected=(ROLE_INTRODUCTION, ROLE_THEMATIC, ROLE_DISCUSSION),
        not_expected=(ROLE_RESULTS, ROLE_METHODS),
        criteria=_REVIEW_CRITERIA,
    ),
    MINI_REVIEW: Profile(
        MINI_REVIEW, "Mini-review",
        expected=(ROLE_INTRODUCTION, ROLE_THEMATIC, ROLE_DISCUSSION),
        not_expected=(ROLE_RESULTS, ROLE_METHODS),
        criteria=_REVIEW_CRITERIA,
    ),
    # Type not known: judge by the research criteria, expect nothing in particular.
    UNKNOWN: Profile(UNKNOWN, "Type not determined", expected=(), not_expected=(), criteria=_RESEARCH_CRITERIA),
}


def get_profile(article_type: str | None) -> Profile:
    return PROFILES.get(article_type or UNKNOWN, PROFILES[UNKNOWN])


def coerce_article_type(value: object) -> str | None:
    """A valid article type or None. Accepts the API keys and common spellings."""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-/]+", "_", value.strip().lower())
    aliases = {
        "full_paper": RESEARCH_ARTICLE, "article": RESEARCH_ARTICLE, "research": RESEARCH_ARTICLE,
        "original_research": RESEARCH_ARTICLE, "paper": RESEARCH_ARTICLE,
        "letter": COMMUNICATION, "letters": COMMUNICATION, "communications": COMMUNICATION,
        "minireview": MINI_REVIEW, "mini": MINI_REVIEW, "review_article": REVIEW,
    }
    key = aliases.get(key, key)
    return key if key in ARTICLE_TYPES else None


# A label on the first page ("COMMUNICATION", "Mini-review") names the article type.
_LABELS: dict[str, str] = {
    "FULL PAPER": RESEARCH_ARTICLE, "RESEARCH ARTICLE": RESEARCH_ARTICLE, "ORIGINAL ARTICLE": RESEARCH_ARTICLE,
    "ORIGINAL RESEARCH": RESEARCH_ARTICLE, "ORIGINAL PAPER": RESEARCH_ARTICLE, "ARTICLE": RESEARCH_ARTICLE,
    "PAPER": RESEARCH_ARTICLE,
    "COMMUNICATION": COMMUNICATION, "COMMUNICATIONS": COMMUNICATION, "LETTER": COMMUNICATION,
    "LETTERS": COMMUNICATION, "RAPID COMMUNICATION": COMMUNICATION, "SHORT COMMUNICATION": COMMUNICATION,
    "PERSPECTIVE": PERSPECTIVE, "PERSPECTIVES": PERSPECTIVE,
    "REVIEW": REVIEW, "REVIEW ARTICLE": REVIEW, "TUTORIAL REVIEW": REVIEW, "CRITICAL REVIEW": REVIEW,
    "MINI-REVIEW": MINI_REVIEW, "MINIREVIEW": MINI_REVIEW, "MINI REVIEW": MINI_REVIEW,
}
_LABEL_SCAN_LINES = 60


def detect_article_type(first_page_lines: list[str]) -> str | None:
    """The article type named by a label line near the top of the first page, if there is one."""
    for line in first_page_lines[:_LABEL_SCAN_LINES]:
        text = re.sub(r"\s+", " ", line).strip().rstrip(":.").upper()
        if 3 <= len(text) <= 24 and text in _LABELS:
            return _LABELS[text]
    return None


# A paper that points to its Supporting Information / ESI has details that were not uploaded.
_SI_REFERENCE = re.compile(
    r"(?:\bESI\b|supporting\s+information|supplementary\s+(?:information|material|data)"
    r"|\b(?:Fig(?:ure)?s?\.?|Table|Scheme)\s*S\d+)",
    re.IGNORECASE,
)


def refers_to_si(text: str) -> bool:
    return bool(_SI_REFERENCE.search(text))


def assess_structure(
    section_roles: dict[str, str], section_chars: int, body_chars: int, article_type: str | None
) -> tuple[str, float]:
    """How much of the paper was sorted into headed sections: ("detected" | "partial" | "none", coverage)."""
    coverage = min(1.0, section_chars / body_chars) if body_chars > 0 else 0.0
    if not section_roles:
        return "none", 0.0
    found = set(section_roles.values())
    thematic = sum(1 for role in section_roles.values() if role == ROLE_THEMATIC)
    if article_type in THEMATIC_TYPES:
        ok = ROLE_INTRODUCTION in found and thematic >= 2
    else:
        ok = len(found & set(CANONICAL_ROLES)) >= 3
    if ok and coverage >= 0.6:
        return "detected", coverage
    return "partial", coverage
