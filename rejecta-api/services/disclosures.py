"""The paper's own competing-interest and funding statements, read by code (no model).

They are context for whoever reads the report: a disclosed patent or shareholding is relevant to how much weight
a claim can bear, and is not a fault. Nothing here changes a score.

Where the statements are: under a heading ("Competing interests", "Conflicts of interest", "Funding", "Notes"...),
after an inline label on the first page ("Competing Interests: P.C. is a director of ..."), or as a single
sentence anywhere ("The authors declare no competing financial interest.").
"""

from __future__ import annotations

import re

from models import Disclosures, ParsedDocument
from services.parser import _heading_title

MAX_STATEMENT_CHARS = 1200
MAX_STATEMENT_LINES = 25

_COI_TITLES = frozenset({
    "competing interests", "competing interest", "conflict of interest", "conflicts of interest",
    "declaration of competing interest", "competing financial interests", "disclosure", "disclosures",
    "declaration of interests", "declarations of interest", "financial disclosure", "conflict of interest statement",
    "conflicts of interest statement",
})
_FUNDING_TITLES = frozenset({"funding", "funding information", "funding sources", "financial support"})

_INLINE_COI = re.compile(
    r"(?i)^\s*(?:competing (?:financial )?interests?|conflicts? of interests?|declaration of (?:competing )?interests?"
    r"|disclosures?)\s*[:.]\s*(\S.*)$"
)
_INLINE_FUNDING = re.compile(r"(?i)^\s*(?:funding(?: information| sources)?|financial support)\s*[:.]\s*(\S.*)$")
_COI_SENTENCE = re.compile(
    r"(?i)[^.]{0,200}\b(?:competing (?:financial )?interests?|conflicts? of interests?)\b[^.]{0,200}\."
)
_NONE_DECLARED = re.compile(
    r"(?i)\b(?:no|not any|nothing to)\b[^.]{0,60}\b(?:competing|conflicts?|declare)|"
    r"\bdeclare[sd]?\s+(?:that\s+)?(?:they\s+have\s+)?no\b|\bnone\s+declared\b|\bnothing to disclose\b"
)
_UNDECLARED_BUT = re.compile(r"(?i)\bdoes not alter\b|\bhowever\b")

INTEREST_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("patent or inventorship", re.compile(r"(?i)\bpatent|\binventor")),
    ("stock or equity", re.compile(r"(?i)\bstock|\bshare ?holder|\bequity\b|\bshares in\b|\bstock options?\b")),
    ("employment by a company", re.compile(r"(?i)\bemploy(?:ee|ed|ment)\b")),
    ("consulting or advisory role", re.compile(r"(?i)\bconsult|\badvisory board|\badvis[eo]r\b")),
    ("company director, founder or officer", re.compile(r"(?i)\bfounder|\bdirector\b|\bofficer\b|\bboard member|\bCEO\b|\bCSO\b")),
    ("royalties, fees or honoraria", re.compile(r"(?i)\broyalt|\bhonorari|\bspeaker(?:'s)? fees?\b|\bpersonal fees\b")),
    ("company funding", re.compile(r"(?i)\b(?:funded|supported|sponsored) by [^.]{0,80}\b(?:Inc|Ltd|LLC|GmbH|Corp|Pharma\w*|Biotech\w*|Therapeutics|Resources|S\.A\.)\b")),
)


def _collect(lines: list[str], start: int, first: str) -> str:
    """The statement from `first` on, until the next heading or inline label."""
    parts = [first] if first else []
    for line in lines[start:start + MAX_STATEMENT_LINES]:
        if _heading_title(line) is not None or _INLINE_COI.match(line) or _INLINE_FUNDING.match(line):
            break
        parts.append(line.strip())
        if sum(len(p) for p in parts) > MAX_STATEMENT_CHARS:
            break
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:MAX_STATEMENT_CHARS]


def _statement(lines: list[str], titles: frozenset[str], inline: re.Pattern[str]) -> str | None:
    for index, line in enumerate(lines):
        title = _heading_title(line)
        if title in titles:
            text = _collect(lines, index + 1, "")
            if text:
                return text
        match = inline.match(line)
        if match:
            return _collect(lines, index + 1, match.group(1).strip())
    return None


def read_disclosures(parsed: ParsedDocument) -> Disclosures:
    lines = parsed.full_text.split("\n")
    coi = _statement(lines, _COI_TITLES, _INLINE_COI)
    if coi is None:  # "Notes: The authors declare no competing financial interest." and the like
        match = _COI_SENTENCE.search(re.sub(r"\s+", " ", parsed.full_text))
        coi = match.group(0).strip() if match else None
    funding = _statement(lines, _FUNDING_TITLES, _INLINE_FUNDING)
    interests = [kind for kind, pattern in INTEREST_KINDS if coi and pattern.search(coi)]
    if funding and not any(k == "company funding" for k in interests):
        if INTEREST_KINDS[-1][1].search(funding):
            interests.append("company funding")
    declares_none = bool(coi and _NONE_DECLARED.search(coi) and not interests and not _UNDECLARED_BUT.search(coi))
    return Disclosures(competing_interests=coi, funding=funding, declares_none=declares_none, interests=interests)
