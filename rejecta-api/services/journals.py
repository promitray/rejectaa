"""Journal matching service using OpenAlex sources and works APIs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from models import JournalMatch, RecentPaper

OPENALEX_BASE_URL = "https://api.openalex.org"
OPENALEX_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 10.0
RECENT_PAPERS_COUNT = 5

logger = logging.getLogger(__name__)


async def match_journal(
    abstract: str,
    target_journal: str,
) -> JournalMatch:
    """Find a journal in OpenAlex and return its metadata.

    Searches OpenAlex sources by journal name, then fetches
    the most recent papers published there.

    Args:
        abstract: Paper abstract — reserved for future semantic
                  matching, not used in v1.
        target_journal: Journal name as entered by the user.

    Returns:
        JournalMatch. found=False if journal cannot be located.
    """
    _ = abstract  # reserved for future semantic matching

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": OPENALEX_USER_AGENT},
        ) as client:
            sources_response = await client.get(
                f"{OPENALEX_BASE_URL}/sources",
                params={"search": target_journal, "per_page": 1},
            )
            sources_response.raise_for_status()
            sources_payload: dict[str, Any] = sources_response.json()
            results = sources_payload.get("results") or []

            if not results:
                logger.warning("Journal not found in OpenAlex: %s", target_journal)
                return JournalMatch(found=False, target=target_journal)

            source = results[0]
            source_id = str(source["id"]).split("/")[-1]
            display_name = source.get("display_name")
            works_count = source.get("works_count")
            logger.debug(
                "Found journal: %s (%s works)",
                display_name,
                works_count,
            )

            recent_papers: list[RecentPaper] = []
            try:
                works_response = await client.get(
                    f"{OPENALEX_BASE_URL}/works",
                    params={
                        "filter": f"primary_location.source.id:{source_id}",
                        "per_page": RECENT_PAPERS_COUNT,
                        "sort": "publication_date:desc",
                        "select": "title,publication_year",
                    },
                )
                works_response.raise_for_status()
                works_payload: dict[str, Any] = works_response.json()
                works_results = works_payload.get("results") or []
                recent_papers = [
                    RecentPaper(
                        title=str(work.get("title")),
                        year=work.get("publication_year")
                        if isinstance(work.get("publication_year"), int)
                        else None,
                    )
                    for work in works_results
                    if work.get("title")
                ]
                logger.debug("Fetched %d recent papers", len(recent_papers))
            except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "Failed to fetch recent papers for %s: %s",
                    target_journal,
                    exc,
                )
                recent_papers = []

            return JournalMatch(
                found=True,
                target=target_journal,
                openalex_name=display_name,
                works_count=works_count,
                recent_papers=recent_papers,
            )
    except httpx.TimeoutException as exc:
        logger.warning(
            "OpenAlex timeout while matching journal %s: %s",
            target_journal,
            exc,
        )
        return JournalMatch(found=False, target=target_journal)
    except httpx.RequestError as exc:
        logger.warning(
            "OpenAlex request error while matching journal %s: %s",
            target_journal,
            exc,
        )
        return JournalMatch(found=False, target=target_journal)
