"""Tables of the literature knowledge base (local Postgres with pgvector, schema "literature").

works              one row per OpenAlex work (abstract_missing flag, author/institution ids, dates, references)
work_embeddings    one row per (work, embedding model); a vector column per vector size, HNSW index per model
journal_profiles   per (journal, model): size, year range, leave-one-out kNN statistics, low_confidence flag
journal_members    which works make up a journal's profile
retrieval_snapshots  per paper: kind "W" keyed by paper hash; kind "J" keyed by paper hash + target journal

The schema is created on first use (`ensure_schema`). It lives in the local RAG database only, never in the
production database (services.literature.store refuses Supabase and DATABASE_URL).
"""

from __future__ import annotations

from services.literature import EMBEDDING_DIMENSIONS

SCHEMA = "literature"


def _vector_column(dims: int) -> str:
    return f"embedding_{dims}"


def statements() -> list[str]:
    """The DDL, one statement each, idempotent."""
    sizes = sorted(set(EMBEDDING_DIMENSIONS.values()))
    vector_columns = ",\n    ".join(f"{_vector_column(d)} vector({d})" for d in sizes)
    out = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.works (
    id text PRIMARY KEY,
    doi text,
    arxiv_id text,
    title text NOT NULL,
    abstract text NOT NULL DEFAULT '',
    abstract_missing boolean NOT NULL DEFAULT true,
    publication_date date,
    year int,
    venue_id text,
    venue_name text,
    work_type text,
    author_ids text[] NOT NULL DEFAULT '{{}}',
    author_names text[] NOT NULL DEFAULT '{{}}',
    institution_ids text[] NOT NULL DEFAULT '{{}}',
    reference_ids text[] NOT NULL DEFAULT '{{}}',
    related_ids text[] NOT NULL DEFAULT '{{}}',
    concepts text[] NOT NULL DEFAULT '{{}}',
    cited_by_count int NOT NULL DEFAULT 0,
    source text NOT NULL DEFAULT 'openalex',
    fetched_at timestamptz NOT NULL DEFAULT now()
)""",
        f"CREATE INDEX IF NOT EXISTS works_doi ON {SCHEMA}.works (lower(doi))",
        f"CREATE INDEX IF NOT EXISTS works_venue_year ON {SCHEMA}.works (venue_id, year)",
        f"CREATE INDEX IF NOT EXISTS works_date ON {SCHEMA}.works (publication_date)",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.work_embeddings (
    work_id text NOT NULL REFERENCES {SCHEMA}.works(id) ON DELETE CASCADE,
    model text NOT NULL,
    dims int NOT NULL,
    text_hash text NOT NULL,
    title_only boolean NOT NULL,
    {vector_columns},
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (work_id, model)
)""",
    ]
    for model, dims in sorted(EMBEDDING_DIMENSIONS.items()):
        index = "work_emb_hnsw_" + "".join(c if c.isalnum() else "_" for c in model)
        out.append(
            f"CREATE INDEX IF NOT EXISTS {index} ON {SCHEMA}.work_embeddings "
            f"USING hnsw ({_vector_column(dims)} vector_cosine_ops) WHERE model = '{model}'"
        )
    out += [
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.journal_profiles (
    journal_key text NOT NULL,
    model text NOT NULL,
    source_id text,
    display_name text NOT NULL,
    tier text NOT NULL DEFAULT 'standard',
    n_papers int NOT NULL DEFAULT 0,
    year_from int,
    year_to int,
    low_confidence boolean NOT NULL DEFAULT true,
    stats jsonb NOT NULL DEFAULT '{{}}',
    built_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (journal_key, model)
)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.journal_members (
    journal_key text NOT NULL,
    work_id text NOT NULL REFERENCES {SCHEMA}.works(id) ON DELETE CASCADE,
    PRIMARY KEY (journal_key, work_id)
)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.retrieval_snapshots (
    kind text NOT NULL CHECK (kind IN ('W', 'J')),
    paper_hash text NOT NULL,
    journal_key text NOT NULL DEFAULT '',
    model text NOT NULL,
    schema_version text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, paper_hash, journal_key, model, schema_version)
)""",
    ]
    return out


def vector_column(model: str) -> str:
    """The column holding this model's vectors."""
    return _vector_column(EMBEDDING_DIMENSIONS[model])
