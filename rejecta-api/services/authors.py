"""Author profiles from ORCID and OpenAlex, cached in the database for everyone.

Flow for one ORCID:

1. A fresh row in ``author_profiles`` answers the request (no upstream call).
2. Otherwise ORCID (name) and OpenAlex (record) are asked, in parallel.
3. The result is stored, so the next lookup by any user is a database read.
4. If the sources are down and an expired row exists, that row is served
   (``source = "stale"``) instead of an error.

The database is optional: without it every lookup goes upstream.

Impact is OpenAlex's ``2yr_mean_citedness``. It is close to, but not, the
Clarivate JCR Impact Factor (that one is licensed data), so it is exposed under
its own name.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from config import settings
from db.pool import DatabaseUnavailableError, get_pool
from db.repos import author_profiles as cache_repo

logger = logging.getLogger("rejecta.authors")

OPENALEX_BASE_URL = "https://api.openalex.org"
ORCID_BASE_URL = "https://pub.orcid.org/v3.0"
USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 15.0
RETRY_DELAY_SECONDS = 0.6

WORKS_PER_PAGE = 200
MAX_WORK_PAGES = 5  # 1000 works; beyond that the profile is flagged truncated
TOP_JOURNALS = 8
TOP_OTHER_VENUES = 4  # repositories, conferences, ...
TOP_TOPICS = 8

DAY = 86_400.0
TTL_FOUND = 14 * DAY  # publication records change slowly
TTL_SHORT = 1 * DAY  # not found, no works yet, or venue impact missing

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_ORCID_IN_URL = re.compile(r"orcid\.org/", re.IGNORECASE)


class AuthorLookupError(Exception):
    """ORCID or OpenAlex could not be reached or answered with an error."""


# --- models ------------------------------------------------------------------


class AuthorWork(BaseModel):
    """One paper, kept small: a profile can hold up to 1000 of these."""

    id: str  # OpenAlex work id, e.g. W2741809807
    year: int | None = None
    type: str = "other"  # article, review, preprint, book-chapter, ...
    cites: int = 0
    venue_id: str | None = None  # OpenAlex source id, e.g. S137773608


class VenueInfo(BaseModel):
    id: str
    name: str
    kind: str | None = None  # journal, repository, conference, ...
    works: int  # how many of this author's papers appeared here
    impact: float | None = None  # OpenAlex 2-year mean citedness


class TopicInfo(BaseModel):
    name: str
    count: int


class AuthorProfile(BaseModel):
    orcid: str
    name: str | None = None
    openalex_id: str | None = None
    institution: str | None = None
    works_count: int = 0
    cited_by_count: int = 0
    h_index: int = 0
    i10_index: int = 0
    mean_citedness_2y: float | None = None
    works: list[AuthorWork] = []
    works_truncated: bool = False
    venues: list[VenueInfo] = []
    topics: list[TopicInfo] = []
    has_publications: bool = True


class AuthorProfileResponse(BaseModel):
    orcid: str
    found: bool
    source: Literal["cache", "live", "stale"]
    fetched_at: datetime
    profile: AuthorProfile | None = None
    message: str | None = None


# --- ORCID id ------------------------------------------------------------------


def _orcid_check_digit(first_15_digits: str) -> str:
    """ISO 7064 mod 11-2, as ORCID defines it."""
    total = 0
    for char in first_15_digits:
        total = (total + int(char)) * 2
    result = (12 - total % 11) % 11
    return "X" if result == 10 else str(result)


def normalise_orcid(raw: str) -> str | None:
    """``0000-0002-1825-0097`` from a bare id, a URL, or digits without dashes. None if invalid."""
    text = _ORCID_IN_URL.split(raw.strip(), maxsplit=1)[-1].strip().strip("/").upper()
    digits = text.replace("-", "").replace(" ", "")
    if len(digits) == 16:
        text = "-".join(digits[i : i + 4] for i in range(0, 16, 4))
    if not _ORCID_RE.match(text):
        return None
    compact = text.replace("-", "")
    if _orcid_check_digit(compact[:15]) != compact[15]:
        return None
    return text


# --- upstream helpers ----------------------------------------------------------


def _openalex_params(extra: dict[str, Any]) -> dict[str, Any]:
    params = dict(extra)
    key = settings.OPENALEX_API_KEY.strip()
    if key:
        params["api_key"] = key
    else:
        params["mailto"] = "hello@rejecta.ai"
    return params


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """GET with one retry on 429/5xx/network errors. 404 returns ``(404, None)``."""
    last_error = "unknown error"
    for attempt in range(2):
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}"
        else:
            if response.status_code == 404:
                return 404, None
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise AuthorLookupError(f"HTTP {response.status_code} from {url.split('?')[0]}")
            else:
                try:
                    return response.status_code, response.json()
                except ValueError as exc:
                    raise AuthorLookupError("Upstream returned invalid JSON") from exc
        if attempt == 0:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise AuthorLookupError(f"Upstream unavailable ({last_error})")


def _short_id(value: Any) -> str | None:
    """``https://openalex.org/A123`` -> ``A123``."""
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def _orcid_name(client: httpx.AsyncClient, orcid: str) -> tuple[bool, str | None]:
    """(orcid_exists, name). The name is None when the researcher keeps it private."""
    status, data = await _get_json(
        client, f"{ORCID_BASE_URL}/{orcid}/person", headers={"Accept": "application/json"}
    )
    if status == 404 or not isinstance(data, dict):
        return False, None
    name = data.get("name")
    if not isinstance(name, dict):
        return True, None

    def value(key: str) -> str:
        node = name.get(key)
        return str(node.get("value") or "").strip() if isinstance(node, dict) else ""

    credit = value("credit-name")
    full = " ".join(part for part in (value("given-names"), value("family-name")) if part)
    return True, (credit or full or None)


async def _openalex_author(client: httpx.AsyncClient, orcid: str) -> dict[str, Any] | None:
    status, data = await _get_json(
        client,
        f"{OPENALEX_BASE_URL}/authors/https://orcid.org/{orcid}",
        params=_openalex_params(
            {"select": "id,display_name,works_count,cited_by_count,summary_stats,last_known_institutions,topics"}
        ),
    )
    return data if status != 404 and isinstance(data, dict) else None


async def _openalex_works(
    client: httpx.AsyncClient, author_id: str, expected: int
) -> tuple[list[AuthorWork], dict[str, tuple[str, str | None]], bool]:
    """Up to MAX_WORK_PAGES pages of works. Returns (works, venue_id -> (name, kind), truncated)."""
    works: list[AuthorWork] = []
    venue_meta: dict[str, tuple[str, str | None]] = {}
    cursor: str | None = "*"
    pages = 0
    while cursor and pages < MAX_WORK_PAGES and (pages == 0 or len(works) < expected):
        _, data = await _get_json(
            client,
            f"{OPENALEX_BASE_URL}/works",
            params=_openalex_params(
                {
                    "filter": f"author.id:{author_id}",
                    "per-page": WORKS_PER_PAGE,
                    "cursor": cursor,
                    "select": "id,type,publication_year,cited_by_count,primary_location",
                }
            ),
        )
        if not isinstance(data, dict):
            break
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            work_id = _short_id(item.get("id"))
            if not work_id:
                continue
            source = (item.get("primary_location") or {}).get("source") or {}
            venue_id = _short_id(source.get("id"))
            if venue_id and venue_id not in venue_meta:
                venue_meta[venue_id] = (str(source.get("display_name") or venue_id), source.get("type"))
            year = item.get("publication_year")
            works.append(
                AuthorWork(
                    id=work_id,
                    year=year if isinstance(year, int) else None,
                    type=str(item.get("type") or "other"),
                    cites=_int(item.get("cited_by_count")),
                    venue_id=venue_id,
                )
            )
        cursor = (data.get("meta") or {}).get("next_cursor")
        pages += 1
    truncated = bool(cursor) and len(works) < expected
    return works, venue_meta, truncated


async def _venue_impacts(client: httpx.AsyncClient, venue_ids: list[str]) -> dict[str, float | None] | None:
    """2-year mean citedness per source. None when the lookup failed (best effort)."""
    if not venue_ids:
        return {}
    try:
        _, data = await _get_json(
            client,
            f"{OPENALEX_BASE_URL}/sources",
            params=_openalex_params(
                {
                    "filter": "openalex:" + "|".join(venue_ids),
                    "select": "id,summary_stats",
                    "per-page": len(venue_ids),
                }
            ),
        )
    except AuthorLookupError:
        return None
    impacts: dict[str, float | None] = {}
    for item in (data or {}).get("results") or []:
        if isinstance(item, dict) and (source_id := _short_id(item.get("id"))):
            stats = item.get("summary_stats")
            impacts[source_id] = _float(stats.get("2yr_mean_citedness")) if isinstance(stats, dict) else None
    return impacts


# --- building a profile ----------------------------------------------------------


async def fetch_profile(client: httpx.AsyncClient, orcid: str) -> tuple[AuthorProfile | None, float]:
    """Ask ORCID and OpenAlex. Returns ``(profile or None if the ORCID does not exist, cache ttl)``."""
    orcid_result, author = await asyncio.gather(_orcid_name(client, orcid), _openalex_author(client, orcid))
    exists, orcid_display = orcid_result

    if author is None:
        if not exists:
            return None, TTL_SHORT
        # A real ORCID that OpenAlex has no record for (new, or no works indexed yet).
        return AuthorProfile(orcid=orcid, name=orcid_display, has_publications=False), TTL_SHORT

    stats = author.get("summary_stats") if isinstance(author.get("summary_stats"), dict) else {}
    institutions = author.get("last_known_institutions") or []
    institution = (
        str(institutions[0].get("display_name"))
        if institutions and isinstance(institutions[0], dict) and institutions[0].get("display_name")
        else None
    )
    topics = [
        TopicInfo(name=str(t["display_name"]), count=_int(t.get("count")))
        for t in (author.get("topics") or [])[:TOP_TOPICS]
        if isinstance(t, dict) and t.get("display_name")
    ]
    author_id = _short_id(author.get("id"))
    works_count = _int(author.get("works_count"))

    works: list[AuthorWork] = []
    venue_meta: dict[str, tuple[str, str | None]] = {}
    truncated = False
    if author_id and works_count:
        works, venue_meta, truncated = await _openalex_works(client, author_id, works_count)

    # Repositories (arXiv, Zenodo) can outnumber journals and their "impact" means nothing,
    # so journals are ranked on their own and the rest get a few slots.
    ranked = Counter(w.venue_id for w in works if w.venue_id).most_common()
    journals = [item for item in ranked if venue_meta[item[0]][1] == "journal"][:TOP_JOURNALS]
    others = [item for item in ranked if venue_meta[item[0]][1] != "journal"][:TOP_OTHER_VENUES]
    top = sorted(journals + others, key=lambda item: -item[1])
    impacts = await _venue_impacts(client, [venue_id for venue_id, _ in top])
    venues = [
        VenueInfo(
            id=venue_id,
            name=venue_meta[venue_id][0],
            kind=venue_meta[venue_id][1],
            works=count,
            impact=(impacts or {}).get(venue_id),
        )
        for venue_id, count in top
    ]

    profile = AuthorProfile(
        orcid=orcid,
        name=orcid_display or (str(author["display_name"]) if author.get("display_name") else None),
        openalex_id=author_id,
        institution=institution,
        works_count=works_count,
        cited_by_count=_int(author.get("cited_by_count")),
        h_index=_int(stats.get("h_index")),
        i10_index=_int(stats.get("i10_index")),
        mean_citedness_2y=_float(stats.get("2yr_mean_citedness")),
        works=works,
        works_truncated=truncated,
        venues=venues,
        topics=topics,
        has_publications=works_count > 0,
    )
    # A partial record (venue impact missing, or nothing published yet) is refreshed sooner.
    complete = impacts is not None and works_count > 0
    return profile, TTL_FOUND if complete else TTL_SHORT


# --- cache + single flight ---------------------------------------------------------

_inflight: dict[str, asyncio.Task[tuple[AuthorProfile | None, float]]] = {}


async def _fetch_live(orcid: str) -> tuple[AuthorProfile | None, float]:
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        return await fetch_profile(client, orcid)


async def _fetch_single_flight(orcid: str) -> tuple[AuthorProfile | None, float]:
    """Concurrent requests for one ORCID share a single upstream fetch."""
    task = _inflight.get(orcid)
    if task is None:
        task = asyncio.ensure_future(_fetch_live(orcid))
        _inflight[orcid] = task
        task.add_done_callback(lambda _done: _inflight.pop(orcid, None))
    return await asyncio.shield(task)


async def _cache_read(orcid: str) -> cache_repo.CachedAuthor | None:
    try:
        async with get_pool().acquire() as conn:
            return await cache_repo.get_and_touch(conn, orcid)
    except DatabaseUnavailableError:
        return None
    except Exception:  # noqa: BLE001 - the cache must never break a lookup
        logger.warning("Author cache read failed", exc_info=True)
        return None


async def _cache_write(orcid: str, profile: AuthorProfile | None, ttl: float) -> None:
    try:
        async with get_pool().acquire() as conn:
            await cache_repo.upsert(
                conn,
                orcid=orcid,
                status="found" if profile else "not_found",
                display_name=profile.name if profile else None,
                payload=profile.model_dump(mode="json") if profile else None,
                ttl_seconds=ttl,
            )
    except DatabaseUnavailableError:
        pass
    except Exception:  # noqa: BLE001
        logger.warning("Author cache write failed", exc_info=True)


def _response(
    orcid: str,
    profile: AuthorProfile | None,
    source: Literal["cache", "live", "stale"],
    fetched_at: datetime,
) -> AuthorProfileResponse:
    if profile is None:
        return AuthorProfileResponse(
            orcid=orcid,
            found=False,
            source=source,
            fetched_at=fetched_at,
            message="No researcher with this ORCID was found. Check the number.",
        )
    message = None
    if not profile.has_publications:
        message = "This ORCID exists, but OpenAlex has no publications for it yet."
    return AuthorProfileResponse(
        orcid=orcid, found=True, source=source, fetched_at=fetched_at, profile=profile, message=message
    )


def _from_cache(orcid: str, row: cache_repo.CachedAuthor, source: Literal["cache", "stale"]) -> AuthorProfileResponse:
    profile = AuthorProfile.model_validate(row.payload) if row.status == "found" and row.payload else None
    return _response(orcid, profile, source, row.fetched_at)


async def get_author_profile(orcid: str) -> AuthorProfileResponse:
    """The profile for a normalised, valid ORCID: from the shared cache when fresh, else live."""
    now = datetime.now(timezone.utc)
    cached = await _cache_read(orcid)
    if cached is not None and cached.is_fresh(now):
        logger.info("author lookup orcid=%s source=cache", orcid)
        return _from_cache(orcid, cached, "cache")

    try:
        profile, ttl = await _fetch_single_flight(orcid)
    except AuthorLookupError:
        if cached is not None:
            logger.warning("author lookup orcid=%s upstream down, serving stale row", orcid)
            return _from_cache(orcid, cached, "stale")
        raise

    await _cache_write(orcid, profile, ttl)
    logger.info("author lookup orcid=%s source=live found=%s", orcid, profile is not None)
    return _response(orcid, profile, "live", datetime.now(timezone.utc))
