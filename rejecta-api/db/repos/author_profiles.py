"""Shared author-profile cache (one row per ORCID, public data only)."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg


@dataclass(frozen=True)
class CachedAuthor:
    status: str  # 'found' | 'not_found'
    display_name: str | None
    payload: dict[str, Any] | None
    fetched_at: datetime
    expires_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at > now


async def get_and_touch(conn: asyncpg.Connection, orcid: str) -> CachedAuthor | None:
    """Read a row and count the request in one statement."""
    row = await conn.fetchrow(
        """
        UPDATE author_profiles
           SET hit_count = hit_count + 1, last_requested_at = now()
         WHERE orcid = $1
        RETURNING status, display_name, payload, fetched_at, expires_at
        """,
        orcid,
    )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):  # asyncpg returns jsonb as text without a codec
        payload = json.loads(payload)
    return CachedAuthor(
        status=row["status"],
        display_name=row["display_name"],
        payload=payload,
        fetched_at=row["fetched_at"],
        expires_at=row["expires_at"],
    )


async def upsert(
    conn: asyncpg.Connection,
    *,
    orcid: str,
    status: str,
    display_name: str | None,
    payload: dict[str, Any] | None,
    ttl_seconds: float,
) -> None:
    await conn.execute(
        """
        INSERT INTO author_profiles (orcid, status, display_name, payload, fetched_at, expires_at, hit_count)
        VALUES ($1, $2, $3, $4::jsonb, now(), now() + make_interval(secs => $5::double precision), 1)
        ON CONFLICT (orcid) DO UPDATE
           SET status       = EXCLUDED.status,
               display_name = EXCLUDED.display_name,
               payload      = EXCLUDED.payload,
               fetched_at   = now(),
               expires_at   = EXCLUDED.expires_at
        """,
        orcid,
        status,
        display_name,
        json.dumps(payload) if payload is not None else None,
        ttl_seconds,
    )
