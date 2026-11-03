"""Queries on api_clients and api_client_secrets."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg


@dataclass(frozen=True)
class ClientState:
    id: uuid.UUID
    client_id: str
    name: str
    plan_id: uuid.UUID
    scopes: tuple[str, ...]
    status: str
    token_version: int


def _state(row: asyncpg.Record) -> ClientState:
    return ClientState(
        id=row["id"], client_id=row["client_id"], name=row["name"], plan_id=row["plan_id"],
        scopes=tuple(row["scopes"]), status=row["status"], token_version=row["token_version"],
    )


async def get_state(conn: asyncpg.Connection, client_uuid: uuid.UUID) -> ClientState | None:
    row = await conn.fetchrow(
        "SELECT id, client_id, name, plan_id, scopes, status, token_version FROM api_clients WHERE id = $1",
        client_uuid,
    )
    return _state(row) if row else None


async def authenticate(conn: asyncpg.Connection, client_id: str, secret_hash: bytes) -> ClientState | None:
    """Client for this id + secret if the secret is live and the client active."""
    row = await conn.fetchrow(
        """
        SELECT c.id, c.client_id, c.name, c.plan_id, c.scopes, c.status, c.token_version, s.id AS secret_id
        FROM api_client_secrets s JOIN api_clients c ON c.id = s.api_client_id
        WHERE s.secret_hash = $1 AND c.client_id = $2
          AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > now())
          AND c.status = 'active'
        """,
        secret_hash,
        client_id,
    )
    if row is None:
        return None
    await conn.execute("UPDATE api_client_secrets SET last_used_at = now() WHERE id = $1", row["secret_id"])
    await conn.execute("UPDATE api_clients SET last_used_at = now() WHERE id = $1", row["id"])
    return _state(row)


@dataclass(frozen=True)
class SecretInfo:
    id: uuid.UUID
    prefix: str
    last4: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


async def list_secrets(conn: asyncpg.Connection, client_uuid: uuid.UUID) -> list[SecretInfo]:
    rows = await conn.fetch(
        """
        SELECT id, prefix, last4, created_at, expires_at, revoked_at, last_used_at
        FROM api_client_secrets WHERE api_client_id = $1 ORDER BY created_at DESC
        """,
        client_uuid,
    )
    return [SecretInfo(**dict(r)) for r in rows]


async def active_secret_count(conn: asyncpg.Connection, client_uuid: uuid.UUID) -> int:
    return await conn.fetchval(
        """
        SELECT count(*) FROM api_client_secrets
        WHERE api_client_id = $1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
        """,
        client_uuid,
    )


async def add_secret(
    conn: asyncpg.Connection, client_uuid: uuid.UUID, secret_hash: bytes, prefix: str, last4: str,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    return await conn.fetchval(
        """
        INSERT INTO api_client_secrets (api_client_id, secret_hash, prefix, last4, created_by)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        client_uuid, secret_hash, prefix, last4, created_by,
    )
