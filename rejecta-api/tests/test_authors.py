"""Author profiles: ORCID validation, ORCID/OpenAlex mapping, and the shared cache."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from db.repos.author_profiles import CachedAuthor
from services import authors
from services.authors import AuthorLookupError, AuthorProfile

ORCID = "0000-0001-6187-6610"


# --- ORCID id ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "0000-0002-1825-0097",
        "  0000-0002-1825-0097 ",
        "https://orcid.org/0000-0002-1825-0097",
        "http://orcid.org/0000-0002-1825-0097/",
        "0000000218250097",
        "0000-0002-1694-233x",  # lower-case check digit X
    ],
)
def test_normalise_orcid_accepts_valid_forms(raw: str) -> None:
    assert authors.normalise_orcid(raw) is not None


def test_normalise_orcid_returns_canonical_form() -> None:
    assert authors.normalise_orcid("https://orcid.org/0000000218250097") == "0000-0002-1825-0097"
    assert authors.normalise_orcid("0000-0002-1694-233x") == "0000-0002-1694-233X"


@pytest.mark.parametrize(
    "raw",
    ["", "hello", "0000-0002-1825-0098", "0000-0002-1825", "0000-0002-1825-00977", "0000-0002-1825-009Z"],
)
def test_normalise_orcid_rejects_invalid(raw: str) -> None:
    assert authors.normalise_orcid(raw) is None


# --- upstream mapping --------------------------------------------------------------

ORCID_PERSON = {"name": {"given-names": {"value": "Jason"}, "family-name": {"value": "Priem"}, "credit-name": None}}

OA_AUTHOR = {
    "id": "https://openalex.org/A5023888391",
    "display_name": "Jason R Priem",
    "works_count": 3,
    "cited_by_count": 5365,
    "summary_stats": {"2yr_mean_citedness": 1.33, "h_index": 18, "i10_index": 21},
    "last_known_institutions": [{"display_name": "OpenAlex"}],
    "topics": [{"display_name": "Scientometrics", "count": 40}, {"display_name": "Open Access", "count": 29}],
}


def _work(work_id: str, year: int, kind: str, cites: int, source: dict | None) -> dict:
    return {
        "id": f"https://openalex.org/{work_id}",
        "publication_year": year,
        "type": kind,
        "cited_by_count": cites,
        "primary_location": {"source": source} if source else None,
    }


JOURNAL = {"id": "https://openalex.org/S1", "display_name": "Journal One", "type": "journal"}
REPO = {"id": "https://openalex.org/S2", "display_name": "arXiv", "type": "repository"}


def _transport(*, author: dict | None = OA_AUTHOR, orcid_ok: bool = True, calls: list[str] | None = None):
    works = [
        _work("W1", 2020, "article", 10, JOURNAL),
        _work("W2", 2021, "article", 5, JOURNAL),
        _work("W3", 2021, "preprint", 1, REPO),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        if "pub.orcid.org" in url:
            return httpx.Response(200, json=ORCID_PERSON) if orcid_ok else httpx.Response(404)
        if "/authors/" in url:
            return httpx.Response(200, json=author) if author else httpx.Response(404)
        if "/works" in url:
            return httpx.Response(200, json={"results": works, "meta": {"next_cursor": None}})
        if "/sources" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "https://openalex.org/S1", "summary_stats": {"2yr_mean_citedness": 4.2}},
                        {"id": "https://openalex.org/S2", "summary_stats": {"2yr_mean_citedness": 0.0}},
                    ]
                },
            )
        return httpx.Response(500)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authors, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.asyncio
async def test_fetch_profile_maps_orcid_and_openalex() -> None:
    async with httpx.AsyncClient(transport=_transport()) as client:
        profile, ttl = await authors.fetch_profile(client, ORCID)

    assert profile is not None
    assert profile.name == "Jason Priem"  # ORCID name wins
    assert profile.institution == "OpenAlex"
    assert (profile.works_count, profile.cited_by_count, profile.h_index, profile.i10_index) == (3, 5365, 18, 21)
    assert [w.id for w in profile.works] == ["W1", "W2", "W3"]
    assert profile.works[2].type == "preprint" and profile.works[2].venue_id == "S2"
    assert [(v.name, v.works, v.impact) for v in profile.venues] == [("Journal One", 2, 4.2), ("arXiv", 1, 0.0)]
    assert profile.venues[0].kind == "journal"
    assert [t.name for t in profile.topics] == ["Scientometrics", "Open Access"]
    assert profile.works_truncated is False
    assert ttl == authors.TTL_FOUND


@pytest.mark.asyncio
async def test_unknown_orcid_is_not_found() -> None:
    async with httpx.AsyncClient(transport=_transport(author=None, orcid_ok=False)) as client:
        profile, ttl = await authors.fetch_profile(client, ORCID)
    assert profile is None
    assert ttl == authors.TTL_SHORT


@pytest.mark.asyncio
async def test_orcid_without_openalex_record_has_no_publications() -> None:
    async with httpx.AsyncClient(transport=_transport(author=None)) as client:
        profile, ttl = await authors.fetch_profile(client, ORCID)
    assert profile is not None and profile.has_publications is False
    assert profile.name == "Jason Priem"
    assert ttl == authors.TTL_SHORT  # refreshed soon, OpenAlex may index them later


@pytest.mark.asyncio
async def test_upstream_server_error_raises() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AuthorLookupError):
            await authors.fetch_profile(client, ORCID)


@pytest.mark.asyncio
async def test_partial_record_when_venue_impact_fails_is_cached_briefly() -> None:
    inner = _transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/sources" in str(request.url):
            return httpx.Response(503)
        return inner.handle_request(request)  # type: ignore[return-value]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        profile, ttl = await authors.fetch_profile(client, ORCID)
    assert profile is not None
    assert all(v.impact is None for v in profile.venues)
    assert ttl == authors.TTL_SHORT


# --- shared cache ------------------------------------------------------------------


def _profile() -> AuthorProfile:
    return AuthorProfile(orcid=ORCID, name="Jason Priem", works_count=3)


def _row(*, fresh: bool, status: str = "found") -> CachedAuthor:
    now = datetime.now(timezone.utc)
    return CachedAuthor(
        status=status,
        display_name="Jason Priem",
        payload=_profile().model_dump(mode="json") if status == "found" else None,
        fetched_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=5) if fresh else now - timedelta(hours=1),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, *, row: CachedAuthor | None, fetch=None):
    """Replace the database and upstream calls; returns the lists the fakes record into."""
    fetched: list[str] = []
    written: list[tuple] = []

    async def read(_orcid: str):
        return row

    async def write(orcid: str, profile, ttl: float) -> None:
        written.append((orcid, profile, ttl))

    async def live(orcid: str):
        fetched.append(orcid)
        if fetch is not None:
            return fetch()
        return _profile(), authors.TTL_FOUND

    monkeypatch.setattr(authors, "_cache_read", read)
    monkeypatch.setattr(authors, "_cache_write", write)
    monkeypatch.setattr(authors, "_fetch_single_flight", live)
    return fetched, written


@pytest.mark.asyncio
async def test_fresh_row_is_served_without_calling_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched, written = _wire(monkeypatch, row=_row(fresh=True))
    result = await authors.get_author_profile(ORCID)
    assert result.source == "cache" and result.found and result.profile is not None
    assert result.profile.name == "Jason Priem"
    assert fetched == [] and written == []


@pytest.mark.asyncio
async def test_cache_miss_goes_live_then_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched, written = _wire(monkeypatch, row=None)
    result = await authors.get_author_profile(ORCID)
    assert result.source == "live" and result.found
    assert fetched == [ORCID]
    assert len(written) == 1 and written[0][0] == ORCID and written[0][2] == authors.TTL_FOUND


@pytest.mark.asyncio
async def test_expired_row_is_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched, written = _wire(monkeypatch, row=_row(fresh=False))
    result = await authors.get_author_profile(ORCID)
    assert result.source == "live"
    assert fetched == [ORCID] and len(written) == 1


@pytest.mark.asyncio
async def test_expired_row_is_served_when_upstream_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise AuthorLookupError("down")

    fetched, written = _wire(monkeypatch, row=_row(fresh=False), fetch=boom)
    result = await authors.get_author_profile(ORCID)
    assert result.source == "stale" and result.found
    assert written == []


@pytest.mark.asyncio
async def test_upstream_down_without_a_row_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise AuthorLookupError("down")

    _wire(monkeypatch, row=None, fetch=boom)
    with pytest.raises(AuthorLookupError):
        await authors.get_author_profile(ORCID)


@pytest.mark.asyncio
async def test_not_found_is_cached_and_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    _, written = _wire(monkeypatch, row=None, fetch=lambda: (None, authors.TTL_SHORT))
    result = await authors.get_author_profile(ORCID)
    assert result.found is False and result.profile is None
    assert written[0][1] is None

    _wire(monkeypatch, row=_row(fresh=True, status="not_found"))
    again = await authors.get_author_profile(ORCID)
    assert again.found is False and again.source == "cache"


@pytest.mark.asyncio
async def test_concurrent_lookups_share_one_upstream_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def slow_live(_orcid: str):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return _profile(), authors.TTL_FOUND

    monkeypatch.setattr(authors, "_fetch_live", slow_live)
    authors._inflight.clear()
    results = await asyncio.gather(*(authors._fetch_single_flight(ORCID) for _ in range(5)))
    assert calls == 1
    assert all(profile is not None and profile.orcid == ORCID for profile, _ in results)
    assert authors._inflight == {}
