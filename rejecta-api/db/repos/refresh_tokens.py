"""Queries on app.refresh_tokens. Only SHA-256 hashes of tokens are stored."""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg


@dataclass(frozen=True)
class RefreshTokenRow:
    id: uuid.UUID
    user_id: uuid.UUID
    family_id: uuid.UUID
    token_version: int
    aal: int
    expires_at: datetime
    session_started_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> "RefreshTokenRow":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


_COLUMNS = "id, user_id, family_id, token_version, aal, expires_at, session_started_at, revoked_at, revoked_reason"


async def insert(
    conn: asyncpg.Connection,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    token_hash: bytes,
    token_version: int,
    aal: int,
    expires_at: datetime,
    session_started_at: datetime,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    user_agent: str | None,
) -> uuid.UUID:
    return await conn.fetchval(
        """
        INSERT INTO refresh_tokens
            (user_id, family_id, token_hash, token_version, aal, expires_at,
             session_started_at, ip, user_agent)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        user_id,
        family_id,
        token_hash,
        token_version,
        aal,
        expires_at,
        session_started_at,
        ip,
        user_agent,
    )


async def lock_by_hash(conn: asyncpg.Connection, token_hash: bytes) -> RefreshTokenRow | None:
    """Fetch and row-lock a token (call inside a transaction)."""
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM refresh_tokens WHERE token_hash = $1 FOR UPDATE", token_hash
    )
    return RefreshTokenRow.from_row(row) if row else None


async def mark_rotated(conn: asyncpg.Connection, token_id: uuid.UUID, replaced_by: uuid.UUID) -> None:
    await conn.execute(
        """
        UPDATE refresh_tokens
           SET revoked_at = now(), revoked_reason = 'rotated', replaced_by = $2, last_used_at = now()
         WHERE id = $1
        """,
        token_id,
        replaced_by,
    )


async def revoke(conn: asyncpg.Connection, token_id: uuid.UUID, reason: str) -> None:
    await conn.execute(
        "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = $2 WHERE id = $1 AND revoked_at IS NULL",
        token_id,
        reason,
    )


async def revoke_family(conn: asyncpg.Connection, family_id: uuid.UUID, reason: str) -> int:
    result = await conn.execute(
        "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = $2 "
        "WHERE family_id = $1 AND revoked_at IS NULL",
        family_id,
        reason,
    )
    return int(result.split()[-1])


async def revoke_all_for_user(conn: asyncpg.Connection, user_id: uuid.UUID, reason: str) -> int:
    result = await conn.execute(
        "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = $2 "
        "WHERE user_id = $1 AND revoked_at IS NULL",
        user_id,
        reason,
    )
    return int(result.split()[-1])


async def family_of(conn: asyncpg.Connection, token_hash: bytes) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(family_id, user_id) for a token hash, revoked or not."""
    row = await conn.fetchrow("SELECT family_id, user_id FROM refresh_tokens WHERE token_hash = $1", token_hash)
    return (row["family_id"], row["user_id"]) if row else None
