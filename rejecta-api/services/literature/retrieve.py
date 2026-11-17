"""P1: prior work for a manuscript, end to end, stored as its W snapshot.

    ms = manuscript.from_parsed(parsed, publication)          # services.literature.manuscript
    result = await retrieve_prior_work(ms, provider)            # -> dict (the W snapshot payload)

Steps:
1. the paper's date (services.literature.dates) and its authors resolved in OpenAlex (services.literature.authors);
2. candidates from the three routes, with exclusions and date classes (services.literature.candidates);
3. works stored and embedded with ONE model (cached by text; title-only works flagged);
4. hybrid ranking, then the LLM rerank of the top (services.literature.ranking);
5. prior-version labels (shared resolved author + similar; or very similar when authorship cannot be checked);
6. the top FINAL_K closest works and the coverage, stored as the W snapshot under the paper's hash and model.

The same paper gets the stored snapshot until `refresh=True` ("Run again"). Not wired into the analysis yet.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from services.literature import DEFAULT_EMBEDDING_MODEL, KB_SCHEMA_VERSION
from services.literature.authors import POSSIBLE_PRIOR, PRIOR_VERSION, prior_version_label, resolve_authors
from services.literature.candidates import Candidate, Options, Pool, gather
from services.literature.dates import resolve_paper_date
from services.literature.embeddings import embed, work_text
from services.literature.manuscript import Manuscript
from services.literature.openalex import OpenAlexClient
from services.literature.ranking import RERANK_PROMPT_VERSION, hybrid, rerank
from services.literature.types import PaperDate, Work
from services.local_store import digest

logger = logging.getLogger(__name__)

RETRIEVAL_VERSION = f"lit-p1-2026-11-16.2|{RERANK_PROMPT_VERSION}"
FINAL_K = 15
RELEVANT = 2  # relevance at or above this counts as relevant
LOW_COVERAGE = 10  # fewer relevant works than this: novelty must not be judged firmly


async def embed_pool(pool: Pool, ms: Manuscript, model: str, provider: Any, store: Any) -> tuple[list[float] | None, dict[str, list[float]]]:
    """Store the candidates, embed them (and the manuscript) with `model`. (query vector, work id -> vector)."""
    works = [c.work for c in pool.candidates.values()]
    if store is not None:
        await store.upsert_works(works)
    texts = [work_text(model, w.title, "" if w.abstract_missing else w.abstract) for w in works]
    vectors = await embed(model, [ms.query_text(), *texts], provider)
    query, rest = vectors[0], vectors[1:]
    by_id = {w.id: v for w, v in zip(works, rest) if v}
    if store is not None and by_id:
        known = await store.embedded_hashes(model, list(by_id))
        rows = [
            (w.id, digest(t), w.abstract_missing, by_id[w.id])
            for w, t in zip(works, texts) if w.id in by_id and known.get(w.id) != digest(t)
        ]
        await store.upsert_embeddings(model, rows)
    return query, by_id


async def rank_pool(
    pool: Pool, ms: Manuscript, model: str, provider: Any, store: Any = None, use_rerank: bool = True
) -> list[Candidate]:
    query, vectors = await embed_pool(pool, ms, model, provider, store)
    ordered = hybrid(list(pool.candidates.values()), ms, query, vectors)
    return await rerank(ms, ordered, provider) if use_rerank else ordered


def _entry(c: Candidate, rank: int) -> dict[str, Any]:
    w = c.work
    return {
        "rank": rank, "id": w.id, "title": w.title, "year": w.year,
        "publication_date": w.publication_date.isoformat() if w.publication_date else None,
        "venue": w.venue_name, "doi": w.doi, "abstract_missing": w.abstract_missing,
        "date_class": c.date_class, "label": c.label, "in_references": c.in_references,
        "routes": sorted(c.routes), "similarity": c.similarity, "keyword": c.keyword, "fused": c.fused,
        "co_cited": c.co_cited, "relevance": c.relevance, "relations": c.relations,
    }


async def retrieve_prior_work(
    ms: Manuscript,
    provider: Any,
    *,
    store: Any = None,
    client: OpenAlexClient | None = None,
    model: str = DEFAULT_EMBEDDING_MODEL,
    refresh: bool = False,
    options: Options | None = None,
    use_rerank: bool = True,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """The W snapshot payload for this manuscript (from the store unless `refresh`)."""
    if store is None:
        from services.literature.store import LiteratureStore

        store = LiteratureStore()
    if not refresh:
        found = await store.get_snapshot("W", ms.paper_hash, model)
        if found is not None:
            return found
    own_client = client is None
    client = client or OpenAlexClient()
    try:
        paper_date: PaperDate = resolve_paper_date(ms.venue_statements, ms.publication, ms.front_text, today=today)
        resolved = await resolve_authors(ms, client)

        async def local(before: dt.date) -> list[Work]:
            vector = (await embed(model, [ms.query_text()], provider))[0]
            if not vector:
                return []
            near = await store.nearest(model, vector, k=50, before=before)
            works = await store.get_works([i for i, _ in near])
            return [works[i] for i, _ in near if i in works]

        pool = await gather(ms, paper_date, client, options or Options(), local)
        ranked = await rank_pool(pool, ms, model, provider, store, use_rerank)
        for c in ranked:
            c.label = prior_version_label(c.work, ms, resolved, c.similarity)
    finally:
        if own_client:
            await client.close()

    top = ranked[:FINAL_K]
    # prior versions are always shown, wherever they rank: the editor needs them
    flagged = [c for c in ranked[FINAL_K:] if c.label in (PRIOR_VERSION, POSSIBLE_PRIOR)]
    shown = top + flagged
    relevant = [c for c in ranked if c.relevance is not None and c.relevance >= RELEVANT]
    payload: dict[str, Any] = {
        "version": RETRIEVAL_VERSION, "schema_version": KB_SCHEMA_VERSION, "model": model,
        "paper_date": {"date": paper_date.date.isoformat(), "source": paper_date.source,
                       "precision": paper_date.precision, "evidence": paper_date.evidence},
        "authors": [{"name": r.name, "openalex_id": r.openalex_id, "score": r.score, "evidence": r.evidence} for r in resolved],
        "anonymised": ms.anonymised,
        "queries": pool.queries,
        "works": [_entry(c, n) for n, c in enumerate(shown, start=1)],
        "coverage": {
            "candidates": len(pool.candidates), "relevant": len(relevant),
            "title_only_in_top": sum(1 for c in top if c.work.abstract_missing),
            "concurrent_in_top": sum(1 for c in top if c.date_class == "concurrent"),
            "excluded_self": pool.excluded_self, "dropped_later": pool.dropped_later,
            "dropped_undated": pool.dropped_undated, "low": len(relevant) < LOW_COVERAGE,
        },
    }
    await store.put_snapshot("W", ms.paper_hash, model, payload)
    return payload
