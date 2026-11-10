"""Journal matching with OpenAlex: find the journal the user named, how it ranks, and what it publishes."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from typing import Any

import httpx

from config import settings
from models import JournalMatch, RecentPaper

OPENALEX_BASE_URL = "https://api.openalex.org"
OPENALEX_USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 10.0
RECENT_PAPERS_COUNT = 5
SIMILAR_PAPERS_COUNT = 5
SOURCE_CANDIDATES = 10
SOURCE_SELECT = "id,display_name,alternate_titles,abbreviated_title,works_count,type,summary_stats"
SOURCE_CACHE_SECONDS = 24 * 3600.0
KEYWORDS_FOR_SIMILAR = 6
MIN_KEYWORD_CHARS = 5

logger = logging.getLogger(__name__)

_source_cache: dict[str, tuple[float, dict[str, Any] | None, bool]] = {}

_STOPWORDS = frozenset(
    "about above after again against among because before being below between both could during each "
    "further having however into itself might other ought ourselves rather should since still their "
    "theirs there these those through under until where which while within without would study paper "
    "results using based shown show shows report reported present presents novel approach method methods "
    "these this that with from have been were their than also such more most only over very well here".split()
)


def normalize_name(name: str) -> str:
    """A journal name reduced for comparison: "The Journal of Chem. & Phys." -> "journal of chem and phys"."""
    text = unicodedata.normalize("NFKC", name).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"^the ", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


# ISO 4 abbreviations used in journal names, with the words they stand for (most common first).
_ISO4: dict[str, tuple[str, ...]] = {
    "j": ("journal",), "am": ("american",), "chem": ("chemical", "chemistry", "chemie"), "soc": ("society",),
    "commun": ("communications",), "angew": ("angewandte",), "int": ("international",), "ed": ("edition",),
    "polym": ("polymer",), "nat": ("nature",), "mater": ("materials",), "phys": ("physical", "physics"),
    "lett": ("letters",), "rev": ("review", "reviews"), "sci": ("science", "sciences"), "org": ("organic",),
    "inorg": ("inorganic",), "macromol": ("macromolecular",), "appl": ("applied",), "biol": ("biology",),
    "proc": ("proceedings",), "natl": ("national",), "acad": ("academy",), "res": ("research",),
    "eng": ("engineering",), "environ": ("environmental",), "catal": ("catalysis",), "anal": ("analytical",),
    "med": ("medicinal", "medicine"), "biochem": ("biochemistry",), "eur": ("european",),
    "adv": ("advanced", "advances"), "funct": ("functional",), "mol": ("molecular",), "struct": ("structural",),
    "comput": ("computational",), "theor": ("theoretical",), "pharm": ("pharmaceutical",),
    "ind": ("industrial",), "electrochem": ("electrochemistry",), "sustain": ("sustainable",),
    "technol": ("technology",), "acc": ("accounts",), "trans": ("transactions",), "discuss": ("discussions",),
    "biomater": ("biomaterials",), "nanomater": ("nanomaterials",), "photochem": ("photochemistry",),
    "photobiol": ("photobiology",), "energy": ("energy",), "food": ("food",), "agric": ("agricultural",),
    "microbiol": ("microbiology",), "immunol": ("immunology",), "clin": ("clinical",), "rep": ("reports",),
}
_WORD_TO_ABBREVIATION = {word: abbr for abbr, words in _ISO4.items() for word in words}
_NAME_STOPWORDS = frozenset({"of", "the", "and", "for", "in", "on", "de", "a"})


def abbreviation_key(name: str) -> tuple[str, ...]:
    """A name reduced to its ISO 4 form, so "Chem. Commun." and "Chemical Communications" compare equal."""
    words = [w for w in normalize_name(name).split() if w not in _NAME_STOPWORDS]
    return tuple(_WORD_TO_ABBREVIATION.get(w, w) for w in words)


def looks_abbreviated(name: str) -> bool:
    words = normalize_name(name).split()
    return "." in name and any(w in _ISO4 for w in words)


def expansions(name: str, limit: int = 2) -> list[str]:
    """Full-word versions of an abbreviated name to search with ("Chem. Commun." -> "chemical communications")."""
    words = normalize_name(name).split()
    out: list[str] = []
    for choice in range(limit):
        expanded = " ".join(_ISO4[w][min(choice, len(_ISO4[w]) - 1)] if w in _ISO4 else w for w in words)
        if expanded not in out:
            out.append(expanded)
    # Last resort: only the words with one meaning ("Angew. Chem. Int. Ed." is "Angewandte Chemie ...").
    certain = " ".join(_ISO4[w][0] if w in _ISO4 else w for w in words if len(_ISO4.get(w, (w,))) == 1)
    if certain and certain not in out:
        out.append(certain)
    return out


def _names(source: dict[str, Any]) -> list[str]:
    names = [source.get("display_name"), source.get("abbreviated_title"), *(source.get("alternate_titles") or [])]
    return [str(n) for n in names if n]


def _tokens(text: str) -> set[str]:
    return set(normalize_name(text).split())


def pick_source(query: str, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """(the source the user most likely means, whether its name matches exactly).

    An exact match on the name, abbreviation or an alternate title wins, the largest journal first if
    several match ("Polymer Chemistry" must not become "Journal of Polymer Science Part A Polymer
    Chemistry", which merely ranks first in the search). Otherwise the closest name, then size, decides,
    and the match is reported as approximate.
    """
    if not candidates:
        return None, False
    wanted = normalize_name(query)
    compact = wanted.replace(" ", "")
    wanted_key = abbreviation_key(query)

    def same(name: object) -> bool:
        norm = normalize_name(str(name or ""))
        if not norm:
            return False
        return norm == wanted or norm.replace(" ", "") == compact or abbreviation_key(norm) == wanted_key

    # The journal's own name beats an abbreviation, which beats an alternate title: "Polymer Chemistry" is
    # also an alternate title of "Journal of Polymer Science Part A: Polymer Chemistry".
    for names_of in (
        lambda s: [s.get("display_name")],
        lambda s: [s.get("abbreviated_title")],
        lambda s: s.get("alternate_titles") or [],
    ):
        exact = [s for s in candidates if any(same(n) for n in names_of(s))]
        if exact:
            return max(exact, key=lambda s: s.get("works_count") or 0), True
    query_tokens = _tokens(query)

    def closeness(source: dict[str, Any]) -> tuple[float, int]:
        best = 0.0
        for name in _names(source):
            tokens = _tokens(name)
            if tokens and query_tokens:
                best = max(best, len(tokens & query_tokens) / len(tokens | query_tokens))
        return best, source.get("works_count") or 0

    journals = [s for s in candidates if s.get("type") in (None, "journal")] or candidates
    return max(journals, key=closeness), False


def abstract_keywords(abstract: str, limit: int = KEYWORDS_FOR_SIMILAR) -> list[str]:
    """The most frequent content words of the abstract, for finding similar papers."""
    words = [w for w in re.findall(r"[a-z][a-z\-]+", abstract.lower()) if len(w) >= MIN_KEYWORD_CHARS and w not in _STOPWORDS]
    return [word for word, _ in Counter(words).most_common(limit)]


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    params = dict(extra)
    if settings.OPENALEX_API_KEY.strip():
        params["api_key"] = settings.OPENALEX_API_KEY.strip()
    return params


def _papers(payload: dict[str, Any]) -> list[RecentPaper]:
    papers: list[RecentPaper] = []
    seen: set[str] = set()
    for work in payload.get("results") or []:
        title = str(work.get("title") or "")
        if not title or normalize_name(title) in seen:  # OpenAlex lists some articles twice
            continue
        seen.add(normalize_name(title))
        year = work.get("publication_year")
        papers.append(RecentPaper(title=title, year=year if isinstance(year, int) else None))
    return papers


async def _find_source(client: httpx.AsyncClient, target_journal: str) -> tuple[dict[str, Any] | None, bool]:
    key = normalize_name(target_journal)
    cached = _source_cache.get(key)
    if cached and time.monotonic() - cached[0] < SOURCE_CACHE_SECONDS:
        return cached[1], cached[2]

    async def search(query: str) -> list[dict[str, Any]]:
        response = await client.get(
            f"{OPENALEX_BASE_URL}/sources",
            params=_params({"search": query, "per_page": SOURCE_CANDIDATES, "select": SOURCE_SELECT}),
        )
        response.raise_for_status()
        return response.json().get("results") or []

    candidates = await search(target_journal)
    source, exact = pick_source(target_journal, candidates)
    if not exact and looks_abbreviated(target_journal):
        # OpenAlex search does not know "Chem. Commun."; search with the words it stands for.
        for expanded in expansions(target_journal):
            candidates += await search(expanded)
            source, exact = pick_source(target_journal, candidates)
            if exact:
                break
    _source_cache[key] = (time.monotonic(), source, exact)
    return source, exact


async def _works(client: httpx.AsyncClient, source_id: str, extra: dict[str, Any]) -> list[RecentPaper]:
    """Research articles of the journal (no news, editorials or issue covers). Never raises."""
    try:
        params = {
            "filter": f"primary_location.source.id:{source_id},type:article,has_abstract:true",
            "select": "title,publication_year",
            **extra,
        }
        response = await client.get(f"{OPENALEX_BASE_URL}/works", params=_params(params))
        response.raise_for_status()
        return _papers(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OpenAlex works lookup failed for %s: %s", source_id, exc)
        return []


async def match_journal(abstract: str, target_journal: str) -> JournalMatch:
    """Find a journal in OpenAlex and return its metadata, recent articles and articles like this paper.

    Args:
        abstract: Paper abstract, used to find the journal's articles closest to this paper.
        target_journal: Journal name as entered by the user.

    Returns:
        JournalMatch. found=False if no journal can be located.
    """
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": OPENALEX_USER_AGENT}) as client:
            source, exact = await _find_source(client, target_journal)
            if source is None:
                logger.warning("Journal not found in OpenAlex: %s", target_journal)
                return JournalMatch(found=False, target=target_journal)

            source_id = str(source["id"]).split("/")[-1]
            display_name = source.get("display_name")
            recent = await _works(client, source_id, {"per_page": RECENT_PAPERS_COUNT, "sort": "publication_date:desc"})
            keywords = abstract_keywords(abstract)
            similar = (
                await _works(client, source_id, {"per_page": SIMILAR_PAPERS_COUNT, "search": " OR ".join(keywords)})
                if keywords
                else []
            )
            stats = source.get("summary_stats") or {}
            citedness = stats.get("2yr_mean_citedness")
            note = None
            if not exact:
                note = (
                    f'No journal is named exactly "{target_journal}"; the closest in OpenAlex is "{display_name}". '
                    "It may not be the journal you meant: enter the journal's full name for a better fit assessment."
                )
            return JournalMatch(
                found=True,
                target=target_journal,
                openalex_name=display_name,
                works_count=source.get("works_count"),
                recent_papers=recent,
                similar_papers=similar,
                match_quality="exact" if exact else "approximate",
                note=note,
                two_year_mean_citedness=round(float(citedness), 2) if isinstance(citedness, (int, float)) else None,
                h_index=stats.get("h_index") if isinstance(stats.get("h_index"), int) else None,
            )
    except httpx.TimeoutException as exc:
        logger.warning("OpenAlex timeout while matching journal %s: %s", target_journal, exc)
    except httpx.HTTPError as exc:
        logger.warning("OpenAlex request error while matching journal %s: %s", target_journal, exc)
    except Exception:
        logger.warning("OpenAlex lookup failed for journal %s; continuing without a match", target_journal, exc_info=True)
    return JournalMatch(found=False, target=target_journal)
