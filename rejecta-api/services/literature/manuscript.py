"""What the literature search knows about the uploaded manuscript: title, abstract, byline, its own identifiers,
its references, and the paper hash its W snapshot is stored under.

The byline is read from the first page by code: the lines between the title and the abstract are split at
commas, "and", "&", affiliation marks (digits, *, †) and the names kept; lines that name an institution are the
affiliations; "Correspondence to: <name>" adds names. A byline of "Anonymous" (or none) marks the manuscript as
anonymised, so authors cannot be resolved.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from services.citations import ARXIV_ID_PATTERN, DOI_PATTERN, trim_doi
from services.local_store import digest

MAX_AUTHORS = 12
MAX_AFFILIATIONS = 8
_INSTITUTION = re.compile(
    r"(?i)\b(universit|institut|department|dept\b|college|laborator|school|cent(?:er|re)|hospital|academy|faculty|"
    r"inc\b|ltd\b|gmbh|corporation|company|research)"
)
_NOT_A_NAME = re.compile(
    r"(?i)\b(abstract|article|letter|communication|review|received|accepted|published|doi|https?|www|journal|"
    r"correspondence|email|keywords?|introduction|copyright|license|vol\b|issue|pages?|open access|"
    r"creative commons|cite this|citation|canada|china|usa|germany|france|japan|india|italy|spain|quebec|street|"
    r"avenue|road)\b"
)
_NAME = re.compile(r"^[A-Z][A-Za-z'’\-\.]+(?:\s+(?:[A-Z][A-Za-z'’\-\.]*|van|von|de|der|da|di|le|la)){1,3}$")
_SPLIT = re.compile(r"\s*(?:[,;*†‡§¶#]|\d+|\band\b|&)\s*")
_CORRESPONDENCE = re.compile(r"(?i)correspondence to:\s*([^\n]+)")


@dataclass
class Manuscript:
    paper_hash: str  # the W snapshot key: the hash of the paper's text
    title: str
    abstract: str
    author_names: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    anonymised: bool = False
    dois: list[str] = field(default_factory=list)  # the manuscript's own DOIs (published or preprint)
    arxiv_ids: list[str] = field(default_factory=list)
    reference_texts: list[str] = field(default_factory=list)
    reference_dois: list[str] = field(default_factory=list)
    venue_statements: list[str] = field(default_factory=list)
    front_text: str = ""
    publication: Any = None  # models.PublicationStatus, when checked
    claims: list[str] = field(default_factory=list)  # the claim ledger's statements, when available

    def query_text(self) -> str:
        """What the manuscript is about, for embedding: title, abstract and (when known) its claims."""
        parts = [self.title, self.abstract, *self.claims[:6]]
        return "\n\n".join(p for p in parts if p).strip()


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", ascii_fold(text).lower())


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", ascii_fold(raw)).strip(" .,-")
    return name


def _is_name(name: str) -> bool:
    """A person's name: 2-4 capitalised words, at most one initial with a dot ("Stan Z. Li", not "Beilstein J. Org.")."""
    if not (_NAME.match(name) and 4 <= len(name) <= 40):
        return False
    abbreviations = sum(1 for t in name.split() if t.endswith(".") and len(t) > 2)
    return abbreviations == 0 and sum(1 for t in name.split() if t.endswith(".")) <= 2


def extract_byline(front_text: str, title: str = "", abstract: str = "") -> tuple[list[str], list[str], bool]:
    """(author names, affiliation lines, anonymised) from the first page."""
    lines = [line.strip() for line in (front_text or "").split("\n")]
    start = 0
    if title:
        # the title as printed: the run of lines that are pieces of the title; the byline follows it
        title_key = _key(title)
        for i, line in enumerate(lines[:40]):
            key = _key(line)
            if len(key) >= 12 and key in title_key:
                j = i
                while j + 1 < len(lines) and _key(lines[j + 1]) and _key(lines[j + 1]) in title_key:
                    j += 1
                start = j + 1
                break
    end = len(lines)
    abstract_start = ascii_fold(abstract[:40]).lower() if abstract else ""
    for i in range(start, min(len(lines), start + 40)):
        line = lines[i]
        lowered = ascii_fold(line).lower()
        if re.match(r"(?i)^\s*abstract\b", line) or (abstract_start and lowered.startswith(abstract_start[:25])) or len(line) > 160:
            end = i
            break
    block = lines[start:end][:25]
    names: list[str] = []
    affiliations: list[str] = []
    for line in block:
        if not line:
            continue
        if _INSTITUTION.search(line) and len(line) > 12:
            if len(affiliations) < MAX_AFFILIATIONS:
                affiliations.append(re.sub(r"^\W*\d*\s*", "", line)[:200])
            continue
        if _NOT_A_NAME.search(line):
            continue
        for piece in _SPLIT.split(line):
            name = _clean_name(piece)
            if _is_name(name) and name not in names:
                names.append(name)
    for found in _CORRESPONDENCE.findall(front_text or ""):
        for piece in re.split(r"[,;]|\band\b", re.sub(r"<[^>]*>", "", found)):
            name = _clean_name(piece)
            if _is_name(name) and name not in names:
                names.append(name)
    # "Stan Z" from a broken "Stan Z. Li1": a name that is the start of another is the same person
    names = [n for n in names if not any(o != n and o.startswith(n) for o in names)]
    anonymised = not names or bool(re.search(r"(?i)\banonymous\b", " ".join(block)))
    if anonymised:
        names = []
    return names[:MAX_AUTHORS], affiliations, anonymised


def _dois(texts: list[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        for raw in re.findall(DOI_PATTERN, text or ""):
            doi = trim_doi(raw).lower()
            if doi and doi not in out:
                out.append(doi)
    return out


def from_parsed(parsed: Any, publication: Any = None, claims: list[str] | None = None) -> Manuscript:
    """The manuscript as the literature search needs it, from a ParsedDocument (and the publication record)."""
    names, affiliations, anonymised = extract_byline(parsed.front_text, parsed.title, parsed.abstract)
    own_dois = [d.lower() for d in (parsed.doi_candidates or [])]
    if publication is not None and getattr(publication, "doi", None):
        doi = str(publication.doi).lower()
        if doi not in own_dois:
            own_dois.append(doi)
    arxiv: list[str] = []
    for text in [*(parsed.venue_statements or []), parsed.front_text or ""]:
        for found in ARXIV_ID_PATTERN.findall(text):
            ident = re.sub(r"v\d+$", "", found)
            if ident not in arxiv:
                arxiv.append(ident)
    return Manuscript(
        paper_hash=digest(parsed.full_text),
        title=parsed.title or "",
        abstract=parsed.abstract or "",
        author_names=names,
        affiliations=affiliations,
        anonymised=anonymised,
        dois=own_dois,
        arxiv_ids=arxiv,
        reference_texts=list(parsed.references or []),
        reference_dois=_dois(list(parsed.references or [])),
        venue_statements=list(parsed.venue_statements or []),
        front_text=parsed.front_text or "",
        publication=publication,
        claims=list(claims or []),
    )
