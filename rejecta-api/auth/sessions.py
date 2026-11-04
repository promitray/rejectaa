"""Refresh-token sessions: issue, rotate (with reuse detection), revoke.

A *session* is a family of refresh tokens that starts at login. Each refresh
replaces the presented token with a new one (rotation). Presenting a token
that was already rotated means it was copied: the whole family is revoked
and the event is audited. One exception: within ROTATION_GRACE_SECONDS a
just-rotated token returns "superseded" without revoking anything, because
two browser tabs refreshing at the same moment is normal; the client simply
retries with the cookie the other tab received. No tokens are ever issued
for a superseded token.

A family stops being refreshable when the user's token_version changes
(password change, role/status change, logout-all) or when
SESSION_MAX_LIFETIME_DAYS have passed since login.

Database changes made while rejecting a token (revocations, audit rows) are
committed: the outcome is returned from the transaction, never raised in it.
"""

import enum
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from auth.request_meta import RequestMeta
from config import settings
from db.repos import audit, refresh_tokens, users
from db.repos.users import AuthState

REFRESH_TOKEN_PREFIX = "rt_"
REFRESH_TOKEN_MAX_LENGTH = 128
ROTATION_GRACE_SECONDS = 10


class RefreshFailure(enum.Enum):
    INVALID = "invalid_refresh_token"
    SUPERSEDED = "refresh_superseded"


@dataclass(frozen=True)
class IssuedSession:
    refresh_token: str
    session_id: uuid.UUID
    expires_at: datetime
    aal: int


@dataclass(frozen=True)
class RotationResult:
    user: AuthState | None
    session: IssuedSession | None
    failure: RefreshFailure | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_refresh_token() -> str:
    return REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_refresh_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def looks_like_refresh_token(raw: str | None) -> bool:
    return bool(raw) and raw.startswith(REFRESH_TOKEN_PREFIX) and len(raw) <= REFRESH_TOKEN_MAX_LENGTH


def _expiry(session_started_at: datetime, now: datetime) -> datetime:
    sliding = now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    absolute = session_started_at + timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS)
    return min(sliding, absolute)


async def start_session(
    conn: asyncpg.Connection,
    user: AuthState,
    *,
    aal: int,
    meta: RequestMeta,
) -> IssuedSession:
    """Create a new session (family) for a user who just authenticated."""
    now = _now()
    raw = new_refresh_token()
    family_id = uuid.uuid4()
    expires_at = _expiry(now, now)
    await refresh_tokens.insert(
        conn,
        user_id=user.id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw),
        token_version=user.token_version,
        aal=aal,
        expires_at=expires_at,
        session_started_at=now,
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
    return IssuedSession(raw, family_id, expires_at, aal)


async def rotate_session(conn: asyncpg.Connection, raw: str, meta: RequestMeta) -> RotationResult:
    """Exchange a refresh token for a new one. See module docstring."""
    if not looks_like_refresh_token(raw):
        return RotationResult(None, None, RefreshFailure.INVALID)

    async with conn.transaction():
        row = await refresh_tokens.lock_by_hash(conn, hash_refresh_token(raw))
        if row is None:
            return RotationResult(None, None, RefreshFailure.INVALID)

        now = _now()
        if row.revoked_at is not None:
            if row.revoked_reason == "rotated":
                if now - row.revoked_at <= timedelta(seconds=ROTATION_GRACE_SECONDS):
                    return RotationResult(None, None, RefreshFailure.SUPERSEDED)
                revoked = await refresh_tokens.revoke_family(conn, row.family_id, "reuse_detected")
                await audit.record(
                    conn,
                    action="auth.refresh_reuse_detected",
                    actor_type="system",
                    target_type="user",
                    target_id=row.user_id,
                    after={"family_id": str(row.family_id), "revoked_tokens": revoked},
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                )
            return RotationResult(None, None, RefreshFailure.INVALID)

        absolute_end = row.session_started_at + timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS)
        if row.expires_at <= now or absolute_end <= now:
            await refresh_tokens.revoke(conn, row.id, "expired")
            return RotationResult(None, None, RefreshFailure.INVALID)

        user = await users.get_auth_state(conn, row.user_id)
        if user is None or user.status != "active" or user.token_version != row.token_version:
            await refresh_tokens.revoke_family(conn, row.family_id, "token_version_changed")
            return RotationResult(None, None, RefreshFailure.INVALID)

        new_raw = new_refresh_token()
        expires_at = _expiry(row.session_started_at, now)
        new_id = await refresh_tokens.insert(
            conn,
            user_id=row.user_id,
            family_id=row.family_id,
            token_hash=hash_refresh_token(new_raw),
            token_version=row.token_version,
            aal=row.aal,
            expires_at=expires_at,
            session_started_at=row.session_started_at,
            ip=meta.ip,
            user_agent=meta.user_agent,
        )
        await refresh_tokens.mark_rotated(conn, row.id, new_id)
        return RotationResult(user, IssuedSession(new_raw, row.family_id, expires_at, row.aal), None)


async def end_session(conn: asyncpg.Connection, raw: str | None) -> uuid.UUID | None:
    """Revoke the session a refresh token belongs to. Returns its user id."""
    if not looks_like_refresh_token(raw):
        return None
    found = await refresh_tokens.family_of(conn, hash_refresh_token(raw))
    if found is None:
        return None
    family_id, user_id = found
    await refresh_tokens.revoke_family(conn, family_id, "logout")
    return user_id
