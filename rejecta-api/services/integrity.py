"""Publication integrity gate: is the uploaded paper already published, retracted, or under an expression of concern?

Runs before the paper is scored. A retraction is a fact about the publication record, not about how the text
reads: a retracted paper passed peer review once and usually reads well. So this is decided from records, not by
the model:

1. the file itself: a "RETRACTED ARTICLE" watermark or banner (services.parser reads it before running headers
   are removed);
2. Crossref, for the paper's own DOI (printed in the file, or found by an exact title and author match). Crossref
   carries the publisher's notices and, since Crossref took over the Retraction Watch database in 2023, the
   Retraction Watch records too, each in `updated-by` with its `source`;
3. OpenAlex `is_retracted` as a second opinion only. It is precise but incomplete, and it once marked
   corrections as retractions, so on its own it adds a note, never a verdict.

Nothing here raises: a lookup that fails gives "not_checked" with whatever the file showed, and the analysis goes on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import httpx

from config import settings
from models import FileMark, IntegrityNotice, ParsedDocument, PublicationStatus
from services.citation_http import NOT_FOUND, OK, limiter

logger = logging.getLogger(__name__)

CROSSREF_WORKS_URL = "https://api.crossref.org/works"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
MAILTO = "hello@rejecta.ai"
USER_AGENT = f"Rejecta/1.0 (mailto:{MAILTO})"
REQUEST_TIMEOUT = 8.0
TOTAL_BUDGET_SECONDS = 20.0
CACHE_TTL_SECONDS = 6 * 3600.0
MAX_CACHE_ENTRIES = 2000

# A DOI found in the file counts as this paper's only when most of the record's title words are on the first page.
TITLE_WORDS_ON_FRONT_PAGE = 0.8
# Without a DOI, a search hit counts only when its title is (nearly) the same and an author is on the first page.
TITLE_SEARCH_SIMILARITY = 0.93
MIN_SEARCH_TITLE_CHARS = 25
TITLE_SEARCH_ROWS = 5

RETRACTION_TYPES = frozenset({"retraction", "partial_retraction", "withdrawal", "removal"})
CONCERN_TYPES = frozenset({"expression_of_concern"})
CORRECTION_TYPES = frozenset({"correction", "corrigendum", "erratum", "addendum", "clarification"})
PREPRINT_TYPES = frozenset({"posted-content"})

# Publishers put these in front of the title of a retracted or flagged article, or of a notice.
_TITLE_PREFIX = re.compile(
    r"(?i)^\s*(?:retracted(?:\s+article)?|withdrawn|retraction(?:\s+note)?|notice of retraction|"
    r"expression of concern|correction|erratum|corrigendum)\s*[:\-–]\s*"
)
_RETRACTED_TITLE = re.compile(r"(?i)^\s*(?:retracted(?:\s+article)?|withdrawn)\s*[:\-–]")


@dataclass
class _Record:
    doi: str
    title: str
    journal: str | None
    journal_abbreviation: str | None
    publisher: str | None
    year: int | None
    type: str
    authors: list[str]
    notices: list[IntegrityNotice] = field(default_factory=list)  # what updates this paper
    updates: list[IntegrityNotice] = field(default_factory=list)  # what this record updates (it is a notice)


_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str) -> tuple[bool, Any]:
    hit = _cache.get(key)
    if hit is None or time.monotonic() - hit[0] > CACHE_TTL_SECONDS:
        return False, None
    return True, hit[1]


def _store(key: str, value: Any) -> None:
    if len(_cache) >= MAX_CACHE_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    _cache.clear()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{3,}", (text or "").lower())


def _plain_title(title: str) -> str:
    return _TITLE_PREFIX.sub("", re.sub(r"<[^>]+>", "", title or "")).strip()


def _similar(a: str, b: str) -> float:
    left, right = " ".join(_words(_plain_title(a))), " ".join(_words(_plain_title(b)))
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def _title_on_page(title: str, front_text: str) -> bool:
    words = _words(_plain_title(title))
    if not words:
        return False
    on_page = set(_words(front_text))
    return sum(1 for w in words if w in on_page) / len(words) >= TITLE_WORDS_ON_FRONT_PAGE


def _date(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    stamp = value.get("date-time")
    if isinstance(stamp, str) and len(stamp) >= 10:
        return stamp[:10]
    parts = (value.get("date-parts") or [[None]])[0]
    if parts and parts[0]:
        return "-".join(f"{int(p):02d}" if i else str(int(p)) for i, p in enumerate(parts[:3]))
    return None


def _notices(items: Any) -> list[IntegrityNotice]:
    """Crossref update entries, once each: the same notice may come from the publisher and Retraction Watch."""
    found: dict[tuple[str, str], IntegrityNotice] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower().replace("-", "_")
        if not kind:
            continue
        doi = str(item.get("DOI") or "") or None
        key = (kind, (doi or "").lower())
        source = str(item.get("source") or "")
        if key in found:
            if source and source not in found[key].source.split(", "):
                found[key].source = ", ".join(filter(None, [found[key].source, source]))
            continue
        found[key] = IntegrityNotice(
            type=kind, label=str(item.get("label") or kind.replace("_", " ").capitalize()), doi=doi,
            date=_date(item.get("updated")), source=source,
        )
    return sorted(found.values(), key=lambda n: (n.date or "", n.type, n.doi or ""))


def _record(message: dict[str, Any]) -> _Record:
    titles = message.get("title") or [""]
    journals = message.get("container-title") or []
    short = message.get("short-container-title") or []
    issued = (message.get("issued") or message.get("published") or {}).get("date-parts") or [[None]]
    year = issued[0][0] if issued and issued[0] and isinstance(issued[0][0], int) else None
    return _Record(
        doi=str(message.get("DOI") or ""),
        title=str(titles[0] if titles else ""),
        journal=str(journals[0]) if journals else None,
        journal_abbreviation=str(short[0]) if short else None,
        publisher=message.get("publisher"),
        year=year,
        type=str(message.get("type") or ""),
        authors=[str(a.get("family") or "") for a in message.get("author") or [] if isinstance(a, dict)],
        notices=_notices(message.get("updated-by")),
        updates=_notices(message.get("update-to")),
    )


async def _crossref_doi(client: httpx.AsyncClient, doi: str) -> tuple[bool, _Record | None]:
    """(answered, record). A DOI Crossref does not know is answered with None."""
    hit, value = _cached(f"doi:{doi.lower()}")
    if hit:
        return True, value
    fetch = await limiter("CrossRef status").get(
        client, f"{CROSSREF_WORKS_URL}/{quote(doi, safe='/()')}", params={"mailto": MAILTO},
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT,
    )
    if fetch.kind == OK and fetch.response is not None:
        record = _record(fetch.response.json().get("message") or {})
        _store(f"doi:{doi.lower()}", record)
        return True, record
    if fetch.kind == NOT_FOUND:
        _store(f"doi:{doi.lower()}", None)
        return True, None
    return False, None


async def _crossref_title(client: httpx.AsyncClient, title: str) -> tuple[bool, list[_Record]]:
    key = f"title:{' '.join(_words(title))}"
    hit, value = _cached(key)
    if hit:
        return True, value
    fetch = await limiter("CrossRef status").get(
        client, CROSSREF_WORKS_URL,
        params={"query.bibliographic": title, "rows": str(TITLE_SEARCH_ROWS), "mailto": MAILTO},
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT,
    )
    if fetch.kind != OK or fetch.response is None:
        return False, []
    items = (fetch.response.json().get("message") or {}).get("items") or []
    records = [_record(item) for item in items if isinstance(item, dict)]
    _store(key, records)
    return True, records


async def _openalex_retracted(client: httpx.AsyncClient, doi: str) -> bool | None:
    params = {"select": "is_retracted"}
    if settings.OPENALEX_API_KEY.strip():
        params["api_key"] = settings.OPENALEX_API_KEY.strip()
    fetch = await limiter("OpenAlex status").get(
        client, f"{OPENALEX_WORKS_URL}/doi:{doi}", params=params, headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    if fetch.kind != OK or fetch.response is None:
        return None
    value = fetch.response.json().get("is_retracted")
    return value if isinstance(value, bool) else None


def _author_on_page(record: _Record, front_text: str) -> bool:
    page = set(_words(front_text))
    return any(len(name) >= 3 and name.lower() in page for name in record.authors)


def _decide(
    marks: list[FileMark], record: _Record | None, answered: bool, tried: bool, doi_source: str | None,
    openalex: bool | None,
) -> PublicationStatus:
    sources = ["file"] if marks else []
    notes: list[str] = []
    notices: list[IntegrityNotice] = []
    is_notice = False
    if record is not None:
        sources.append("Crossref")
        notices = list(record.notices)
        if record.updates:  # the uploaded file is the notice itself
            is_notice = True
            notices = list(record.updates)
    if openalex is not None:
        sources.append("OpenAlex")

    kinds = {n.type for n in notices}
    retracted = bool(kinds & RETRACTION_TYPES) or any(m.kind in ("retracted", "withdrawn") for m in marks)
    retracted = retracted or bool(record is not None and not is_notice and _RETRACTED_TITLE.match(record.title))
    concern = bool(kinds & CONCERN_TYPES) or any(m.kind == "expression_of_concern" for m in marks)

    if record is not None and not (kinds & RETRACTION_TYPES) and openalex is True:
        notes.append(
            "OpenAlex lists this work as retracted, but Crossref has no retraction notice for it. "
            "Check the article page on the publisher's site."
        )
    if marks and record is not None and not (kinds & (RETRACTION_TYPES | CONCERN_TYPES)):
        notes.append("The file is marked as retracted or flagged, but Crossref has no notice for it yet.")
    if record is not None and record.type in PREPRINT_TYPES:
        notes.append("A preprint with this title is posted; it has not been published in a journal under this record.")

    if retracted:
        status = "retracted"
    elif concern:
        status = "expression_of_concern"
    elif record is not None and record.type not in PREPRINT_TYPES and kinds & CORRECTION_TYPES:
        status = "corrected"
    elif record is not None and record.type not in PREPRINT_TYPES:
        status = "published"
    elif tried and not answered:
        status = "not_checked"
        notes.append("The publication record could not be checked (Crossref did not answer).")
    else:
        status = "not_found"

    published = record is not None and record.type not in PREPRINT_TYPES
    return PublicationStatus(
        status=status,  # type: ignore[arg-type]
        doi=record.doi if published else None,
        doi_source=doi_source if published else None,
        title=_plain_title(record.title) if published else None,
        journal=record.journal if published else None,
        journal_abbreviation=record.journal_abbreviation if published else None,
        publisher=record.publisher if published else None,
        year=record.year if published else None,
        notices=notices,
        file_marks=list(marks),
        is_notice=is_notice,
        sources=sources,
        notes=notes,
    )


async def _check(parsed: ParsedDocument, client: httpx.AsyncClient) -> PublicationStatus:
    marks = list(parsed.integrity_marks)
    front = parsed.front_text or parsed.full_text[:6000]
    record: _Record | None = None
    doi_source: str | None = None
    answered = False
    tried = False

    for doi in parsed.doi_candidates:
        tried = True
        ok, found = await _crossref_doi(client, doi)
        answered = answered or ok
        # a DOI in the file may be a cited or related work: it is this paper's only if the title is on page one
        if found is not None and (_title_on_page(found.title, front) or _similar(found.title, parsed.title) >= TITLE_SEARCH_SIMILARITY):
            record, doi_source = found, "file"
            break

    if record is None and len(parsed.title) >= MIN_SEARCH_TITLE_CHARS:
        tried = True
        ok, candidates = await _crossref_title(client, parsed.title)
        answered = answered or ok
        matches = [
            c for c in candidates
            if _similar(c.title, parsed.title) >= TITLE_SEARCH_SIMILARITY and _author_on_page(c, front)
        ]
        # a journal article wins over a preprint of the same paper
        matches.sort(key=lambda c: (c.type in PREPRINT_TYPES, -_similar(c.title, parsed.title)))
        if matches:
            record, doi_source = matches[0], "title_search"

    openalex: bool | None = None
    if record is not None and record.type not in PREPRINT_TYPES and not record.updates:
        openalex = await _openalex_retracted(client, record.doi)

    return _decide(marks, record, answered, tried, doi_source, openalex)


def same_journal(status: PublicationStatus, *names: str | None) -> bool:
    """Whether the journal the paper appeared in is the journal named (typed by the author or matched in OpenAlex)."""

    def key(text: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower().replace("&", "and"))

    own = {key(status.journal), key(status.journal_abbreviation)} - {""}
    return any(key(name) in own for name in names if key(name))


async def check_publication(parsed: ParsedDocument, client: httpx.AsyncClient | None = None) -> PublicationStatus:
    """The paper's publication status. Never raises; a failed lookup gives "not_checked" plus what the file shows."""
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        return await asyncio.wait_for(_check(parsed, http), TOTAL_BUDGET_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the analysis goes on without the record
        if not isinstance(exc, asyncio.TimeoutError):
            logger.warning("Publication status check failed", exc_info=True)
        status = _decide(list(parsed.integrity_marks), None, False, bool(parsed.doi_candidates or parsed.title), None, None)
        if status.status == "not_found":
            status.status = "not_checked"
        if "The publication record could not be checked (Crossref did not answer)." not in status.notes:
            status.notes.append("The publication record could not be checked in time.")
        return status
    finally:
        if own_client:
            await http.aclose()
