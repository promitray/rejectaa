"""Append-only audit log (the API role can INSERT and SELECT, nothing else)."""

import ipaddress
import json
import uuid
from typing import Any

import asyncpg


async def record(
    conn: asyncpg.Connection,
    *,
    action: str,
    actor_type: str = "user",
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None,
    user_agent: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit_log
            (actor_type, actor_id, action, target_type, target_id, before, after, ip, user_agent)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
        """,
        actor_type,
        actor_id,
        action,
        target_type,
        str(target_id) if target_id is not None else None,
        json.dumps(before, default=str) if before is not None else None,
        json.dumps(after, default=str) if after is not None else None,
        ip,
        user_agent,
    )
