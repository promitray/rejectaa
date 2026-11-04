"""Queries on app.users."""

import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg

# Never selected into AuthState (the per-request cached record).
_AUTH_COLUMNS = "id, email, full_name, role, status, plan_id, token_version, mfa_enabled, created_at"
_FULL_COLUMNS = (
    _AUTH_COLUMNS
    + ", password_hash, failed_login_count, locked_until, last_login_at,"
    + " mfa_secret_enc, mfa_last_step, mfa_pending_secret_enc"
)


class EmailTakenError(Exception):
    """An account with this email already exists."""


@dataclass(frozen=True)
class AuthState:
    """What authorisation needs about a user, without secrets."""

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    status: str
    plan_id: uuid.UUID
    token_version: int
    mfa_enabled: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> "AuthState":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class UserRecord(AuthState):
    """Full user row for login, password and MFA flows. Contains secrets."""

    password_hash: str
    failed_login_count: int
    locked_until: datetime | None
    last_login_at: datetime | None
    mfa_secret_enc: str | None
    mfa_last_step: int | None
    mfa_pending_secret_enc: str | None

    @property
    def auth_state(self) -> AuthState:
        return AuthState(**{field: getattr(self, field) for field in AuthState.__dataclass_fields__})


async def get_by_email(conn: asyncpg.Connection, email: str) -> UserRecord | None:
    row = await conn.fetchrow(f"SELECT {_FULL_COLUMNS} FROM users WHERE email = lower(btrim($1))", email)
    return UserRecord.from_row(row) if row else None


async def get_by_id(conn: asyncpg.Connection, user_id: uuid.UUID) -> UserRecord | None:
    row = await conn.fetchrow(f"SELECT {_FULL_COLUMNS} FROM users WHERE id = $1", user_id)
    return UserRecord.from_row(row) if row else None


async def get_auth_state(conn: asyncpg.Connection, user_id: uuid.UUID) -> AuthState | None:
    row = await conn.fetchrow(f"SELECT {_AUTH_COLUMNS} FROM users WHERE id = $1", user_id)
    return AuthState.from_row(row) if row else None


ADMIN_PLAN_KEY = "lab"


async def admin_plan_id(conn: asyncpg.Connection) -> uuid.UUID | None:
    """The plan admins start on (Lab: unlimited), or None if it is missing or archived."""
    return await conn.fetchval("SELECT id FROM plans WHERE key = $1 AND status = 'active'", ADMIN_PLAN_KEY)


async def create_user(
    conn: asyncpg.Connection,
    *,
    email: str,
    password_hash: str,
    full_name: str,
    role: str = "user",
) -> UserRecord:
    """Insert a user.

    Admins start on the Lab plan; everyone else, and admins when Lab is
    unavailable, get the default plan (filled in by a database trigger).
    """
    plan_id = await admin_plan_id(conn) if role == "admin" else None
    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO users (email, password_hash, full_name, role, plan_id)
            VALUES (lower(btrim($1)), $2, $3, $4, $5)
            RETURNING {_FULL_COLUMNS}
            """,
            email,
            password_hash,
            full_name,
            role,
            plan_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise EmailTakenError(email) from exc
    assert row is not None
    return UserRecord.from_row(row)


async def record_login_failure(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    *,
    max_failures: int,
    lockout_minutes: int,
) -> datetime | None:
    """Count a failed login; lock the account after ``max_failures``.

    A lock that has already expired starts a fresh count. Returns the
    ``locked_until`` value now in force (or None).
    """
    return await conn.fetchval(
        """
        WITH calc AS (
            SELECT CASE WHEN locked_until IS NOT NULL AND locked_until <= now()
                        THEN 1 ELSE failed_login_count + 1 END AS failures
            FROM users WHERE id = $1
        )
        UPDATE users u
           SET failed_login_count = calc.failures,
               locked_until = CASE WHEN calc.failures >= $2
                                   THEN now() + make_interval(mins => $3)
                                   WHEN u.locked_until <= now() THEN NULL
                                   ELSE u.locked_until END
          FROM calc
         WHERE u.id = $1
        RETURNING u.locked_until
        """,
        user_id,
        max_failures,
        lockout_minutes,
    )


async def record_login_success(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = now() WHERE id = $1",
        user_id,
    )


async def update_password(conn: asyncpg.Connection, user_id: uuid.UUID, password_hash: str) -> int:
    """Set a new hash. The users trigger bumps and returns token_version."""
    return await conn.fetchval(
        """
        UPDATE users SET password_hash = $2, failed_login_count = 0, locked_until = NULL
        WHERE id = $1 RETURNING token_version
        """,
        user_id,
        password_hash,
    )


async def bump_token_version(conn: asyncpg.Connection, user_id: uuid.UUID) -> int:
    """Invalidate every access and refresh token issued to the user so far."""
    return await conn.fetchval(
        "UPDATE users SET token_version = token_version + 1 WHERE id = $1 RETURNING token_version",
        user_id,
    )


async def promote_to_admin(conn: asyncpg.Connection, user_id: uuid.UUID) -> None:
    """Make a user an admin and move them to the admin (Lab) plan if it exists."""
    await conn.execute(
        "UPDATE users SET role = 'admin', status = 'active', plan_id = COALESCE($2, plan_id) WHERE id = $1",
        user_id,
        await admin_plan_id(conn),
    )


@dataclass(frozen=True)
class PlanSummary:
    id: uuid.UUID
    key: str
    name: str


async def get_plan_summary(conn: asyncpg.Connection, plan_id: uuid.UUID) -> PlanSummary | None:
    row = await conn.fetchrow("SELECT id, key, name FROM plans WHERE id = $1", plan_id)
    return PlanSummary(row["id"], row["key"], row["name"]) if row else None
