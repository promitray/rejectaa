"""Citation Autopsy service for DOI extraction and CrossRef verification."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from models import CitationResult, CitationStatus, CitationSummary

logger = logging.getLogger(__name__)

CROSSREF_BASE_URL = "https://api.crossref.org/works"
CROSSREF_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 8.0
MAX_REFERENCES = 60

ARXIV_DOI_PREFIX = "10.48550/arxiv."
ARXIV_API_URL = "https://export.arxiv.org/abs/"

DOI_PATTERN = r"10\.\d{4,9}/[^\s,\]>\"'\)]+"
DOI_TRAILING_PUNCTUATION = ".,);:"


def _is_arxiv_doi(doi: str) -> bool:
    """Return True if this DOI is an arXiv preprint DOI."""
    return doi.lower().startswith(ARXIV_DOI_PREFIX)


def _extract_arxiv_id(doi: str) -> str | None:
    """Extract the arXiv ID from an arXiv DOI.

    e.g. "10.48550/arXiv.1706.03762" -> "1706.03762"
         "10.48550/arXiv.2303.08774v2" -> "2303.08774v2"
    """
    parts = doi.split("arXiv.", maxsplit=1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    parts = doi.split("arxiv.", maxsplit=1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return None


async def _check_arxiv(
    client: httpx.AsyncClient,
    doi: str,
    ref: str,
) -> CitationResult:
    """Verify an arXiv DOI using the arXiv API.

    Called as fallback when CrossRef returns 404 for arXiv DOIs.
    Uses the arXiv abs page — if it returns 200 the paper exists.

    Returns CitationResult with status verified if found,
    ghost if arXiv also cannot find it, unverified on error.
    """
    arxiv_id = _extract_arxiv_id(doi)
    if not arxiv_id:
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.ghost,
            title=None,
            year=None,
        )

    try:
        url = f"{ARXIV_API_URL}{arxiv_id}"
        r = await client.get(url, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200:
            title: str | None = None
            import re as _re

            title_match = _re.search(
                r"<title>[^:]+:\s*(.+?)\s*</title>",
                r.text,
                _re.IGNORECASE,
            )
            if title_match:
                title = title_match.group(1).strip()

            logger.debug("arXiv confirmed DOI %s", doi)
            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.verified,
                title=title,
                year=None,
            )

        logger.debug("arXiv returned %s for %s", r.status_code, arxiv_id)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.ghost,
            title=None,
            year=None,
        )

    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("arXiv fallback failed for %s: %s", doi, exc)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )


def _extract_doi(text: str) -> str | None:
    """Extract a DOI from a reference string.

    Looks for the standard DOI pattern 10.XXXX/anything.
    Strips trailing punctuation that may have been captured.
    Returns None if no DOI found.
    """
    match = re.search(DOI_PATTERN, text)
    if not match:
        return None
    doi = match.group(0).rstrip(DOI_TRAILING_PUNCTUATION)
    return doi or None


async def _check_one(client: httpx.AsyncClient, ref: str) -> CitationResult:
    """Check a single reference string against CrossRef.

    Returns CitationResult with status:
    - verified: DOI found and resolves successfully
    - ghost: DOI found but returns 404 or error from CrossRef
    - unverified: no DOI found, or network/timeout error
    """
    doi = _extract_doi(ref)
    if not doi:
        return CitationResult(
            raw=ref,
            doi=None,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )

    try:
        url = f"{CROSSREF_BASE_URL}/{quote(doi, safe='')}"
        response = await client.get(url)

        if response.status_code == 200:
            data: Any = response.json()
            message: Any = data.get("message", {}) if isinstance(data, dict) else {}

            title: str | None = None
            titles = message.get("title")
            if isinstance(titles, list) and titles:
                first = titles[0]
                if isinstance(first, str) and first.strip():
                    title = first.strip()

            year: int | None = None
            published = message.get("published")
            if isinstance(published, dict):
                date_parts = published.get("date-parts")
                if (
                    isinstance(date_parts, list)
                    and date_parts
                    and isinstance(date_parts[0], list)
                    and date_parts[0]
                ):
                    try:
                        year = int(date_parts[0][0])
                    except (TypeError, ValueError):
                        year = None

            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.verified,
                title=title,
                year=year,
            )

        if response.status_code == 404:
            # Before marking as ghost, check if this is an arXiv DOI
            # CrossRef 404s many valid arXiv preprints
            if _is_arxiv_doi(doi):
                logger.debug(
                    "CrossRef 404 for arXiv DOI %s — trying arXiv API as fallback",
                    doi,
                )
                return await _check_arxiv(client, doi, ref)
            return CitationResult(
                raw=ref,
                doi=doi,
                status=CitationStatus.ghost,
                title=None,
                year=None,
            )

        logger.warning("CrossRef returned %s for DOI %s", response.status_code, doi)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )

    except httpx.TimeoutException:
        logger.debug("CrossRef timeout for DOI %s", doi)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )
    except httpx.RequestError as exc:
        logger.warning("CrossRef request error for %s: %s", doi, exc)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )
    except Exception:
        logger.error("Unexpected error checking DOI %s", doi, exc_info=True)
        return CitationResult(
            raw=ref,
            doi=doi,
            status=CitationStatus.unverified,
            title=None,
            year=None,
        )


async def verify_citations(references: list[str]) -> list[CitationResult]:
    """Verify a list of reference strings against CrossRef.

    Runs all checks concurrently using a single shared HTTP client.
    Caps at MAX_REFERENCES to prevent abuse.

    Args:
        references: List of raw reference strings from a paper.

    Returns:
        List of CitationResult, one per input reference,
        in the same order as the input list.
    """
    refs = references[:MAX_REFERENCES]
    logger.info("Verifying %d citations against CrossRef", len(refs))

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": CROSSREF_USER_AGENT},
    ) as client:
        results = await asyncio.gather(*(_check_one(client, ref) for ref in refs))

    verified = sum(1 for r in results if r.status == CitationStatus.verified)
    ghost = sum(1 for r in results if r.status == CitationStatus.ghost)
    unverified = sum(1 for r in results if r.status == CitationStatus.unverified)
    logger.info(
        "Citation check complete: %d verified, %d ghost, %d unverified",
        verified,
        ghost,
        unverified,
    )
    return results


def build_citation_summary(results: list[CitationResult]) -> CitationSummary:
    """Build a CitationSummary from a list of CitationResult.

    Args:
        results: Output from verify_citations().

    Returns:
        CitationSummary with counts and full item list.
    """
    verified_count = sum(1 for r in results if r.status == CitationStatus.verified)
    ghost_count = sum(1 for r in results if r.status == CitationStatus.ghost)
    unverified_count = sum(1 for r in results if r.status == CitationStatus.unverified)
    return CitationSummary(
        verified=verified_count,
        ghost=ghost_count,
        unverified=unverified_count,
        total=len(results),
        items=results,
    )
