"""Local storage for the evidence ledger: model answers, embeddings and each paper's chunk index.

Three things are kept, all keyed by hashes of their input, so a lookup never depends on time or order:
- "llm":        the answer to one model request (temperature 0): the same paper gets the same report every run;
- embeddings:   the vector of one piece of text for one embedding model;
- paper chunks: a paper's paragraphs with their ids and vectors (the "vectorised paper"), by the file's hash.

Where: a LOCAL Postgres when RAG_DATABASE_URL is set (schema "rag", created on first use), otherwise JSON files
under RAG_CACHE_DIR. The production database (DATABASE_URL, Supabase) is never used: an address on supabase.co
or pooler.supabase.com is refused and the files are used instead. RAG_STORE=off keeps everything in memory.

Never raises: a store that fails is logged and the analysis runs without it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

SCHEMA = "rag"
_REFUSED_HOSTS = ("supabase.co", "supabase.com")
_API_ROOT = Path(__file__).resolve().parents[1]


def digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _backend_name() -> str:
    mode = (settings.RAG_STORE or "auto").strip().lower()
    if mode == "off":
        return "memory"
    url = (settings.RAG_DATABASE_URL or "").strip()
    if mode in ("auto", "postgres") and url:
        if any(host in url.lower() for host in _REFUSED_HOSTS):
            logger.error("RAG_DATABASE_URL points at Supabase; the local store uses files instead")
            return "files"
        if url == (settings.DATABASE_URL or "").strip():
            logger.error("RAG_DATABASE_URL is the production DATABASE_URL; the local store uses files instead")
            return "files"
        return "postgres"
    return "files"


MAX_MEMORY_ENTRIES = 4000  # per kind; the oldest go first


class _Memory:
    def __init__(self) -> None:
        self.kv: dict[tuple[str, str], Any] = {}
        self.vectors: dict[tuple[str, str], list[float]] = {}
        self.papers: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _trim(table: dict[Any, Any]) -> None:
        while len(table) > MAX_MEMORY_ENTRIES:
            table.pop(next(iter(table)))

    async def get(self, namespace: str, key: str) -> Any | None:
        return self.kv.get((namespace, key))

    async def put(self, namespace: str, key: str, value: Any) -> None:
        self.kv[(namespace, key)] = value
        self._trim(self.kv)

    async def get_vectors(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        return {h: self.vectors[(model, h)] for h in hashes if (model, h) in self.vectors}

    async def put_vectors(self, model: str, vectors: dict[str, list[float]]) -> None:
        for h, vec in vectors.items():
            self.vectors[(model, h)] = vec
        self._trim(self.vectors)

    async def put_paper(self, paper_key: str, chunks: list[dict[str, Any]]) -> None:
        self.papers[paper_key] = chunks


class _Files(_Memory):
    """One JSON file per entry: <dir>/<namespace>/<first two hex>/<key>.json. Old entries expire."""

    def __init__(self, root: Path, max_age_days: int) -> None:
        super().__init__()
        self.root = root
        self.max_age = max_age_days * 86400

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / key[:2] / f"{key}.json"

    def _read(self, path: Path) -> Any | None:
        try:
            if self.max_age and time.time() - path.stat().st_mtime > self.max_age:
                path.unlink(missing_ok=True)
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def get(self, namespace: str, key: str) -> Any | None:
        return await asyncio.to_thread(self._read, self._path(namespace, key))

    async def put(self, namespace: str, key: str, value: Any) -> None:
        await asyncio.to_thread(self._write, self._path(namespace, key), value)

    async def get_vectors(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for h in hashes:
            found = await self.get(f"emb-{_safe(model)}", h)
            if found is not None:
                out[h] = found
        return out

    async def put_vectors(self, model: str, vectors: dict[str, list[float]]) -> None:
        for h, vec in vectors.items():
            await self.put(f"emb-{_safe(model)}", h, vec)

    async def put_paper(self, paper_key: str, chunks: list[dict[str, Any]]) -> None:
        await self.put("papers", paper_key, chunks)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


class _Postgres:
    """The same operations on a local Postgres. Vectors are float4[] columns (pgvector is not required: a paper
    has a few hundred chunks and the similarity is computed in Python)."""

    def __init__(self, url: str, max_age_days: int) -> None:
        self.url = url
        self.max_age_days = max_age_days
        self.pool: Any = None
        self.lock = asyncio.Lock()

    async def _ready(self) -> Any:
        if self.pool is not None:
            return self.pool
        async with self.lock:
            if self.pool is None:
                import asyncpg

                from db.url import normalize_postgres_url

                pool = await asyncpg.create_pool(normalize_postgres_url(self.url), min_size=1, max_size=4, ssl=False)
                async with pool.acquire() as conn:
                    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
                    await conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {SCHEMA}.kv (namespace text NOT NULL, key text NOT NULL, "
                        "value jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (namespace, key))"
                    )
                    await conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {SCHEMA}.embeddings (model text NOT NULL, text_hash text NOT NULL, "
                        "vector float4[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (model, text_hash))"
                    )
                    await conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {SCHEMA}.paper_chunks (paper_key text NOT NULL, chunk_id text NOT NULL, "
                        "position int NOT NULL, section text NOT NULL, text text NOT NULL, text_hash text NOT NULL, "
                        "model text, vector float4[], created_at timestamptz NOT NULL DEFAULT now(), "
                        "PRIMARY KEY (paper_key, chunk_id))"
                    )
                    if self.max_age_days:
                        await conn.execute(
                            f"DELETE FROM {SCHEMA}.kv WHERE created_at < now() - make_interval(days => $1)", self.max_age_days
                        )
                self.pool = pool
        return self.pool

    async def get(self, namespace: str, key: str) -> Any | None:
        pool = await self._ready()
        row = await pool.fetchval(f"SELECT value FROM {SCHEMA}.kv WHERE namespace = $1 AND key = $2", namespace, key)
        return json.loads(row) if row is not None else None

    async def put(self, namespace: str, key: str, value: Any) -> None:
        pool = await self._ready()
        await pool.execute(
            f"INSERT INTO {SCHEMA}.kv (namespace, key, value) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, created_at = now()",
            namespace, key, json.dumps(value, ensure_ascii=False),
        )

    async def get_vectors(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        if not hashes:
            return {}
        pool = await self._ready()
        rows = await pool.fetch(
            f"SELECT text_hash, vector FROM {SCHEMA}.embeddings WHERE model = $1 AND text_hash = ANY($2::text[])", model, hashes
        )
        return {r["text_hash"]: list(r["vector"]) for r in rows}

    async def put_vectors(self, model: str, vectors: dict[str, list[float]]) -> None:
        if not vectors:
            return
        pool = await self._ready()
        await pool.executemany(
            f"INSERT INTO {SCHEMA}.embeddings (model, text_hash, vector) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            [(model, h, vec) for h, vec in vectors.items()],
        )

    async def put_paper(self, paper_key: str, chunks: list[dict[str, Any]]) -> None:
        pool = await self._ready()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f"DELETE FROM {SCHEMA}.paper_chunks WHERE paper_key = $1", paper_key)
            await conn.executemany(
                f"INSERT INTO {SCHEMA}.paper_chunks (paper_key, chunk_id, position, section, text, text_hash, model, vector) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                [
                    (paper_key, c["id"], c["position"], c["section"], c["text"], c["text_hash"], c.get("model"), c.get("vector"))
                    for c in chunks
                ],
            )


class Store:
    """The store the pipeline uses. Every method swallows its backend's errors (logged) and degrades to memory."""

    def __init__(self) -> None:
        self.name = _backend_name()
        if self.name == "postgres":
            self.backend: Any = _Postgres(settings.RAG_DATABASE_URL, settings.LLM_RESPONSE_CACHE_DAYS)
        elif self.name == "files":
            root = Path(settings.RAG_CACHE_DIR)
            self.backend = _Files(root if root.is_absolute() else _API_ROOT / root, settings.LLM_RESPONSE_CACHE_DAYS)
        else:
            self.backend = _Memory()
        self.memory = _Memory()

    async def _try(self, what: str, call: Any, default: Any) -> Any:
        try:
            return await call
        except Exception:  # noqa: BLE001 - the store is an optimisation, never a reason to fail an analysis
            logger.warning("Local store (%s) could not %s; using memory for this request", self.name, what, exc_info=True)
            return default

    async def get(self, namespace: str, key: str) -> Any | None:
        found = await self.memory.get(namespace, key)
        if found is None:
            found = await self._try("read", self.backend.get(namespace, key), None)
        return found

    async def put(self, namespace: str, key: str, value: Any) -> None:
        await self.memory.put(namespace, key, value)
        await self._try("write", self.backend.put(namespace, key, value), None)

    async def get_vectors(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        found = await self.memory.get_vectors(model, hashes)
        rest = [h for h in hashes if h not in found]
        if rest:
            found.update(await self._try("read vectors", self.backend.get_vectors(model, rest), {}))
        return found

    async def put_vectors(self, model: str, vectors: dict[str, list[float]]) -> None:
        await self.memory.put_vectors(model, vectors)
        await self._try("write vectors", self.backend.put_vectors(model, vectors), None)

    async def put_paper(self, paper_key: str, chunks: list[dict[str, Any]]) -> None:
        await self._try("write the paper index", self.backend.put_paper(paper_key, chunks), None)


_STORE: Store | None = None


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store()
    return _STORE


def reset_store() -> None:
    """Forget the store (tests, or after the settings change)."""
    global _STORE
    _STORE = None
