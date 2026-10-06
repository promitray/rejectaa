"""Citation Autopsy: DOI, arXiv id, and title/author verification."""

from __future__ import annotations

import asyncio
import logging
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import httpx

from models import CitationResult, CitationStatus, CitationSummary

logger = logging.getLogger(__name__)

CROSSREF_BASE_URL = "https://api.crossref.org/works"
CROSSREF_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 8.0
MAX_REFERENCES = 60
LOOKUP_CONCURRENCY = 5

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


def _guess_title(ref: str) -> str | None:
    """Pull a likely title from an author-year bibliography line."""
    cleaned = re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*", "", ref).strip()
    parts = [part.strip() for part in re.split(r"\.\s+", cleaned) if part.strip()]
    if not parts:
        return None
    start = 1 if len(parts) > 1 and _looks_like_authors(parts[0]) else 0
    for part in parts[start:]:
        if re.fullmatch(r"\(?\d{4}[a-z]?\)?", part):
            continue
        if part.lower().startswith("arxiv"):
            continue
        if len(part) >= MIN_TITLE_CHARS:
            return part.rstrip(".")
    return None


def _first_author_last(ref: str) -> str | None:
    """Best-effort last name of the first author."""
    head = re.split(r"\.\s+", ref, maxsplit=1)[0]
    head = re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*", "", head)
    first = re.split(r",| and ", head, maxsplit=1)[0].strip()
    first = re.sub(r"\s+et\s+al\.?.*$", "", first, flags=re.IGNORECASE).strip()
    tokens = first.split()
    if not tokens:
        return None
    last = tokens[-1].strip(".,;")
    if last.isalpha() and len(last) >= 2:
        return last.lower()
    return None


def _unverified(ref: str, doi: str | None = None) -> CitationResult:
    """Return an unverified result for a reference we could not resolve."""
    return CitationResult(
        raw=ref,
        doi=doi,
        status=CitationStatus.unverified,
        title=None,
        year=None,
    )


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


def _result_from_crossref(ref: str, message: dict[str, Any]) -> CitationResult:
    """Build a verified result from a CrossRef work payload."""
    doi = message.get("DOI")
    return CitationResult(
        raw=ref,
        doi=str(doi) if isinstance(doi, str) and doi else None,
        status=CitationStatus.verified,
        title=_title_from_crossref(message),
        year=_year_from_crossref(message),
    )


async def _get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """GET with a short retry when CrossRef rate-limits."""
    response: httpx.Response | None = None
    for attempt in range(3):
        response = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 429:
            return response
        await asyncio.sleep(0.6 * (attempt + 1))
    return response


async def _with_arxiv_title(
    client: httpx.AsyncClient,
    result: CitationResult,
    ref: str,
    arxiv_id: str | None = None,
) -> CitationResult:
    """Fill a missing title from the arXiv abs page when we already have an id."""
    found_id = arxiv_id or _extract_arxiv_id(ref)
    if result.doi and not found_id and _is_arxiv_doi(result.doi):
        found_id = _arxiv_id_from_doi(result.doi)
    if not found_id:
        return result
    arxiv = await _check_arxiv_id(client, found_id, ref)
    if arxiv.title:
        return result.model_copy(update={"title": arxiv.title})
    return result


async def _check_arxiv_id(
    client: httpx.AsyncClient,
    arxiv_id: str,
    ref: str,
) -> CitationResult:
    """Confirm an arXiv id against the abs page."""
    try:
        response = await client.get(f"{ARXIV_ABS_URL}{arxiv_id}", timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            title: str | None = None
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
                ).strip()
                title = title or None
            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id.split("v")[0]),
                status=CitationStatus.verified,
                title=title,
                year=None,
            )
        if response.status_code == 404:
            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id),
                status=CitationStatus.ghost,
                title=None,
                year=None,
            )
        logger.debug("arXiv returned %s for %s", response.status_code, arxiv_id)
        return _unverified(ref, ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id))
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("arXiv lookup failed for %s: %s", arxiv_id, exc)
        return _unverified(ref)


async def _check_doi(client: httpx.AsyncClient, doi: str, ref: str) -> CitationResult:
    """Resolve a DOI via CrossRef, with an arXiv fallback for preprint DOIs."""
    try:
        url = f"{CROSSREF_BASE_URL}/{quote(doi, safe='')}"
        response = await _get(client, url)
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
                year=None,
            )
        if response.status_code == 404:
            if _is_arxiv_doi(doi):
                arxiv_id = _arxiv_id_from_doi(doi)
                if arxiv_id:
                    return await _check_arxiv_id(client, arxiv_id, ref)
            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.ghost,
                title=None,
                year=None,
            )
        logger.warning("CrossRef returned %s for DOI %s", response.status_code, doi)
        return _unverified(ref, doi)
    except httpx.TimeoutException:
        logger.debug("CrossRef timeout for DOI %s", doi)
        return _unverified(ref, doi)
    except httpx.RequestError as exc:
        logger.warning("CrossRef request error for %s: %s", doi, exc)
        return _unverified(ref, doi)
    except Exception:
        logger.error("Unexpected error checking DOI %s", doi, exc_info=True)
        return _unverified(ref, doi)


def _crossref_match_quality(ref: str, item: dict[str, Any]) -> bool:
    """Accept a bibliographic hit only if title or author evidence is strong."""
    result_title = _title_from_crossref(item) or ""
    guessed = _guess_title(ref) or ""
    similarity = 0.0
    if guessed and result_title:
        similarity = _title_similarity(guessed, result_title)
    elif result_title:
        similarity = _title_similarity(result_title, ref)
    if similarity >= 0.88:
        return True

    score = item.get("score")
    try:
        numeric_score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        numeric_score = 0.0
    if numeric_score < CROSSREF_MIN_SCORE:
        return False
    if similarity >= TITLE_SIMILARITY_MIN:
        return True

    last = _first_author_last(ref)
    authors = item.get("author")
    if last and isinstance(authors, list):
        for author in authors[:2]:
            if isinstance(author, dict):
                family = str(author.get("family") or "").lower()
                if family and family == last:
                    return bool(result_title) and numeric_score >= 60
    return False


async def _search_crossref(client: httpx.AsyncClient, ref: str) -> CitationResult | None:
    """Search CrossRef with the bibliography line or a guessed title."""
    query = _guess_title(ref) or ref
    author = _first_author_last(ref)
    if author and query != ref:
        bibliographic = f"{query} {author}"
    else:
        bibliographic = ref[:400]
    try:
        response = await _get(
            client,
            CROSSREF_BASE_URL,
            params={
                "query.bibliographic": bibliographic,
                "rows": 3,
                "select": "DOI,title,published,author,score",
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
    """Search the arXiv API by title and first author when the line looks like a preprint."""
    if "arxiv" not in ref.lower():
        return None
    title = _guess_title(ref)
    if not title:
        return None
    author = _first_author_last(ref)
    query = f'ti:"{title}"'
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
            if _title_similarity(title, found_title) < TITLE_SIMILARITY_MIN:
                continue
            return CitationResult(
                raw=ref,
                doi=ARXIV_DOI_TEMPLATE.format(arxiv_id=id_match.group(1).split("v")[0]),
                status=CitationStatus.verified,
                title=found_title,
                year=None,
            )
        return None
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("arXiv search failed: %s", exc)
        return None


async def _check_one(client: httpx.AsyncClient, ref: str, gate: asyncio.Semaphore) -> CitationResult:
    """Verify one reference: DOI, then arXiv id, then title/author search."""
    async with gate:
        doi = _extract_doi(ref)
        if doi:
            result = await _check_doi(client, doi, ref)
            if result.status == CitationStatus.verified and not result.title:
                return await _with_arxiv_title(client, result, ref)
            return result

        arxiv_id = _extract_arxiv_id(ref)
        if arxiv_id:
            constructed = ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id.split("v")[0])
            doi_result = await _check_doi(client, constructed, ref)
            if doi_result.status == CitationStatus.verified:
                if not doi_result.title:
                    return await _with_arxiv_title(client, doi_result, ref, arxiv_id)
                return doi_result
            return await _check_arxiv_id(client, arxiv_id, ref)

        found = await _search_crossref(client, ref)
        if found is not None:
            return found

        found = await _search_arxiv(client, ref)
        if found is not None:
            return found

        return _unverified(ref)


async def verify_citations(references: list[str]) -> list[CitationResult]:
    """Verify reference strings against CrossRef and arXiv.

    DOI lines still go to CrossRef first. Lines without a DOI try an
    arXiv id, then a title/author search. Caps at MAX_REFERENCES.

    Args:
        references: Raw bibliography strings from the parser.

    Returns:
        One CitationResult per input reference, in the same order.
    """
    refs = references[:MAX_REFERENCES]
    logger.info("Verifying %d citations against CrossRef and arXiv", len(refs))
    gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": CROSSREF_USER_AGENT},
    ) as client:
        results = await asyncio.gather(*(_check_one(client, ref, gate) for ref in refs))

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
    """Build a CitationSummary from a list of CitationResult."""
    return CitationSummary(
        verified=sum(1 for item in results if item.status == CitationStatus.verified),
        ghost=sum(1 for item in results if item.status == CitationStatus.ghost),
        unverified=sum(1 for item in results if item.status == CitationStatus.unverified),
        total=len(results),
        items=results,
    )
