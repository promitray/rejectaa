"""Citation Autopsy: DOI, arXiv id, and title/author verification."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import statistics
from datetime import date
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import httpx

from models import CitationResult, CitationStatus, CitationSummary, PublicationType

logger = logging.getLogger(__name__)

CROSSREF_BASE_URL = "https://api.crossref.org/works"
CROSSREF_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
DATACITE_DOI_URL = "https://api.datacite.org/dois"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 8.0
MAX_REFERENCES = 60
LOOKUP_CONCURRENCY = 8
CROSSREF_CONCURRENCY = 2
CROSSREF_GAP_SECONDS = 0.25

ARXIV_DOI_PREFIX = "10.48550/arxiv."
ARXIV_ABS_URL = "https://export.arxiv.org/abs/"
ARXIV_SEARCH_URL = "https://export.arxiv.org/api/query"
ARXIV_DOI_TEMPLATE = "10.48550/arXiv.{arxiv_id}"

DOI_PATTERN = r"10\.\d{4,9}/[^\s,\]>\"'\)]+"
DOI_TRAILING_PUNCTUATION = ".,);:"
ARXIV_ID_PATTERN = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)"
    r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+/\d{7})",
    re.IGNORECASE,
)
CROSSREF_MIN_SCORE = 40.0
TITLE_SIMILARITY_MIN = 0.72
MIN_TITLE_CHARS = 12


def _is_arxiv_doi(doi: str) -> bool:
    """Return True if this DOI is an arXiv preprint DOI."""
    return doi.lower().startswith(ARXIV_DOI_PREFIX)


def _arxiv_id_from_doi(doi: str) -> str | None:
    """Extract the arXiv ID from an arXiv DOI."""
    for marker in ("arXiv.", "arxiv."):
        parts = doi.split(marker, maxsplit=1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return None


def _extract_doi(text: str) -> str | None:
    """Extract a CrossRef-style DOI from a reference string."""
    match = re.search(DOI_PATTERN, text)
    if not match:
        return None
    doi = match.group(0).rstrip(DOI_TRAILING_PUNCTUATION)
    return doi or None


def _extract_arxiv_id(text: str) -> str | None:
    """Extract an arXiv id from a DOI or from common bibliography forms."""
    doi = _extract_doi(text)
    if doi and _is_arxiv_doi(doi):
        return _arxiv_id_from_doi(doi)
    match = ARXIV_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _normalize_title(value: str) -> str:
    """Lowercase and strip punctuation for title comparison."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _title_similarity(left: str, right: str) -> float:
    """Return 0–1 similarity between two titles."""
    a = _normalize_title(left)
    b = _normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        return len(shorter) / len(longer)
    return SequenceMatcher(None, a, b).ratio()


def _looks_like_authors(part: str) -> bool:
    """Heuristic: first bibliography clause is usually the author list."""
    lowered = part.lower()
    if "," in part or " and " in lowered or " et al" in lowered:
        return True
    if re.search(r"\b[A-Z]\.", part):
        return True
    tokens = [token for token in part.split() if token[0].isalpha()]
    if 1 <= len(tokens) <= 4 and ":" not in part:
        return all(token[0].isupper() for token in tokens)
    return False


def _heal_pdf_hyphens(text: str) -> str:
    """Join words split by a PDF line-break hyphen: 'im- age' → 'image'."""
    return re.sub(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])", "", text)


def _clean_title(title: str) -> str:
    """Strip venue suffixes that PDFs glue onto the title."""
    title = re.split(
        r"\s+[Ii]n (?:Advances|Proceedings|Proc\.|Annual)\b",
        title,
        maxsplit=1,
    )[0]
    title = re.sub(r",?\s*\(\s*NIPS\s*\)\s*,?\s*\d{4}$", "", title, flags=re.IGNORECASE)
    title = re.sub(r",\s*\d{4}$", "", title)
    return title.strip(" .\"'")


def _guess_title(ref: str) -> str | None:
    """Pull a likely title from an author-year bibliography line."""
    cleaned = _heal_pdf_hyphens(
        re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*", "", ref).strip()
    )
    # Check if title is enclosed in quotes
    quoted_match = re.search(r'["“\'‘]([^"”\'’]{10,250})["”\'’]', cleaned)
    if quoted_match:
        return _clean_title(quoted_match.group(1))

    # Split on period followed by whitespace, ignoring middle initials and common abbreviations
    parts = [
        part.strip()
        for part in re.split(r"(?<!\b[A-Za-z])(?<!\bet al)(?<!\bJr)(?<!\bDr)\.\s+", cleaned)
        if part.strip()
    ]
    if not parts:
        return None

    # Advance past author clauses and year clauses
    start = 0
    while start < len(parts) and (
        _looks_like_authors(parts[start])
        or re.fullmatch(r"\(?\d{4}[a-z]?\)?", parts[start])
    ):
        start += 1

    if start >= len(parts):
        start = 1 if len(parts) > 1 else 0

    for part in parts[start:]:
        if re.fullmatch(r"\(?\d{4}[a-z]?\)?", part):
            continue
        if part.lower().startswith("arxiv") or part.lower().startswith("in proceedings"):
            continue
        if len(part) >= MIN_TITLE_CHARS:
            return _clean_title(part.rstrip("."))

    if len(parts) > 1 and len(parts[1]) >= MIN_TITLE_CHARS:
        return _clean_title(parts[1].rstrip("."))
    return None


def _first_author_last(ref: str) -> str | None:
    """Best-effort last name of the first author."""
    cleaned = _heal_pdf_hyphens(
        re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*", "", ref).strip()
    )
    head = re.split(r"(?<!\b[A-Za-z])(?<!\bet al)(?<!\bJr)(?<!\bDr)\.\s+", cleaned, maxsplit=1)[0]
    first = re.split(r",| and ", head, maxsplit=1)[0].strip()
    first = re.sub(r"\s+et\s+al\.?.*$", "", first, flags=re.IGNORECASE).strip()
    tokens = [t.strip(".,;") for t in first.split() if t.strip(".,;")]
    if not tokens:
        return None
    if len(tokens[-1]) >= 2 and tokens[-1].isalpha():
        return tokens[-1].lower()
    if len(tokens[0]) >= 2 and tokens[0].isalpha():
        return tokens[0].lower()
    return None


PREPRINT_CITED_PATTERN = re.compile(
    r"arxiv|\bcorr\b|preprint|biorxiv|medrxiv|chemrxiv|ssrn|researchsquare", re.IGNORECASE
)
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
YEAR_TOLERANCE = 2
LOW_CONFIDENCE = 0.6


def _clean_text(value: str | None) -> str | None:
    """Unescape HTML entities and collapse whitespace in provider text."""
    if not value:
        return value
    return re.sub(r"\s+", " ", html.unescape(value)).strip() or None


def _cited_as_preprint(raw: str) -> bool:
    """True when the author's own reference line says arXiv / CoRR / preprint."""
    return bool(PREPRINT_CITED_PATTERN.search(raw))


def _tidy(result: CitationResult) -> CitationResult:
    """Clean provider text fields on a finished result."""
    return result.model_copy(
        update={
            "title": _clean_text(result.title),
            "venue": _clean_text(result.venue),
            "authors": [_clean_text(name) or name for name in result.authors],
        }
    )


def _apply_consistency(ref: str, result: CitationResult) -> CitationResult:
    """Cross-check a title-matched result against the reference's year and author.

    Title similarity alone can match a different edition or a look-alike paper.
    A year or first-author conflict lowers confidence; both conflicting means
    the match is not trustworthy, so the reference stays unverified.
    """
    if result.status != CitationStatus.verified:
        return result
    notes: list[str] = []

    ref_years = [int(y) for y in YEAR_PATTERN.findall(ref)]
    year_conflict = bool(
        ref_years
        and result.year
        and min(abs(result.year - y) for y in ref_years) > YEAR_TOLERANCE
    )
    if year_conflict:
        notes.append(f"matched record year {result.year} differs from cited year {ref_years[-1]}")

    last = _first_author_last(ref)
    author_conflict = bool(
        last
        and result.authors
        and not any(last in name.lower() for name in result.authors)
    )
    if author_conflict:
        notes.append(f"first author '{last}' not found in matched authors")

    if year_conflict and author_conflict:
        return _unverified(
            ref,
            doi=None,
            diagnostic="Closest match rejected: " + "; ".join(notes) + ".",
            action="Check the author names and year, or add a DOI.",
        )
    if notes:
        return result.model_copy(
            update={
                "confidence_score": min(result.confidence_score, LOW_CONFIDENCE),
                "diagnostic_reason": f"{result.diagnostic_reason} Check: " + "; ".join(notes) + ".",
                "suggested_action": "Confirm this is the edition you meant to cite.",
            }
        )
    return result


def _unverified(
    ref: str,
    doi: str | None = None,
    diagnostic: str | None = None,
    action: str | None = None,
) -> CitationResult:
    """Return an unverified result with diagnostic guidance."""
    diag = diagnostic or (
        f"Provided DOI '{doi}' did not resolve on CrossRef or arXiv."
        if doi
        else "No resolving DOI or arXiv ID found. Title and author bibliographic search returned low confidence."
    )
    act = action or (
        "Check DOI syntax or replace with archival published DOI."
        if doi
        else "Provide official DOI or complete volume/page metadata to verify."
    )
    return CitationResult(
        raw=ref,
        doi=doi,
        status=CitationStatus.unverified,
        title=None,
        authors=[],
        year=None,
        venue=None,
        publication_type=PublicationType.unknown.value,
        is_preprint=False,
        source_url=f"https://doi.org/{doi}" if doi else None,
        confidence_score=0.0,
        diagnostic_reason=diag,
        suggested_action=act,
    )


def _authors_from_crossref(message: dict[str, Any]) -> list[str]:
    """Extract clean author names from CrossRef message."""
    authors_data = message.get("author") or []
    names: list[str] = []
    if isinstance(authors_data, list):
        for a in authors_data[:6]:
            if isinstance(a, dict):
                given = str(a.get("given", "")).strip()
                family = str(a.get("family", "")).strip()
                if given and family:
                    names.append(f"{given} {family}")
                elif family:
                    names.append(family)
                elif a.get("name"):
                    names.append(str(a.get("name")).strip())
    return names


def _venue_from_crossref(message: dict[str, Any]) -> str | None:
    """Extract container title or publisher from CrossRef work."""
    container = message.get("container-title")
    if isinstance(container, list) and container and isinstance(container[0], str):
        return container[0].strip() or None
    if isinstance(container, str) and container.strip():
        return container.strip()
    publisher = message.get("publisher")
    if isinstance(publisher, str) and publisher.strip():
        return publisher.strip()
    return None


def _is_crossref_preprint(doi: str | None, message: dict[str, Any]) -> bool:
    """Determine whether a CrossRef item is a preprint."""
    if doi and _is_arxiv_doi(doi):
        return True
    work_type = str(message.get("type", "")).lower()
    if work_type == "posted-content":
        return True
    venue = (_venue_from_crossref(message) or "").lower()
    if any(p in venue for p in ("arxiv", "biorxiv", "medrxiv", "chemrxiv", "preprints")):
        return True
    return False


def _year_from_crossref(message: dict[str, Any]) -> int | None:
    """Read the publication year from a CrossRef work message."""
    published = message.get("published")
    if not isinstance(published, dict):
        return None
    date_parts = published.get("date-parts")
    if (
        isinstance(date_parts, list)
        and date_parts
        and isinstance(date_parts[0], list)
        and date_parts[0]
    ):
        try:
            return int(date_parts[0][0])
        except (TypeError, ValueError):
            return None
    return None


def _title_from_crossref(message: dict[str, Any]) -> str | None:
    """Read the first title from a CrossRef work message."""
    titles = message.get("title")
    if isinstance(titles, list) and titles:
        first = titles[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _result_from_crossref(
    ref: str,
    message: dict[str, Any],
    confidence: float = 1.0,
) -> CitationResult:
    """Build an enriched verified result from a CrossRef work payload."""
    doi = message.get("DOI")
    doi_str = str(doi) if isinstance(doi, str) and doi else None
    venue = _venue_from_crossref(message)
    is_preprint = _is_crossref_preprint(doi_str, message)
    work_type = str(message.get("type", "")).lower()

    if is_preprint:
        pub_type = PublicationType.preprint.value
    elif "proceeding" in work_type or "conference" in work_type:
        pub_type = PublicationType.conference.value
    elif "book" in work_type:
        pub_type = PublicationType.book.value
    elif "journal" in work_type or "article" in work_type:
        pub_type = PublicationType.journal.value
    else:
        pub_type = PublicationType.unknown.value

    source_url = f"https://doi.org/{doi_str}" if doi_str else None

    return CitationResult(
        raw=ref,
        doi=doi_str,
        status=CitationStatus.verified,
        title=_title_from_crossref(message),
        authors=_authors_from_crossref(message),
        year=_year_from_crossref(message),
        venue=venue,
        publication_type=pub_type,
        is_preprint=is_preprint,
        source_url=source_url,
        confidence_score=round(confidence, 2),
        diagnostic_reason=f"Verified via CrossRef official record (DOI: {doi_str or 'matched'}).",
    )


async def _get_crossref(
    client: httpx.AsyncClient,
    gate: asyncio.Semaphore,
    url: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call CrossRef slowly. They 429 if we burst 40 bibliographic searches."""
    async with gate:
        response: httpx.Response | None = None
        for attempt in range(4):
            response = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code != 429:
                await asyncio.sleep(CROSSREF_GAP_SECONDS)
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
            except ValueError:
                wait = 1.5 * (attempt + 1)
            logger.info("CrossRef rate-limited; waiting %.1fs", wait)
            await asyncio.sleep(wait)
        return response


def _year_from_arxiv_id(arxiv_id: str) -> int | None:
    """Derive the publication year from an arXiv identifier."""
    # New format: YYMM.NNNN[vN] (e.g., 1607.06450 -> 2016)
    match_new = re.match(r"^(\d{2})(\d{2})\.\d{4,5}", arxiv_id)
    if match_new:
        yy = int(match_new.group(1))
        return 1900 + yy if yy >= 91 else 2000 + yy
    # Old format: math/0211111, hep-th/9901001
    match_old = re.search(r"/(\d{2})(\d{2})\d{3}", arxiv_id)
    if match_old:
        yy = int(match_old.group(1))
        return 1900 + yy if yy >= 91 else 2000 + yy
    return None


async def _with_arxiv_title(
    client: httpx.AsyncClient,
    result: CitationResult,
    ref: str,
    arxiv_id: str | None = None,
) -> CitationResult:
    """Fill missing metadata from the arXiv abs page when an ID is known."""
    found_id = arxiv_id or _extract_arxiv_id(ref)
    if result.doi and not found_id and _is_arxiv_doi(result.doi):
        found_id = _arxiv_id_from_doi(result.doi)
    if not found_id:
        return result
    arxiv = await _check_arxiv_id(client, found_id, ref)
    updates: dict[str, Any] = {}
    if arxiv.title and not result.title:
        updates["title"] = arxiv.title
    if arxiv.year and not result.year:
        updates["year"] = arxiv.year
    if arxiv.authors and not result.authors:
        updates["authors"] = arxiv.authors
    if not result.venue:
        updates["venue"] = "arXiv"
    if updates:
        return result.model_copy(update=updates)
    return result


async def _check_arxiv_id(
    client: httpx.AsyncClient,
    arxiv_id: str,
    ref: str,
) -> CitationResult:
    """Confirm an arXiv id against the official abstract landing page."""
    try:
        response = await client.get(f"{ARXIV_ABS_URL}{arxiv_id}", timeout=REQUEST_TIMEOUT)
        clean_id = arxiv_id.split("v")[0]
        if response.status_code == 200:
            title: str | None = None
            meta_title = re.search(
                r'<meta\s+name="citation_title"\s+content="([^"]+)"',
                response.text,
                re.IGNORECASE,
            )
            if meta_title:
                title = re.sub(r"\s+", " ", meta_title.group(1)).strip() or None
            else:
                title_match = re.search(
                    r"<title>(.*?)</title>",
                    response.text,
                    re.IGNORECASE | re.DOTALL,
                )
                if title_match:
                    title = re.sub(
                        r"^(\[[^\]]+\]|arxiv\.org)\s*[-:]?\s*",
                        "",
                        re.sub(r"\s+", " ", title_match.group(1)),
                        flags=re.IGNORECASE,
                    ).strip() or None

            author_tags = re.findall(
                r'<meta\s+name="citation_author"\s+content="([^"]+)"',
                response.text,
                re.IGNORECASE,
            )
            authors = [re.sub(r"\s+", " ", a).strip() for a in author_tags if a.strip()][:6]

            year = _year_from_arxiv_id(clean_id)
            meta_date = re.search(
                r'<meta\s+name="citation_date"\s+content="(\d{4})',
                response.text,
                re.IGNORECASE,
            )
            if meta_date:
                try:
                    year = int(meta_date.group(1))
                except ValueError:
                    pass

            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=clean_id),
                status=CitationStatus.verified,
                title=title,
                authors=authors,
                year=year,
                venue="arXiv",
                publication_type=PublicationType.preprint.value,
                is_preprint=True,
                source_url=f"https://arxiv.org/abs/{arxiv_id}",
                confidence_score=1.0,
                diagnostic_reason=f"Verified via arXiv official repository (arXiv:{clean_id}).",
                suggested_action=None,
            )
        if response.status_code == 404:
            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=clean_id),
                status=CitationStatus.ghost,
                title=None,
                authors=[],
                year=None,
                venue="arXiv",
                publication_type=PublicationType.preprint.value,
                is_preprint=True,
                source_url=f"https://arxiv.org/abs/{arxiv_id}",
                confidence_score=0.0,
                diagnostic_reason=f"arXiv ID '{arxiv_id}' was not found on arXiv (HTTP 404). Dead identifier or hallucinated preprint.",
                suggested_action="Verify arXiv ID digits or locate published peer-reviewed conference/journal version.",
            )
        logger.debug("arXiv returned %s for %s", response.status_code, arxiv_id)
        return _unverified(
            ref,
            ARXIV_DOI_TEMPLATE.format(arxiv_id=clean_id),
            diagnostic=f"arXiv server returned status {response.status_code} for ID '{arxiv_id}'.",
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("arXiv lookup failed for %s: %s", arxiv_id, exc)
        return _unverified(ref, diagnostic=f"Connection failure contacting arXiv: {exc}")


async def _check_datacite(
    client: httpx.AsyncClient,
    doi: str,
    ref: str,
) -> tuple[int | None, CitationResult | None]:
    """Look a DOI up in DataCite (Zenodo, figshare, many repositories).

    Returns (status_code, result). result is None unless the DOI exists.
    """
    try:
        response = await client.get(
            f"{DATACITE_DOI_URL}/{quote(doi, safe='/')}", timeout=REQUEST_TIMEOUT
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("DataCite lookup failed for %s: %s", doi, exc)
        return None, None
    if response.status_code != 200:
        return response.status_code, None
    try:
        attrs = response.json()["data"]["attributes"]
    except (ValueError, KeyError, TypeError):
        return response.status_code, None
    titles = attrs.get("titles") or []
    title = titles[0].get("title") if titles and isinstance(titles[0], dict) else None
    creators = [
        str(c.get("name")).strip()
        for c in (attrs.get("creators") or [])[:6]
        if isinstance(c, dict) and c.get("name")
    ]
    general = str((attrs.get("types") or {}).get("resourceTypeGeneral") or "").lower()
    is_preprint = general == "preprint"
    year = attrs.get("publicationYear")
    return 200, CitationResult(
        raw=ref,
        doi=doi,
        status=CitationStatus.verified,
        title=title,
        authors=creators,
        year=int(year) if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else None,
        venue=attrs.get("publisher") if isinstance(attrs.get("publisher"), str) else None,
        publication_type=PublicationType.preprint.value if is_preprint else PublicationType.unknown.value,
        is_preprint=is_preprint,
        source_url=f"https://doi.org/{doi}",
        confidence_score=1.0,
        diagnostic_reason=f"Verified via DataCite official record (DOI: {doi}).",
    )


async def _check_doi(
    client: httpx.AsyncClient,
    gate: asyncio.Semaphore,
    doi: str,
    ref: str,
) -> CitationResult:
    """Resolve a DOI via CrossRef, with an arXiv fallback for preprint DOIs."""
    try:
        url = f"{CROSSREF_BASE_URL}/{quote(doi, safe='')}"
        response = await _get_crossref(client, gate, url)
        if response.status_code == 200:
            data: Any = response.json()
            message: Any = data.get("message", {}) if isinstance(data, dict) else {}
            if isinstance(message, dict):
                return _result_from_crossref(ref, message)
            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.verified,
                title=None,
                authors=[],
                year=None,
                venue=None,
                publication_type=PublicationType.unknown.value,
                is_preprint=False,
                source_url=f"https://doi.org/{doi}",
                confidence_score=1.0,
                diagnostic_reason=f"Verified via CrossRef official record (DOI: {doi}).",
            )
        if response.status_code == 404:
            if _is_arxiv_doi(doi):
                arxiv_id = _arxiv_id_from_doi(doi)
                if arxiv_id:
                    return await _check_arxiv_id(client, arxiv_id, ref)
            datacite_status, datacite_result = await _check_datacite(client, doi, ref)
            if datacite_result is not None:
                return datacite_result
            if datacite_status != 404:
                return _unverified(
                    ref,
                    doi,
                    diagnostic=(
                        f"DOI '{doi}' is not in CrossRef and DataCite could not be reached "
                        f"(status {datacite_status}); cannot confirm it is fabricated."
                    ),
                )
            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.ghost,
                title=None,
                authors=[],
                year=None,
                venue=None,
                publication_type=PublicationType.unknown.value,
                is_preprint=False,
                source_url=f"https://doi.org/{doi}",
                confidence_score=0.0,
                diagnostic_reason=f"DOI '{doi}' does not exist in CrossRef or DataCite (HTTP 404). Dead DOI or fabricated citation.",
                suggested_action="Verify DOI syntax or replace with valid DOI from publisher landing page.",
            )
        logger.warning("CrossRef returned %s for DOI %s", response.status_code, doi)
        return _unverified(
            ref,
            doi,
            diagnostic=f"CrossRef returned status {response.status_code} for DOI '{doi}'.",
        )
    except httpx.TimeoutException:
        logger.debug("CrossRef timeout for DOI %s", doi)
        return _unverified(ref, doi, diagnostic=f"CrossRef request timed out resolving DOI '{doi}'.")
    except httpx.RequestError as exc:
        logger.warning("CrossRef request error for %s: %s", doi, exc)
        return _unverified(ref, doi, diagnostic=f"Network error contacting CrossRef for DOI '{doi}': {exc}")
    except Exception:
        logger.error("Unexpected error checking DOI %s", doi, exc_info=True)
        return _unverified(ref, doi, diagnostic=f"Unexpected error resolving DOI '{doi}'.")


def _author_in_item(ref: str, item: dict[str, Any]) -> bool:
    """True if the guessed first-author last name appears in the CrossRef authors."""
    last = _first_author_last(ref)
    authors = item.get("author")
    if not last or not isinstance(authors, list):
        return False
    for author in authors[:4]:
        if isinstance(author, dict) and str(author.get("family") or "").lower() == last:
            return True
    return False


def _crossref_match_quality(ref: str, item: dict[str, Any]) -> bool:
    """Accept a bibliographic hit only if title or author evidence is strong."""
    result_title = _title_from_crossref(item) or ""
    guessed = _guess_title(ref) or ""
    similarity = 0.0
    if guessed and result_title:
        similarity = _title_similarity(guessed, result_title)
    elif result_title:
        similarity = _title_similarity(result_title, ref)
    if similarity >= 0.92:
        return True

    if guessed and result_title and _author_in_item(ref, item):
        a = _normalize_title(guessed)
        b = _normalize_title(result_title)
        if a and b and min(len(a), len(b)) >= 18 and (b.startswith(a) or a.startswith(b)):
            return True

    score = item.get("score")
    try:
        numeric_score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        numeric_score = 0.0
    if numeric_score < CROSSREF_MIN_SCORE:
        return False
    if similarity >= TITLE_SIMILARITY_MIN and _author_in_item(ref, item):
        return True
    return False


async def _search_crossref(
    client: httpx.AsyncClient,
    gate: asyncio.Semaphore,
    ref: str,
) -> CitationResult | None:
    """Search CrossRef with the bibliography line or a guessed title."""
    query = _guess_title(ref) or ref
    author = _first_author_last(ref)
    if author and query != ref:
        bibliographic = f"{query} {author}"
    else:
        bibliographic = ref[:400]
    try:
        response = await _get_crossref(
            client,
            gate,
            CROSSREF_BASE_URL,
            params={
                "query.bibliographic": bibliographic,
                "rows": 3,
                "select": "DOI,title,published,author,score,type,container-title,publisher",
            },
        )
        if response.status_code != 200:
            logger.debug("CrossRef search returned %s", response.status_code)
            return None
        data: Any = response.json()
        message = data.get("message", {}) if isinstance(data, dict) else {}
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list):
            return None
        ranked: list[tuple[float, dict[str, Any]]] = []
        guessed = _guess_title(ref) or ""
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _title_from_crossref(item) or ""
            similarity = _title_similarity(guessed, title) if guessed and title else 0.0
            ranked.append((similarity, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        for _, item in ranked:
            if _crossref_match_quality(ref, item):
                return _result_from_crossref(ref, item)
        return None
    except (httpx.TimeoutException, httpx.RequestError, ValueError) as exc:
        logger.warning("CrossRef search failed: %s", exc)
        return None


async def _search_arxiv(client: httpx.AsyncClient, ref: str) -> CitationResult | None:
    """Search the arXiv API by title and first author."""
    title = _guess_title(ref)
    if not title:
        return None
    author = _first_author_last(ref)
    query = f'all:"{title}"'
    if author:
        query = f"{query} AND au:{author}"
    try:
        response = await client.get(
            ARXIV_SEARCH_URL,
            params={"search_query": query, "start": 0, "max_results": 3},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        entries = re.findall(
            r"<entry>(.*?)</entry>",
            response.text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        for entry in entries:
            title_match = re.search(
                r"<title>(.*?)</title>",
                entry,
                flags=re.DOTALL | re.IGNORECASE,
            )
            id_match = re.search(
                r"arxiv.org/abs/([0-9]+\.[0-9]+(?:v\d+)?|[a-z\-]+/[0-9]+)",
                entry,
                flags=re.IGNORECASE,
            )
            if not title_match or not id_match:
                continue
            found_title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            a = _normalize_title(title)
            b = _normalize_title(found_title)
            similar = _title_similarity(title, found_title) >= TITLE_SIMILARITY_MIN
            prefix = bool(
                a and b and min(len(a), len(b)) >= 18 and (b.startswith(a) or a.startswith(b))
            )
            if not similar and not prefix:
                continue

            raw_id = id_match.group(1)
            clean_id = raw_id.split("v")[0]
            year = _year_from_arxiv_id(clean_id)

            author_matches = re.findall(
                r"<name>(.*?)</name>",
                entry,
                flags=re.DOTALL | re.IGNORECASE,
            )
            authors = [re.sub(r"\s+", " ", a).strip() for a in author_matches if a.strip()][:6]

            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=clean_id),
                status=CitationStatus.verified,
                title=found_title,
                authors=authors,
                year=year,
                venue="arXiv",
                publication_type=PublicationType.preprint.value,
                is_preprint=True,
                source_url=f"https://arxiv.org/abs/{clean_id}",
                confidence_score=0.92,
                diagnostic_reason=f"Verified via arXiv title/author bibliographic query (arXiv:{clean_id}).",
                suggested_action=None,
            )
        return None
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("arXiv search failed: %s", exc)
        return None


async def _search_openalex(
    client: httpx.AsyncClient,
    ref: str,
) -> CitationResult | None:
    """Search OpenAlex works API by title as secondary fallback for non-DOI entries."""
    query = _guess_title(ref) or ref
    first_author = _first_author_last(ref)
    try:
        response = await client.get(
            OPENALEX_WORKS_URL,
            params={"search": query[:250], "per-page": 3},
            headers={"User-Agent": OPENALEX_USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            logger.debug("OpenAlex search returned %s", response.status_code)
            return None
        data: Any = response.json()
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None

        guessed = _guess_title(ref) or query
        for work in results:
            if not isinstance(work, dict):
                continue
            work_title = str(work.get("title") or work.get("display_name") or "")
            if not work_title:
                continue

            similarity = _title_similarity(guessed, work_title)

            authorships = work.get("authorships") or []
            author_names: list[str] = []
            author_match = False
            if isinstance(authorships, list):
                for auth in authorships[:6]:
                    if isinstance(auth, dict):
                        author_obj = auth.get("author") or {}
                        name = author_obj.get("display_name")
                        if name:
                            author_names.append(str(name).strip())
                            if first_author and first_author in str(name).lower():
                                author_match = True

            if similarity >= 0.90 or (similarity >= TITLE_SIMILARITY_MIN and author_match):
                doi_raw = work.get("doi")
                doi_str = None
                if isinstance(doi_raw, str) and doi_raw.strip():
                    doi_str = doi_raw.replace("https://doi.org/", "").strip() or None

                primary_loc = work.get("primary_location") or {}
                source_obj = primary_loc.get("source") or {} if isinstance(primary_loc, dict) else {}
                venue_name = source_obj.get("display_name") if isinstance(source_obj, dict) else None
                if not venue_name and isinstance(work.get("host_venue"), dict):
                    venue_name = work["host_venue"].get("display_name")

                work_type_raw = str(work.get("type", "")).lower()
                source_type_raw = (
                    str(source_obj.get("type", "")).lower()
                    if isinstance(source_obj, dict)
                    else ""
                )
                is_preprint = (
                    work_type_raw == "preprint"
                    or "arxiv" in (venue_name or "").lower()
                    or (doi_str is not None and _is_arxiv_doi(doi_str))
                )

                if is_preprint:
                    pub_type = PublicationType.preprint.value
                elif "journal" in source_type_raw or "article" in work_type_raw:
                    pub_type = PublicationType.journal.value
                elif "conference" in source_type_raw or "proceedings" in work_type_raw:
                    pub_type = PublicationType.conference.value
                elif "book" in source_type_raw or "book" in work_type_raw:
                    pub_type = PublicationType.book.value
                else:
                    pub_type = PublicationType.unknown.value

                landing_url = (
                    doi_raw
                    or (primary_loc.get("landing_page_url") if isinstance(primary_loc, dict) else None)
                    or work.get("id")
                )

                return CitationResult(
                    raw=ref,
                    doi=doi_str,
                    status=CitationStatus.verified,
                    title=work_title,
                    authors=author_names,
                    year=work.get("publication_year"),
                    venue=venue_name,
                    publication_type=pub_type,
                    is_preprint=is_preprint,
                    source_url=landing_url,
                    confidence_score=0.95,
                    diagnostic_reason=f"Verified via OpenAlex catalog ({venue_name or 'academic index'}).",
                    suggested_action=None,
                )
        return None
    except (httpx.TimeoutException, httpx.RequestError, ValueError) as exc:
        logger.warning("OpenAlex search failed: %s", exc)
        return None


async def _check_one_raw(
    client: httpx.AsyncClient,
    ref: str,
    work_gate: asyncio.Semaphore,
    crossref_gate: asyncio.Semaphore,
) -> CitationResult:
    """Verify one reference: DOI, arXiv id, CrossRef search, arXiv search, OpenAlex search.

    Any lookup failure becomes unverified. Never raises to the caller.
    """
    try:
        async with work_gate:
            doi = _extract_doi(ref)
            if doi and _is_arxiv_doi(doi):
                arxiv_id = _arxiv_id_from_doi(doi)
                if arxiv_id:
                    return await _check_arxiv_id(client, arxiv_id, ref)
            if doi:
                result = await _check_doi(client, crossref_gate, doi, ref)
                if result.status == CitationStatus.verified:
                    if not result.title:
                        return await _with_arxiv_title(client, result, ref)
                    return result
                if result.status == CitationStatus.ghost:
                    return result

            arxiv_id = _extract_arxiv_id(ref)
            if arxiv_id:
                return await _check_arxiv_id(client, arxiv_id, ref)

            found = await _search_crossref(client, crossref_gate, ref)
            if found is not None:
                return found

            found = await _search_arxiv(client, ref)
            if found is not None:
                return found

            found = await _search_openalex(client, ref)
            if found is not None:
                return found

            return _unverified(ref, doi=doi)
    except Exception:
        logger.warning("Citation lookup failed; marking unverified", exc_info=True)
        return _unverified(ref)


async def _check_one(
    client: httpx.AsyncClient,
    ref: str,
    work_gate: asyncio.Semaphore,
    crossref_gate: asyncio.Semaphore,
) -> CitationResult:
    """Verify one reference, then tidy text and cross-check title-only matches."""
    result = await _check_one_raw(client, ref, work_gate, crossref_gate)
    if _extract_doi(ref) is None and _extract_arxiv_id(ref) is None:
        result = _apply_consistency(ref, result)
    return _tidy(result)


async def verify_citations(references: list[str]) -> list[CitationResult]:
    """Verify reference strings against CrossRef, arXiv, and OpenAlex.

    DOI lines still go to CrossRef first. Lines without a DOI try an
    arXiv id, CrossRef search, arXiv search, then OpenAlex search. Caps at MAX_REFERENCES.

    Args:
        references: Raw bibliography strings from the parser.

    Returns:
        One CitationResult per input reference, in the same order.
    """
    refs = references[:MAX_REFERENCES]
    if not refs:
        return []
    logger.info("Verifying %d citations against CrossRef, arXiv, and OpenAlex", len(refs))
    work_gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)
    crossref_gate = asyncio.Semaphore(CROSSREF_CONCURRENCY)

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": CROSSREF_USER_AGENT},
        ) as client:
            raw_results = await asyncio.gather(
                *(_check_one(client, ref, work_gate, crossref_gate) for ref in refs),
                return_exceptions=True,
            )
    except Exception:
        logger.warning("Citation batch failed; marking all unverified", exc_info=True)
        return [_unverified(ref) for ref in refs]

    results: list[CitationResult] = []
    for ref, item in zip(refs, raw_results, strict=True):
        if isinstance(item, CitationResult):
            results.append(item)
        else:
            logger.warning("Citation lookup failed; marking unverified: %s", item)
            results.append(_unverified(ref))

    verified = sum(1 for item in results if item.status == CitationStatus.verified)
    ghost = sum(1 for item in results if item.status == CitationStatus.ghost)
    unverified = sum(1 for item in results if item.status == CitationStatus.unverified)
    logger.info(
        "Citation check complete: %d verified, %d ghost, %d unverified",
        verified,
        ghost,
        unverified,
    )
    return results


def build_citation_summary(results: list[CitationResult]) -> CitationSummary:
    """Build an enriched CitationSummary with bibliometric telemetry and editorial warnings."""
    verified = sum(1 for item in results if item.status == CitationStatus.verified)
    ghost = sum(1 for item in results if item.status == CitationStatus.ghost)
    unverified = sum(1 for item in results if item.status == CitationStatus.unverified)
    total = len(results)

    # A matched arXiv record is not a preprint citation if the author cited the
    # published venue (e.g. "In ICLR, 2015"), so judge by the reference as written.
    preprint_count = sum(
        1
        for item in results
        if item.status == CitationStatus.verified and item.is_preprint and _cited_as_preprint(item.raw)
    )
    peer_reviewed_count = verified - preprint_count
    preprint_ratio_pct = round((preprint_count / max(1, verified)) * 100, 1) if verified > 0 else 0.0

    # Age is measured from the newest cited year, a proxy for the manuscript's
    # own date, so a 2017 paper is not judged against today's calendar.
    all_years = [
        item.year
        for item in results
        if item.year and 1900 <= item.year <= date.today().year + 1
    ]
    median_age: float | None = None
    if all_years:
        reference_year = max(all_years)
        ages = [reference_year - y for y in all_years]
        median_age = round(float(statistics.median(ages)), 1)

    warnings: list[str] = []
    if ghost > 0:
        warnings.append(
            f"CRITICAL: {ghost} phantom or hallucinated reference(s) detected with dead DOIs/IDs. "
            "This is a primary desk-reject trigger for scientific misconduct or generative AI hallucination."
        )
    if preprint_ratio_pct > 50.0:
        warnings.append(
            f"PREPRINT SHARE: {preprint_ratio_pct}% of verified references ({preprint_count}/{verified}) are preprints. "
            "This is common in fast-moving fields such as machine learning; check your target journal's norms."
        )
    if median_age is not None and median_age > 10.0:
        warnings.append(
            f"REFERENCE AGE: median reference is {median_age} years older than the newest one cited. "
            "Consider whether recent work in your area is represented."
        )
    if unverified > 0:
        warnings.append(
            f"UNVERIFIED REFERENCES: {unverified} reference(s) could not be resolved against CrossRef, arXiv, or OpenAlex. "
            "Ensure titles and volume metadata are accurate."
        )

    return CitationSummary(
        verified=verified,
        ghost=ghost,
        unverified=unverified,
        total=total,
        preprint_count=preprint_count,
        peer_reviewed_count=peer_reviewed_count,
        preprint_ratio_pct=preprint_ratio_pct,
        median_citation_age_years=median_age,
        editorial_warnings=warnings,
        items=results,
    )

