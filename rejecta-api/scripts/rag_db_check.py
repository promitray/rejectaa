"""Check the local RAG store: which backend is used, and for Postgres, the tables and their row counts.

    python -m scripts.rag_db_check
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from services.local_store import SCHEMA, get_store  # noqa: E402


async def main() -> int:
    store = get_store()
    print(f"store: {store.name}")
    if store.name != "postgres":
        print("RAG_DATABASE_URL is not set (or refused); the store uses", "files" if store.name == "files" else "memory")
        return 1
    host = settings.RAG_DATABASE_URL.rsplit("@", 1)[-1]
    pool = await store.backend._ready()  # creates schema "rag" and its tables when missing
    version = await pool.fetchval("SELECT version()")
    vector = await pool.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    print(f"connected: {host}\n{version.split(',')[0]}\npgvector: {vector or 'not installed'}")
    for table in ("kv", "embeddings", "paper_chunks"):
        count = await pool.fetchval(f"SELECT count(*) FROM {SCHEMA}.{table}")
        print(f"  {SCHEMA}.{table}: {count} rows")
    await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
