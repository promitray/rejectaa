"""Reference parsing and fingerprint matching for citation verification.

Chemistry, physics and medicine usually cite without an article title:

    Grubbs, R. H. Angew. Chem. Int. Ed. 2006, 45, 3760-3765.          (ACS)
    R. H. Grubbs, Angew. Chem., Int. Ed., 2006, 45, 3760.             (RSC)
    Kolb HC, Finn MG. Angew Chem Int Ed. 2001;40(11):2004-21.         (Vancouver)
    Kolb, H. C. (2001). Title. Angew. Chem., 40(11), 2004-2021.       (APA)

The identity of such a paper is its fingerprint: first author + year + volume + first page.
Catalogs (CrossRef, Europe PMC, OpenAlex) all return those fields, so a candidate is accepted
when the fingerprint agrees, not when a guessed "title" looks similar. A relevance score alone
is never enough: a made-up reference still gets a plausible-looking nearest neighbour.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

YEAR_RE = r"(?:19|20)\d{2}"
_PAGE = r"[A-Za-z]{0,2}\d[\w\-–—]*"
MIN_TITLE_WORDS = 3
MIN_TITLE_CHARS = 15
YEAR_TOLERANCE = 1  # online-first vs issue year routinely differ by one

# volume/page tails, tried in order; each yields year, vol, page
_TAIL_PATTERNS = [
    # ACS / RSC / Nature:  2001, 40, 2004-2021   or   2017, 53(2), 157
    re.compile(
        rf"(?P<year>{YEAR_RE})[a-z]?\s*,\s*(?:vol\.?\s*)?(?P<vol>\d{{1,4}})\s*(?:\(\s*\w+\s*\))?\s*,\s*(?:pp?\.?\s*)?(?P<page>{_PAGE})"
    ),
    # Vancouver:  2001;40(11):2004-21
    re.compile(
        rf"(?P<year>{YEAR_RE})\s*;\s*(?P<vol>\d{{1,4}})\s*(?:\([^)]*\))?\s*:\s*(?P<page>{_PAGE})"
    ),
    # Elsevier:  40 (2001) 2004-2021
    re.compile(rf"(?P<vol>\d{{1,4}})\s*\(\s*(?P<year>{YEAR_RE})\s*\)\s*,?\s*(?P<page>{_PAGE})"),
    # no volume (the year is the volume):  Eur. J. Org. Chem., 2008, 5391-5400   Chem. Commun., 2008, 2052-2054
    re.compile(
        rf"(?P<vol>)(?P<year>{YEAR_RE})[a-z]?\s*,\s*(?P<page>\d{{3,6}}(?:[\-\u2013\u2014]\d{{1,6}})?)(?![\d,])"
    ),
]
# APA: year in parentheses early on, then "Journal, 40(11), 2004-2021"
_APA_YEAR = re.compile(rf"\(\s*(?P<year>{YEAR_RE})[a-z]?\s*\)")
_APA_TAIL = re.compile(rf"(?P<vol>\d{{1,4}})\s*(?:\(\s*\w+\s*\))?\s*,\s*(?:pp?\.?\s*)?(?P<page>{_PAGE})")
_DOI_OR_URL = re.compile(r"(?:https?://\S+|doi:\s*\S+|10\.\d{4,9}/\S+)", re.IGNORECASE)
_NUMERIC_PREFIX = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*")
_INITIAL = re.compile(r"(?:[A-Z]\.?-?){1,3}")


@dataclass
class ParsedReference:
    """What can be read off a bibliography line."""

    raw: str
    first_author: str | None = None  # simplified surname, letters only
    author_display: str | None = None  # surname as written, for catalog queries
    year: int | None = None
    volume: str | None = None
    first_page: str | None = None
    title: str | None = None  # only when the line really contains one

    @property
    def has_fingerprint(self) -> bool:
        return bool(self.first_author and self.year and (self.volume or self.first_page))


def clean_reference(raw: str) -> str:
    """Drop the list number, repair PDF line-break hyphens, collapse whitespace."""
    text = _NUMERIC_PREFIX.sub("", raw.strip())
    text = re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", text)  # "im- age" -> "image"
    return re.sub(r"\s+", " ", text).strip()


def simplify_name(value: str) -> str:
    """Letters only, no accents, lower case: 'Wedler-Jasinski' -> 'wedlerjasinski'."""
    decomposed = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z]", "", "".join(c for c in decomposed if not unicodedata.combining(c)).lower())


def _is_initial(token: str) -> bool:
    return bool(_INITIAL.fullmatch(token))


def extract_first_author_pair(cleaned: str) -> tuple[str | None, str | None]:
    """(simplified surname, surname as written) of the first author, for the common layouts."""
    head = re.split(r";|\s+and\s+|\s+&\s+|\s+et\s+al", cleaned[:160], maxsplit=1)[0]
    first = head.split(",")[0]
    if "," not in head:
        lead = re.findall(r"[^\s,]+", first)[:1]
        if lead and not _is_initial(lead[0].strip(".")):
            # "Grubbs RH. Angew Chem Int Ed. 2006": the author block ends at the first ". "
            first = re.split(r"\.\s", first, maxsplit=1)[0]
    tokens = [t for t in re.findall(r"[^\s,]+", first)]
    names = [t.strip(".") for t in tokens if not _is_initial(t)]
    names = [n for n in names if len(simplify_name(n)) >= 2]
    if not names:
        return None, None
    surname = simplify_name(names[-1])
    return (surname, names[-1]) if len(surname) >= 2 else (None, None)


def extract_first_author(cleaned: str) -> str | None:
    """Simplified surname of the first author."""
    return extract_first_author_pair(cleaned)[0]


def _first_page(page: str | None) -> str | None:
    if not page:
        return None
    first = re.split(r"[\-–—]", page.strip())[0].strip().lower()
    first = first.lstrip("0") or first
    return first or None


def _norm_volume(volume: str | None) -> str | None:
    if not volume:
        return None
    return volume.strip().lstrip("0") or volume.strip()


def extract_tail(cleaned: str) -> tuple[int | None, str | None, str | None, int | None]:
    """(year, volume, first_page, start_index) from the citation tail, or Nones."""
    text = _DOI_OR_URL.sub(" ", cleaned)
    for pattern in _TAIL_PATTERNS:
        match = pattern.search(text)
        if match:
            return (
                int(match.group("year")),
                _norm_volume(match.group("vol")),
                _first_page(match.group("page")),
                match.start(),
            )
    year_match = _APA_YEAR.search(text)
    if year_match:
        tail = _APA_TAIL.search(text, year_match.end())
        year = int(year_match.group("year"))
        if tail:
            # the tail start (not the year) so the title between them stays in the searched text
            return year, _norm_volume(tail.group("vol")), _first_page(tail.group("page")), tail.start()
        return year, None, None, year_match.start()
    plain = re.search(rf"\b({YEAR_RE})\b", text)
    if plain:
        return int(plain.group(1)), None, None, plain.start()
    return None, None, None, None


_DOTTED_INITIAL = re.compile(r"(?:[A-Z]\.-?){1,3}")
_AUTHOR_CHUNK = re.compile(
    r"^\s*(?:[A-Z][\w'\u2019\-]+\s+[A-Z]{1,3}"  # Vancouver:  Kolb HC
    r"|[A-Z][\w'\u2019\-]+,?\s+(?:[A-Z]\.\s*){1,3}"  # ACS:        Kolb, H. C.
    r"|(?:[A-Z]\.\s*){1,3}[A-Z][\w'\u2019\-]+)\.?\s*$"  # RSC:        H. C. Kolb
)


def _looks_like_author_list(text: str) -> bool:
    chunks = [c for c in re.split(r"[,;]| and ", text) if c.strip()]
    if len(chunks) < 2:
        return False
    return sum(bool(_AUTHOR_CHUNK.match(c)) for c in chunks) / len(chunks) >= 0.6


_VENUE_START = re.compile(r"^(?:in|proc|proceedings|advances|journal|annual|conference|transactions)\b", re.IGNORECASE)


_KEEP_AFTER_TAIL = re.compile(r"^(?:doi\b|https?:|pmid\b|pmc\b|epub\b|arxiv\b|10\.\d{4,9}/)", re.IGNORECASE)


def trim_after_citation(text: str) -> str:
    """Drop text glued after the end of a citation (a figure caption, a page header, the next note).

    A citation ends with its year, volume and pages. What follows is kept only if it is a DOI, a URL
    or a bracketed/parenthesised note; a new sentence starting with a capital letter is not part of it.
    """
    end: int | None = None
    for pattern in _TAIL_PATTERNS:
        match = pattern.search(text)
        if match:
            end = match.end()
            break
    if end is None:
        apa = _APA_YEAR.search(text)
        tail = _APA_TAIL.search(text, apa.end()) if apa else None
        end = tail.end() if tail else None
    if end is None:
        return text
    remainder = text[end:].lstrip(" .;,")
    if not remainder or _KEEP_AFTER_TAIL.match(remainder) or remainder[0] in "([\u2020\u2021*":
        return text
    if remainder[0].isupper():
        return text[:end].rstrip(" .;,") + "."
    return text


def looks_like_title(value: str | None) -> bool:
    """Reject 'titles' that are really a citation tail, an author list or a venue."""
    if not value:
        return False
    text = value.strip(" .;,")
    words = text.split()
    if len(words) < MIN_TITLE_WORDS or len(text) < MIN_TITLE_CHARS:
        return False
    letters = sum(c.isalpha() for c in text)
    if letters / max(1, len(text)) < 0.7:
        return False
    if re.match(rf"\W*{YEAR_RE}\b", text) or _VENUE_START.match(text):
        return False
    # initials carry a period ("A. Suzuki"); a bare "A" or "I" at the start of a title is a word
    initials = sum(1 for w in words if _DOTTED_INITIAL.fullmatch(w.strip(",;")))
    if initials >= 1 and len(words) <= 6:
        return False  # "A. Suzuki, Angew"
    if ";" in text and initials >= 2:
        return False  # an author list
    if _looks_like_author_list(text):
        return False
    if sum(1 for w in words if len(w.strip(".")) <= 4 and w.endswith(".")) >= max(2, len(words) // 2):
        return False  # "Angew. Chem. Int. Ed." abbreviation chain
    return True


def extract_title(cleaned: str, tail_start: int | None) -> str | None:
    """The article title, only if the reference really has one."""
    quoted = re.search(r'["“‘]([^"”’]{10,250})["”’]', cleaned)
    if quoted and looks_like_title(quoted.group(1)):
        return quoted.group(1).strip(" .,")

    body = cleaned[:tail_start] if tail_start else cleaned
    body = _DOI_OR_URL.sub(" ", body).strip()
    # sentences, without splitting after a single-letter initial ("K. B. Click ..." stays glued)
    parts = [p.strip() for p in re.split(r"(?<!\b[A-Za-z])\.\s+(?=[A-Z\[(])", body) if p.strip()]

    candidates: list[str] = []
    for part in parts:
        # authors and title can be glued: "..., Sharpless, K. B. Click Chemistry: Diverse ..."
        glued = re.match(r"^(?P<authors>.*\b[A-Z]\.)\s+(?P<title>[A-Z][^;]{12,})$", part)
        # when authors and title are glued, only the title half is a candidate
        candidates.append(glued.group("title") if glued else part)
    plausible = [c for c in candidates if looks_like_title(c)]
    if not plausible:
        return None
    # the title is the longest sentence-like piece; journal names and venues are shorter
    return max(plausible, key=lambda c: len(c.split())).strip(" .,")


def parse_reference(raw: str) -> ParsedReference:
    cleaned = clean_reference(raw)
    year, volume, page, tail_start = extract_tail(cleaned)
    author, author_display = extract_first_author_pair(cleaned)
    return ParsedReference(
        raw=raw,
        first_author=author,
        author_display=author_display,
        year=year,
        volume=volume,
        first_page=page,
        title=extract_title(cleaned, tail_start),
    )


# ---------------------------------------------------------------------------
# Candidates from catalogs, and the comparison
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """A catalog record reduced to the fields that identify a paper."""

    title: str | None = None
    families: list[str] = field(default_factory=list)  # simplified surnames, in order
    years: list[int] = field(default_factory=list)
    volume: str | None = None
    first_page: str | None = None
    journals: list[str] = field(default_factory=list)


def _years_from_parts(*blocks: Any) -> list[int]:
    years: list[int] = []
    for block in blocks:
        if isinstance(block, dict):
            parts = block.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                try:
                    years.append(int(parts[0][0]))
                except (TypeError, ValueError):
                    pass
    return years


def candidate_from_crossref(item: dict[str, Any]) -> Candidate:
    families = []
    for author in (item.get("author") or [])[:6]:
        if isinstance(author, dict):
            name = author.get("family") or author.get("name") or ""
            if name:
                families.append(simplify_name(str(name)))
    titles = item.get("title") or []
    journals = [str(t) for key in ("short-container-title", "container-title") for t in (item.get(key) or []) if t]
    return Candidate(
        title=str(titles[0]) if titles else None,
        families=[f for f in families if f],
        years=_years_from_parts(item.get("issued"), item.get("published"), item.get("published-print"),
                                item.get("published-online")),
        volume=_norm_volume(str(item["volume"])) if item.get("volume") else None,
        first_page=_first_page(str(item["page"])) if item.get("page") else None,
        journals=journals,
    )


def candidate_from_europepmc(record: dict[str, Any]) -> Candidate:
    author_string = str(record.get("authorString") or "")
    families = []
    for chunk in author_string.rstrip(".").split(",")[:6]:
        surname = chunk.strip().split(" ")[0] if chunk.strip() else ""
        if surname:
            families.append(simplify_name(surname))
    year = record.get("pubYear")
    return Candidate(
        title=str(record.get("title") or "").rstrip(".") or None,
        families=[f for f in families if f],
        years=[int(year)] if isinstance(year, (int, str)) and str(year).isdigit() else [],
        volume=_norm_volume(str(record["journalVolume"])) if record.get("journalVolume") else None,
        first_page=_first_page(str(record["pageInfo"])) if record.get("pageInfo") else None,
        journals=[str(record["journalTitle"])] if record.get("journalTitle") else [],
    )


def candidate_from_openalex(work: dict[str, Any]) -> Candidate:
    families = []
    for authorship in (work.get("authorships") or [])[:6]:
        name = ((authorship or {}).get("author") or {}).get("display_name")
        if name:
            families.append(simplify_name(str(name).split()[-1]))
    biblio = work.get("biblio") or {}
    source = ((work.get("primary_location") or {}).get("source") or {})
    year = work.get("publication_year")
    return Candidate(
        title=work.get("title") or work.get("display_name"),
        families=[f for f in families if f],
        years=[int(year)] if isinstance(year, int) else [],
        volume=_norm_volume(str(biblio["volume"])) if biblio.get("volume") else None,
        first_page=_first_page(str(biblio["first_page"])) if biblio.get("first_page") else None,
        journals=[str(source["display_name"])] if source.get("display_name") else [],
    )


@dataclass
class MatchReport:
    author: bool = False
    year: bool = False
    volume: bool = False
    page: bool = False
    journal: bool = False
    title_similarity: float = 0.0
    year_conflict: bool = False
    page_known: bool = False  # both the reference and the record state a first page
    page_conflict: bool = False  # ...and they differ
    author_close: bool = False  # surname spelled almost the same ("Makyla" / "Makya")
    title_in_line: bool = False  # the record's title appears word for word in the cited line

    @property
    def fingerprint(self) -> bool:
        """Does the record's fingerprint agree with the cited reference?

        The first page is what makes a citation specific: "Smith, 2019, vol. 141" fits hundreds of
        papers. So when both sides give a page it must agree (and a different page rules the record
        out), and then the author plus the year or the volume must agree too.

        Only when the catalog record carries no page at all, author + year + volume + a journal
        name that appears in the cited line is accepted.
        """
        if self.page_conflict or self.year_conflict:
            return False
        if self.page:
            if self.author:
                return bool(self.year or self.volume)
            # catalogs and PDFs spell names differently; a one-letter difference is accepted only
            # when year, volume AND first page all agree
            return bool(self.author_close and self.year and self.volume)
        if not self.page_known:
            return bool(self.author and self.year and self.volume and self.journal)
        return False

    @property
    def title_match(self) -> bool:
        return self.title_similarity >= 0.92

    @property
    def title_evidence(self) -> bool:
        """Title match: similar to the guessed title, or the full title is in the line with the author."""
        return bool(self.title_match or (self.title_in_line and self.author and not self.year_conflict))

    @property
    def verified(self) -> bool:
        return bool(self.fingerprint or self.title_evidence)

    @property
    def confidence(self) -> float:
        if self.fingerprint:
            hits = sum([self.author, self.year, self.volume, self.page])
            return 0.98 if hits == 4 else 0.93 if hits == 3 else 0.88
        if self.title_evidence:
            return 0.95 if self.title_match else 0.92
        return 0.0

    @property
    def matched_fields(self) -> int:
        return sum([self.author, self.year, self.volume, self.page])

    def differs_in(self) -> list[str]:
        names = {"author": self.author, "year": self.year, "volume": self.volume, "first page": self.page}
        return [name for name, ok in names.items() if not ok]


def _title_similarity(left: str, right: str) -> float:
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.lower())).strip()

    a, b = norm(left), norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _journal_hint(parsed_raw: str, journals: list[str]) -> bool:
    """Does the cited line contain one of the record's journal names (punctuation ignored)?"""
    flat = re.sub(r"[^a-z0-9 ]", " ", parsed_raw.lower())
    flat = re.sub(r"\s+", " ", flat)
    for journal in journals:
        name = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", journal.lower())).strip()
        if len(name) >= 4 and name in flat:
            return True
    return False


def compare(parsed: ParsedReference, candidate: Candidate) -> MatchReport:
    """How well does a catalog record agree with the cited reference?"""
    report = MatchReport()
    if parsed.first_author and candidate.families:
        wanted = parsed.first_author
        report.author = any(
            len(wanted) >= 3 and (family == wanted or family.endswith(wanted) or wanted.endswith(family) and len(family) >= 4)
            for family in candidate.families[:4]
        )
    if parsed.first_author and candidate.families and not report.author:
        wanted = parsed.first_author
        report.author_close = len(wanted) >= 4 and any(
            len(family) >= 4 and SequenceMatcher(None, wanted, family).ratio() >= 0.8
            for family in candidate.families[:4]
        )
    if candidate.title:
        title_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", candidate.title.lower())).strip()
        line_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", parsed.raw.lower())).strip()
        report.title_in_line = len(title_norm) >= 20 and len(title_norm.split()) >= 3 and title_norm in line_norm
    if parsed.year and candidate.years:
        gap = min(abs(parsed.year - y) for y in candidate.years)
        report.year = gap <= YEAR_TOLERANCE
        report.year_conflict = gap > YEAR_TOLERANCE
    if parsed.volume and candidate.volume:
        report.volume = parsed.volume == candidate.volume
    if parsed.first_page and candidate.first_page:
        report.page_known = True
        report.page = parsed.first_page == candidate.first_page
        report.page_conflict = not report.page
    if parsed.title and candidate.title:
        report.title_similarity = _title_similarity(parsed.title, candidate.title)
    report.journal = _journal_hint(parsed.raw, candidate.journals)
    return report
