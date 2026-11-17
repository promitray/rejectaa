"""Candidate prior work for a manuscript, from three routes, with exclusions and date classes.

Routes:
  (a) the manuscript's references (by DOI), their OpenAlex related works, and the most cited works citing the first
      few references, all before the paper's date. Off in the evaluation (it would hand back the hidden references);
  (b) OpenAlex search: the title, the abstract's key words and the manuscript's claims, before the paper's date;
  (c) the local knowledge base: nearest works by this model's vectors, before the paper's date;
  (d) bibliographic coupling: the works that several of the closest search hits cite (the hits are before the
      paper's date, so they cannot cite it; nothing here comes from the manuscript's own reference list).

Every candidate then gets, by code:
  - excluded as the paper itself only on an exact DOI or arXiv id match (a near-identical title is a possible prior
    version, decided in services.literature.authors, never silently dropped);
  - a date class (services.literature.dates): prior or concurrent are kept; later and undated are dropped;
  - which routes found it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from services.journals import abstract_keywords
from services.literature.dates import classify_by_date
from services.literature.manuscript import Manuscript
from services.literature.openalex import OpenAlexClient
from services.literature.types import PaperDate, Work

SEARCH_PER_QUERY = 50
LOCAL_NEAREST = 50
MAX_REFERENCES = 120
MAX_RELATED = 150
CITING_REFERENCES = 5  # references whose citing works are read
CITING_PER_REFERENCE = 20
MAX_CLAIM_QUERIES = 3
COUPLING_HITS = 15  # closest search hits whose references are counted
COUPLING_MIN = 2  # cited by at least this many of them
MAX_COUPLED = 150
SEARCH_TYPES = "article|preprint|review"


@dataclass
class Options:
    use_paper_references: bool = True  # route (a)
    use_search: bool = True  # route (b)
    use_local: bool = True  # route (c)
    use_coupling: bool = True  # route (d)


@dataclass
class Candidate:
    work: Work
    routes: set[str] = field(default_factory=set)  # "references", "related", "citing", "search", "local"
    date_class: str = "prior"  # prior | concurrent
    in_references: bool = False  # the manuscript cites it
    label: str = ""  # prior_version_same_authors | same_authors | possible_prior_version | ""
    similarity: float = 0.0
    keyword: float = 0.0
    fused: float = 0.0
    co_cited: int = 0  # how many of the closest search hits cite it (route d)
    relevance: int | None = None  # 0-3 from the rerank
    relations: list[str] = field(default_factory=list)  # task / method / data_material / finding


@dataclass
class Pool:
    candidates: dict[str, Candidate] = field(default_factory=dict)
    excluded_self: list[str] = field(default_factory=list)
    dropped_later: int = 0
    dropped_undated: int = 0
    queries: list[str] = field(default_factory=list)


def is_self(work: Work, ms: Manuscript) -> bool:
    """The manuscript itself: an exact DOI or arXiv id match only."""
    if work.doi and work.doi.lower() in {d.lower() for d in ms.dois}:
        return True
    return bool(work.arxiv_id and work.arxiv_id in set(ms.arxiv_ids))


def search_queries(ms: Manuscript) -> list[str]:
    queries = [ms.title] if ms.title else []
    keywords = abstract_keywords(ms.abstract, limit=8) if ms.abstract else []
    if keywords:
        queries.append(" ".join(keywords))
    queries += [c for c in ms.claims[:MAX_CLAIM_QUERIES] if c]
    return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))


def _add(pool: Pool, works: list[Work], route: str, ms: Manuscript, paper_date: PaperDate, references: set[str]) -> None:
    for work in works:
        if not work.id:
            continue
        if is_self(work, ms):
            if work.id not in pool.excluded_self:
                pool.excluded_self.append(work.id)
            continue
        date_class = classify_by_date(work.publication_date, work.year, paper_date)
        if date_class == "later":
            pool.dropped_later += 1
            continue
        if date_class == "undated":
            pool.dropped_undated += 1
            continue
        cand = pool.candidates.get(work.id)
        if cand is None:
            cand = pool.candidates[work.id] = Candidate(work, date_class=date_class)
        cand.routes.add(route)
        cand.in_references = cand.in_references or work.id in references


async def gather(
    ms: Manuscript,
    paper_date: PaperDate,
    client: OpenAlexClient,
    options: Options,
    local_nearest: Any = None,
) -> Pool:
    """All candidates from the enabled routes. `local_nearest(before) -> list[Work]` runs route (c)."""
    pool = Pool()
    before = (paper_date.date - dt.timedelta(days=1)).isoformat()
    reference_ids: set[str] = set()

    if options.use_paper_references and ms.reference_dois:
        refs = await client.works_by_dois(ms.reference_dois[:MAX_REFERENCES])
        reference_ids = {w.id for w in refs}
        _add(pool, refs, "references", ms, paper_date, reference_ids)
        related = list(dict.fromkeys(r for w in refs for r in w.related_ids if r not in reference_ids))[:MAX_RELATED]
        _add(pool, await client.works_by_ids(related), "related", ms, paper_date, reference_ids)
        for ref in sorted(refs, key=lambda w: (-w.cited_by_count, w.id))[:CITING_REFERENCES]:
            _add(pool, await client.citing(ref.id, before, CITING_PER_REFERENCE), "citing", ms, paper_date, reference_ids)

    hits: list[Work] = []  # the search's closest works, in OpenAlex relevance order
    if options.use_search:
        pool.queries = search_queries(ms)
        filters = f"to_publication_date:{before},type:{SEARCH_TYPES}"
        for query in pool.queries:
            found = await client.search(query, filters, SEARCH_PER_QUERY)
            hits += [w for w in found[:COUPLING_HITS] if w.id not in {h.id for h in hits}]
            _add(pool, found, "search", ms, paper_date, reference_ids)
        if pool.queries:  # the most cited works on the topic: references are often a field's classics
            _add(pool, await client.search(pool.queries[0], filters, SEARCH_PER_QUERY, sort="cited_by_count:desc"),
                 "search_cited", ms, paper_date, reference_ids)

    if options.use_coupling and hits:
        counts: dict[str, int] = {}
        for hit in hits[:COUPLING_HITS * 2]:
            if is_self(hit, ms):
                continue
            for ref in set(hit.reference_ids):
                counts[ref] = counts.get(ref, 0) + 1
        coupled = sorted((i for i, n in counts.items() if n >= COUPLING_MIN), key=lambda i: (-counts[i], i))[:MAX_COUPLED]
        missing = [i for i in coupled if i not in pool.candidates]
        _add(pool, await client.works_by_ids(missing), "coupling", ms, paper_date, reference_ids)
        for i in coupled:
            if i in pool.candidates:
                pool.candidates[i].co_cited = counts[i]
                pool.candidates[i].routes.add("coupling")

    if options.use_local and local_nearest is not None:
        _add(pool, await local_nearest(paper_date.date), "local", ms, paper_date, reference_ids)
    return pool
