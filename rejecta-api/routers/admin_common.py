"""Shared pieces for admin routes: DB error mapping, audit, reset links."""

import hashlib
import json
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from auth.dependencies import CurrentUser
from auth.errors import ApiError
from auth.request_meta import RequestMeta
from config import settings
from db.repos import audit

RESET_TOKEN_PREFIX = "pr_"
RESET_LINK_HOURS = 24


def escape_like(value: str) -> str:
    """Escape backslash, % and _ so user text is matched literally inside ILIKE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found.")


@contextmanager
def db_errors() -> Iterator[None]:
    """Turn integrity errors raised by database rules into clear 4xx responses."""
    try:
        yield
    except asyncpg.RaiseError as exc:  # rules from triggers (last admin, archive, default plan)
        raise ApiError(409, "rule_violation", exc.message) from exc
    except asyncpg.UniqueViolationError as exc:
        raise ApiError(409, "already_exists", "Something with this key or email already exists.") from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise ApiError(409, "in_use", "This item is still referenced and cannot be changed that way.") from exc
    except asyncpg.CheckViolationError as exc:
        if exc.constraint_name == "plans_require_default":
            raise ApiError(409, "rule_violation", "Exactly one active default plan is required.") from exc
        if exc.constraint_name == "plans_default_is_active":
            raise ApiError(409, "rule_violation", "The default plan cannot be archived.") from exc
        raise ApiError(400, "invalid_value", f"Invalid value ({exc.constraint_name}).") from exc


async def admin_audit(
    conn: asyncpg.Connection,
    current: CurrentUser,
    meta: RequestMeta,
    action: str,
    *,
    target_type: str,
    target_id: str | uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    await audit.record(
        conn,
        action=action,
        actor_id=current.id,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        ip=meta.ip,
        user_agent=meta.user_agent,
    )


def hash_reset_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


async def create_reset_link(
    conn: asyncpg.Connection, user_id: uuid.UUID, created_by: uuid.UUID
) -> tuple[str, datetime]:
    """One-time password (re)set link. Earlier unused links for the user stop working."""
    raw = RESET_TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=RESET_LINK_HOURS)
    await conn.execute(
        "UPDATE password_reset_tokens SET used_at = now() WHERE user_id = $1 AND used_at IS NULL", user_id
    )
    await conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token_hash, created_by, expires_at) VALUES ($1, $2, $3, $4)",
        user_id,
        hash_reset_token(raw),
        created_by,
        expires_at,
    )
    return f"{settings.APP_URL.rstrip('/')}/reset-password?token={raw}", expires_at


def jsonish(value: Any) -> Any:
    """asyncpg returns jsonb as text; decode for API responses."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
