"""OpenAlex client for the literature knowledge base: works, searches, a journal's works, and sources.

Paced with the shared OpenAlex limiter (services.citation_http), with OPENALEX_API_KEY when it is set. Answers are
cached in the local store (services.local_store, namespace "openalex") so the same request is not sent twice.
Never raises for network problems: a failed request gives an empty answer and the caller decides.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import httpx

from config import settings
from services.citation_http import limiter
from services.literature.types import Work
from services.local_store import digest, get_store

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openalex.org"
USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 20.0
CACHE_NAMESPACE = "openalex"
WORK_FIELDS = (
    "id,doi,title,display_name,publication_date,publication_year,type,primary_location,authorships,"
    "abstract_inverted_index,referenced_works,related_works,concepts,cited_by_count,ids"
)
MAX_PER_PAGE = 200


def short_id(url_or_id: str | None) -> str:
    """"https://openalex.org/W123" -> "W123"."""
    return (url_or_id or "").rstrip("/").rsplit("/", 1)[-1]


def abstract_from_index(index: dict[str, list[int]] | None) -> str:
    """OpenAlex gives abstracts as an inverted index (word -> positions); rebuild the text."""
    if not index:
        return ""
    positions: dict[int, str] = {}
    for word, places in index.items():
        for place in places:
            positions[place] = word
    return " ".join(positions[p] for p in sorted(positions))


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", value.strip(), flags=re.IGNORECASE).lower() or None


def _arxiv(work: dict[str, Any]) -> str | None:
    for location in [work.get("primary_location") or {}, *(work.get("locations") or [])]:
        url = str((location or {}).get("landing_page_url") or "")
        found = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", url)
        if found:
            return found.group(1)
    doi = _doi(work.get("doi")) or ""
    found = re.search(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", doi)
    return found.group(1) if found else None


def work_from_json(data: dict[str, Any]) -> Work:
    """A Work from an OpenAlex work record."""
    abstract = abstract_from_index(data.get("abstract_inverted_index"))
    source = ((data.get("primary_location") or {}).get("source") or {})
    authors = data.get("authorships") or []
    date = data.get("publication_date")
    try:
        publication_date = dt.date.fromisoformat(date) if date else None
    except ValueError:
        publication_date = None
    institutions: list[str] = []
    for a in authors:
        for inst in a.get("institutions") or []:
            iid = short_id(inst.get("id"))
            if iid and iid not in institutions:
                institutions.append(iid)
    return Work(
        id=short_id(data.get("id")),
        title=str(data.get("title") or data.get("display_name") or "").strip(),
        abstract=abstract,
        abstract_missing=not bool(abstract.strip()),
        doi=_doi(data.get("doi")),
        arxiv_id=_arxiv(data),
        publication_date=publication_date,
        year=data.get("publication_year") if isinstance(data.get("publication_year"), int) else None,
        venue_id=short_id(source.get("id")) or None,
        venue_name=source.get("display_name"),
        work_type=data.get("type"),
        author_ids=[short_id((a.get("author") or {}).get("id")) for a in authors if (a.get("author") or {}).get("id")],
        author_names=[str((a.get("author") or {}).get("display_name") or "") for a in authors],
        institution_ids=institutions,
        reference_ids=[short_id(r) for r in data.get("referenced_works") or []],
        related_ids=[short_id(r) for r in data.get("related_works") or []],
        concepts=[str(c.get("display_name")) for c in (data.get("concepts") or [])[:10] if c.get("display_name")],
        cited_by_count=int(data.get("cited_by_count") or 0),
    )


class OpenAlexClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._own = client is None
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})

    async def close(self) -> None:
        if self._own:
            await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        query = dict(params)
        key_param = settings.OPENALEX_API_KEY.strip()
        cache_key = digest(path, *(f"{k}={query[k]}" for k in sorted(query)))
        store = get_store()
        found = await store.get(CACHE_NAMESPACE, cache_key)
        if isinstance(found, dict):
            return found
        if key_param:
            query["api_key"] = key_param
        fetch = await limiter("OpenAlex", anonymous=not key_param).get(
            self.client, f"{BASE_URL}{path}", params=query, timeout=REQUEST_TIMEOUT
        )
        if not fetch.ok or fetch.response is None:
            logger.info("OpenAlex %s failed: %s", path, fetch.detail)
            return None
        try:
            data = fetch.response.json()
        except ValueError:
            return None
        await store.put(CACHE_NAMESPACE, cache_key, data)
        return data

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Any OpenAlex endpoint, paced and cached (e.g. "/authors")."""
        return await self._get(path, params)

    async def works_by_dois(self, dois: list[str]) -> list[Work]:
        """Works by DOI, 50 per request."""
        out: list[Work] = []
        for start in range(0, len(dois), 50):
            chunk = [d for d in dois[start : start + 50] if d]
            if not chunk:
                continue
            data = await self._get("/works", {"filter": f"doi:{'|'.join(chunk)}", "per_page": 50, "select": WORK_FIELDS})
            out += [work_from_json(w) for w in (data or {}).get("results") or []]
        return out

    async def citing(self, work_id: str, before: str, per_page: int = 25) -> list[Work]:
        """The most cited works that cite `work_id`, published before the date `before` (YYYY-MM-DD)."""
        data = await self._get("/works", {
            "filter": f"cites:{short_id(work_id)},to_publication_date:{before}", "sort": "cited_by_count:desc",
            "per_page": per_page, "select": WORK_FIELDS,
        })
        return [work_from_json(w) for w in (data or {}).get("results") or []]

    async def work(self, identifier: str) -> Work | None:
        """A work by OpenAlex id ("W123"), DOI ("10.1/x") or arXiv id."""
        ident = identifier.strip()
        if ident.lower().startswith("10."):
            ident = f"doi:{ident}"
        data = await self._get(f"/works/{ident}", {"select": WORK_FIELDS})
        return work_from_json(data) if data and data.get("id") else None

    async def works_by_ids(self, ids: list[str]) -> list[Work]:
        """Works by OpenAlex ids, 50 per request (the OR filter limit)."""
        out: list[Work] = []
        for start in range(0, len(ids), 50):
            chunk = [short_id(i) for i in ids[start : start + 50] if i]
            if not chunk:
                continue
            data = await self._get("/works", {"filter": f"openalex:{'|'.join(chunk)}", "per_page": 50, "select": WORK_FIELDS})
            out += [work_from_json(w) for w in (data or {}).get("results") or []]
        return out

    async def search(self, query: str, filters: str = "", per_page: int = 50, sort: str = "") -> list[Work]:
        params: dict[str, Any] = {"search": query, "per_page": min(per_page, MAX_PER_PAGE), "select": WORK_FIELDS}
        if filters:
            params["filter"] = filters
        if sort:
            params["sort"] = sort
        data = await self._get("/works", params)
        return [work_from_json(w) for w in (data or {}).get("results") or []]

    async def sample_works(self, filters: str, n: int, seed: int) -> list[Work]:
        """A reproducible random sample (OpenAlex `sample` + `seed`) of the works matching `filters`."""
        data = await self._get(
            "/works", {"filter": filters, "sample": n, "seed": seed, "per_page": min(n, MAX_PER_PAGE), "select": WORK_FIELDS}
        )
        return [work_from_json(w) for w in (data or {}).get("results") or []]

    async def find_source(self, name: str) -> dict[str, Any] | None:
        """The OpenAlex source (journal) whose name matches exactly (case-insensitive), or None."""
        data = await self._get("/sources", {"search": name, "per_page": 10, "select": "id,display_name,works_count,type"})
        wanted = re.sub(r"\s+", " ", name.strip().lower())
        for source in (data or {}).get("results") or []:
            if re.sub(r"\s+", " ", str(source.get("display_name") or "").strip().lower()) == wanted:
                return {"id": short_id(source.get("id")), "display_name": source.get("display_name"),
                        "works_count": source.get("works_count")}
        return None
