"""Queries for admin TOTP enrollment, verification and recovery codes."""

import uuid

import asyncpg


async def set_pending_secret(conn: asyncpg.Connection, user_id: uuid.UUID, secret_enc: str) -> None:
    """Store (or replace) the not-yet-confirmed secret. Does not touch token_version."""
    await conn.execute(
        """
        UPDATE users SET mfa_pending_secret_enc = $2, mfa_pending_created_at = now()
        WHERE id = $1 AND NOT mfa_enabled
        """,
        user_id,
        secret_enc,
    )


async def activate(conn: asyncpg.Connection, user_id: uuid.UUID, step: int) -> int | None:
    """Promote the pending secret to the live one. Returns the new token_version.

    The step used to confirm is recorded so the same code cannot be replayed
    for the first login. None if there was no pending secret (raced/stale).
    """
    return await conn.fetchval(
        """
        UPDATE users
           SET mfa_secret_enc = mfa_pending_secret_enc,
               mfa_enabled = true,
               mfa_last_step = $2,
               mfa_pending_secret_enc = NULL,
               mfa_pending_created_at = NULL
         WHERE id = $1 AND NOT mfa_enabled AND mfa_pending_secret_enc IS NOT NULL
        RETURNING token_version
        """,
        user_id,
        step,
    )


async def claim_step(conn: asyncpg.Connection, user_id: uuid.UUID, step: int) -> bool:
    """Atomically accept a TOTP step only if it is newer than the last one used."""
    return bool(
        await conn.fetchval(
            """
            UPDATE users SET mfa_last_step = $2
            WHERE id = $1 AND mfa_enabled AND (mfa_last_step IS NULL OR mfa_last_step < $2)
            RETURNING true
            """,
            user_id,
            step,
        )
    )


async def replace_recovery_codes(conn: asyncpg.Connection, user_id: uuid.UUID, hashes: list[bytes]) -> None:
    await conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = $1", user_id)
    await conn.executemany(
        "INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES ($1, $2)",
        [(user_id, code_hash) for code_hash in hashes],
    )


async def use_recovery_code(conn: asyncpg.Connection, user_id: uuid.UUID, code_hash: bytes) -> bool:
    """Mark a recovery code used. False if unknown or already used."""
    return bool(
        await conn.fetchval(
            """
            UPDATE mfa_recovery_codes SET used_at = now()
            WHERE user_id = $1 AND code_hash = $2 AND used_at IS NULL
            RETURNING true
            """,
            user_id,
            code_hash,
        )
    )


async def unused_recovery_codes(conn: asyncpg.Connection, user_id: uuid.UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM mfa_recovery_codes WHERE user_id = $1 AND used_at IS NULL", user_id
    )


async def reset(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    """Remove MFA completely (bumps token_version via trigger). Next login re-enrolls."""
    await conn.execute(
        """
        UPDATE users
           SET mfa_enabled = false, mfa_secret_enc = NULL, mfa_last_step = NULL,
               mfa_pending_secret_enc = NULL, mfa_pending_created_at = NULL
         WHERE id = $1
        """,
        user_id,
    )
    await conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = $1", user_id)
