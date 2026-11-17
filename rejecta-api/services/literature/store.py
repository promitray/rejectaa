"""The literature store: works, their embeddings, journal profiles and per-paper retrieval snapshots.

Local Postgres with pgvector only (RAG_DATABASE_URL, the Docker database in docker/rag-db). An address on
Supabase, or the production DATABASE_URL, is refused. Unlike services.local_store there is no file fallback: a
vector index needs pgvector, so without the database the knowledge base is simply unavailable.

Snapshots make the report repeatable while the knowledge base grows: the works retrieved for a paper ("W", keyed
by the paper's hash) and the journal evidence for a paper and target journal ("J", keyed by paper hash + journal)
are stored once and reused until "Run again" asks for fresh ones.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from config import settings
from services.literature import EMBEDDING_DIMENSIONS, KB_SCHEMA_VERSION
from services.literature.schema import SCHEMA, statements, vector_column
from services.literature.types import Work

_REFUSED_HOSTS = ("supabase.co", "supabase.com")


class LiteratureStoreUnavailable(RuntimeError):
    """No usable local database: the knowledge base cannot run."""


def database_url() -> str:
    url = (settings.RAG_DATABASE_URL or "").strip()
    if not url:
        raise LiteratureStoreUnavailable("RAG_DATABASE_URL is not set (the local Docker database)")
    if any(host in url.lower() for host in _REFUSED_HOSTS) or url == (settings.DATABASE_URL or "").strip():
        raise LiteratureStoreUnavailable("RAG_DATABASE_URL must be the local database, not Supabase or DATABASE_URL")
    return url


def journal_key(name: str) -> str:
    """A journal's key: its name in lower case, letters and digits only, spaces kept single."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())).strip()


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


class LiteratureStore:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or database_url()
        self.pool: Any = None
        self._lock = asyncio.Lock()

    async def ready(self) -> Any:
        if self.pool is not None:
            return self.pool
        async with self._lock:
            if self.pool is None:
                import asyncpg

                from db.url import normalize_postgres_url

                pool = await asyncpg.create_pool(normalize_postgres_url(self.url), min_size=1, max_size=4, ssl=False)
                async with pool.acquire() as conn:
                    for sql in statements():
                        await conn.execute(sql)
                self.pool = pool
        return self.pool

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    # ---------------------------------------------------------------- works
    async def upsert_works(self, works: list[Work]) -> None:
        if not works:
            return
        pool = await self.ready()
        await pool.executemany(
            f"""INSERT INTO {SCHEMA}.works (id, doi, arxiv_id, title, abstract, abstract_missing, publication_date, year,
                venue_id, venue_name, work_type, author_ids, author_names, institution_ids, reference_ids, related_ids,
                concepts, cited_by_count)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (id) DO UPDATE SET doi = EXCLUDED.doi, title = EXCLUDED.title, abstract = EXCLUDED.abstract,
                abstract_missing = EXCLUDED.abstract_missing, publication_date = EXCLUDED.publication_date,
                year = EXCLUDED.year, venue_id = EXCLUDED.venue_id, venue_name = EXCLUDED.venue_name,
                author_ids = EXCLUDED.author_ids, author_names = EXCLUDED.author_names,
                institution_ids = EXCLUDED.institution_ids, reference_ids = EXCLUDED.reference_ids,
                related_ids = EXCLUDED.related_ids, concepts = EXCLUDED.concepts,
                cited_by_count = EXCLUDED.cited_by_count, fetched_at = now()""",
            [
                (w.id, w.doi, w.arxiv_id, w.title, w.abstract, w.abstract_missing, w.publication_date, w.year,
                 w.venue_id, w.venue_name, w.work_type, w.author_ids, w.author_names, w.institution_ids,
                 w.reference_ids, w.related_ids, w.concepts, w.cited_by_count)
                for w in works
            ],
        )

    async def get_works(self, ids: list[str]) -> dict[str, Work]:
        if not ids:
            return {}
        pool = await self.ready()
        rows = await pool.fetch(f"SELECT * FROM {SCHEMA}.works WHERE id = ANY($1::text[])", ids)
        out: dict[str, Work] = {}
        for r in rows:
            out[r["id"]] = Work(
                id=r["id"], title=r["title"], abstract=r["abstract"], abstract_missing=r["abstract_missing"],
                doi=r["doi"], arxiv_id=r["arxiv_id"], publication_date=r["publication_date"], year=r["year"],
                venue_id=r["venue_id"], venue_name=r["venue_name"], work_type=r["work_type"],
                author_ids=list(r["author_ids"]), author_names=list(r["author_names"]),
                institution_ids=list(r["institution_ids"]), reference_ids=list(r["reference_ids"]),
                related_ids=list(r["related_ids"]), concepts=list(r["concepts"]), cited_by_count=r["cited_by_count"],
            )
        return out

    # ---------------------------------------------------------------- embeddings
    async def upsert_embeddings(self, model: str, rows: list[tuple[str, str, bool, list[float]]]) -> None:
        """rows: (work_id, text_hash, title_only, vector). The vector must have the model's size."""
        if not rows:
            return
        dims = EMBEDDING_DIMENSIONS[model]
        column = vector_column(model)
        for _, _, _, vector in rows:
            if len(vector) != dims:
                raise ValueError(f"{model} vectors have {dims} values, got {len(vector)}")
        pool = await self.ready()
        await pool.executemany(
            f"""INSERT INTO {SCHEMA}.work_embeddings (work_id, model, dims, text_hash, title_only, {column})
            VALUES ($1, $2, $3, $4, $5, $6::vector)
            ON CONFLICT (work_id, model) DO UPDATE SET text_hash = EXCLUDED.text_hash,
                title_only = EXCLUDED.title_only, {column} = EXCLUDED.{column}, created_at = now()""",
            [(work_id, model, dims, text_hash, title_only, _vector_literal(vector)) for work_id, text_hash, title_only, vector in rows],
        )

    async def embedded_hashes(self, model: str, ids: list[str]) -> dict[str, str]:
        """work id -> text hash already embedded with this model (to skip unchanged works)."""
        if not ids:
            return {}
        pool = await self.ready()
        rows = await pool.fetch(
            f"SELECT work_id, text_hash FROM {SCHEMA}.work_embeddings WHERE model = $1 AND work_id = ANY($2::text[])", model, ids
        )
        return {r["work_id"]: r["text_hash"] for r in rows}

    async def nearest(
        self,
        model: str,
        vector: list[float],
        k: int = 50,
        before: Any = None,
        exclude_ids: list[str] | None = None,
        venue_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """(work id, cosine similarity) of the k nearest works with this model's vectors only. `before`: only works
        published before that date (undated works are left out). Ties go to the smaller id: same input, same order."""
        column = vector_column(model)
        pool = await self.ready()
        conditions = ["e.model = $1"]
        args: list[Any] = [model, _vector_literal(vector), k]
        if before is not None:
            args.append(before)
            conditions.append(f"w.publication_date < ${len(args)}")
        if exclude_ids:
            args.append(exclude_ids)
            conditions.append(f"NOT (w.id = ANY(${len(args)}::text[]))")
        if venue_id:
            args.append(venue_id)
            conditions.append(f"w.venue_id = ${len(args)}")
        rows = await pool.fetch(
            f"""SELECT w.id, 1 - (e.{column} <=> $2::vector) AS similarity
            FROM {SCHEMA}.work_embeddings e JOIN {SCHEMA}.works w ON w.id = e.work_id
            WHERE {' AND '.join(conditions)}
            ORDER BY e.{column} <=> $2::vector, w.id
            LIMIT $3""",
            *args,
        )
        return [(r["id"], round(float(r["similarity"]), 6)) for r in rows]

    # ---------------------------------------------------------------- snapshots
    async def get_snapshot(self, kind: str, paper_hash: str, model: str, journal: str = "") -> dict[str, Any] | None:
        pool = await self.ready()
        row = await pool.fetchval(
            f"""SELECT payload FROM {SCHEMA}.retrieval_snapshots WHERE kind = $1 AND paper_hash = $2 AND journal_key = $3
            AND model = $4 AND schema_version = $5""",
            kind, paper_hash, journal_key(journal) if kind == "J" else "", model, KB_SCHEMA_VERSION,
        )
        return json.loads(row) if row is not None else None

    async def put_snapshot(self, kind: str, paper_hash: str, model: str, payload: dict[str, Any], journal: str = "") -> None:
        """W: keyed by the paper's hash. J: keyed by the paper's hash and the target journal."""
        if kind not in ("W", "J"):
            raise ValueError("kind is W or J")
        if kind == "J" and not journal_key(journal):
            raise ValueError("a J snapshot needs the target journal")
        pool = await self.ready()
        await pool.execute(
            f"""INSERT INTO {SCHEMA}.retrieval_snapshots (kind, paper_hash, journal_key, model, schema_version, payload)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (kind, paper_hash, journal_key, model, schema_version)
            DO UPDATE SET payload = EXCLUDED.payload, created_at = now()""",
            kind, paper_hash, journal_key(journal) if kind == "J" else "", model, KB_SCHEMA_VERSION,
            json.dumps(payload, ensure_ascii=False),
        )

    # ---------------------------------------------------------------- journal profiles (filled in P3)
    async def put_journal_profile(self, key: str, model: str, profile: dict[str, Any], members: list[str]) -> None:
        pool = await self.ready()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.journal_profiles (journal_key, model, source_id, display_name, tier, n_papers,
                    year_from, year_to, low_confidence, stats)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                ON CONFLICT (journal_key, model) DO UPDATE SET source_id = EXCLUDED.source_id,
                    display_name = EXCLUDED.display_name, tier = EXCLUDED.tier, n_papers = EXCLUDED.n_papers,
                    year_from = EXCLUDED.year_from, year_to = EXCLUDED.year_to, low_confidence = EXCLUDED.low_confidence,
                    stats = EXCLUDED.stats, built_at = now()""",
                key, model, profile.get("source_id"), profile.get("display_name") or key, profile.get("tier") or "standard",
                int(profile.get("n_papers") or len(members)), profile.get("year_from"), profile.get("year_to"),
                bool(profile.get("low_confidence", True)), json.dumps(profile.get("stats") or {}),
            )
            await conn.execute(f"DELETE FROM {SCHEMA}.journal_members WHERE journal_key = $1", key)
            await conn.executemany(
                f"INSERT INTO {SCHEMA}.journal_members (journal_key, work_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                [(key, w) for w in members],
            )

    async def counts(self) -> dict[str, int]:
        pool = await self.ready()
        out: dict[str, int] = {}
        for table in ("works", "work_embeddings", "journal_profiles", "journal_members", "retrieval_snapshots"):
            out[table] = int(await pool.fetchval(f"SELECT count(*) FROM {SCHEMA}.{table}"))
        return out
