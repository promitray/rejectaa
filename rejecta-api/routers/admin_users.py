"""Admin: manage users, their plans, per-user overrides, sessions and MFA.

Every route requires an admin with a two-factor session (require_admin) and
writes an audit entry. Admins never see or set passwords: new accounts and
resets get a one-time link the admin passes on.
"""

import secrets
import uuid
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Query, Request, Response, status

from auth import entitlements, user_cache
from auth.dependencies import CurrentUser, require_admin
from auth.errors import ApiError
from auth.passwords import hash_password
from auth.request_meta import request_meta
from db.pool import get_pool
from db.repos import mfa as mfa_repo
from db.repos import refresh_tokens, users
from routers.admin_common import admin_audit, create_reset_link, db_errors, escape_like, not_found
from routers.admin_schemas import (
    AdminUser,
    AdminUserCreate,
    AdminUserCreated,
    AdminUserDetail,
    AdminUserList,
    AdminUserUpdate,
    OverrideUpdate,
    PasswordLink,
    PlanRef,
    UsageReset,
    UserOverride,
)
from routers.me import feature_usage

router = APIRouter(prefix="/admin/users", tags=["admin"])

_USER_SELECT = """
    SELECT u.id, u.email, u.full_name, u.role, u.status, u.mfa_enabled, u.created_at, u.last_login_at,
           (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,
           p.id AS plan_id, p.key AS plan_key, p.name AS plan_name
    FROM users u JOIN plans p ON p.id = u.plan_id
"""


def _user(row: asyncpg.Record) -> AdminUser:
    return AdminUser(
        id=row["id"],
        email=row["email"],
        full_name=row["full_name"],
        role=row["role"],
        status=row["status"],
        plan=PlanRef(id=row["plan_id"], key=row["plan_key"], name=row["plan_name"]),
        mfa_enabled=row["mfa_enabled"],
        locked=row["locked"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


async def _fetch_user(conn: asyncpg.Connection, user_id: uuid.UUID) -> AdminUser:
    row = await conn.fetchrow(_USER_SELECT + " WHERE u.id = $1", user_id)
    if row is None:
        raise not_found("User")
    return _user(row)


async def _require_active_plan(conn: asyncpg.Connection, plan_id: uuid.UUID) -> None:
    plan_status = await conn.fetchval("SELECT status FROM plans WHERE id = $1", plan_id)
    if plan_status is None:
        raise not_found("Plan")
    if plan_status != "active":
        raise ApiError(400, "plan_archived", "Users cannot be put on an archived plan.")


@router.get("", response_model=AdminUserList)
async def list_users(
    search: str | None = Query(default=None, max_length=120),
    role: Literal["admin", "user"] | None = None,
    user_status: Literal["active", "suspended"] | None = Query(default=None, alias="status"),
    plan_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: CurrentUser = Depends(require_admin),
) -> AdminUserList:
    pattern = f"%{escape_like(search.strip())}%" if search and search.strip() else None
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            _USER_SELECT
            + """
            WHERE ($1::text IS NULL OR u.email ILIKE $1 OR u.full_name ILIKE $1)
              AND ($2::text IS NULL OR u.role = $2)
              AND ($3::text IS NULL OR u.status = $3)
              AND ($4::uuid IS NULL OR u.plan_id = $4)
            ORDER BY u.created_at DESC
            LIMIT $5 OFFSET $6
            """,
            pattern, role, user_status, plan_id, page_size, (page - 1) * page_size,
        )
        total = await conn.fetchval(
            """
            SELECT count(*) FROM users u
            WHERE ($1::text IS NULL OR u.email ILIKE $1 OR u.full_name ILIKE $1)
              AND ($2::text IS NULL OR u.role = $2)
              AND ($3::text IS NULL OR u.status = $3)
              AND ($4::uuid IS NULL OR u.plan_id = $4)
            """,
            pattern, role, user_status, plan_id,
        )
    return AdminUserList(items=[_user(r) for r in rows], total=total, page=page, page_size=page_size)


@router.post("", response_model=AdminUserCreated, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: AdminUserCreate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminUserCreated:
    """Create an account. The person sets their own password via the returned link."""
    meta = request_meta(request)
    unusable = await hash_password(secrets.token_urlsafe(32))  # nobody knows it
    async with get_pool().acquire() as conn:
        with db_errors():
            async with conn.transaction():
                if body.plan_id is not None:
                    await _require_active_plan(conn, body.plan_id)
                try:
                    created = await users.create_user(
                        conn, email=body.email, password_hash=unusable, full_name=body.full_name.strip(), role=body.role
                    )
                except users.EmailTakenError as exc:
                    raise ApiError(409, "email_taken", "An account with this email already exists.") from exc
                if body.plan_id is not None:
                    await conn.execute("UPDATE users SET plan_id = $2 WHERE id = $1", created.id, body.plan_id)
                url, expires_at = await create_reset_link(conn, created.id, current.id)
                await admin_audit(conn, current, meta, "admin.user_created", target_type="user", target_id=created.id,
                                  after={"email": created.email, "role": body.role, "plan_id": body.plan_id})
        user = await _fetch_user(conn, created.id)
    return AdminUserCreated(user=user, password_setup=PasswordLink(url=url, expires_at=expires_at))


@router.get("/{user_id}", response_model=AdminUserDetail)
async def get_user(user_id: uuid.UUID, _: CurrentUser = Depends(require_admin)) -> AdminUserDetail:
    async with get_pool().acquire() as conn:
        user = await _fetch_user(conn, user_id)
        features = await feature_usage(
            conn, subject_type="user", subject_id=user.id, plan_id=user.plan.id, user_id=user.id
        )
        overrides = await conn.fetch(
            """
            SELECT feature_key, enabled, daily_limit_set, daily_limit, monthly_limit_set, monthly_limit,
                   expires_at, reason, updated_at
            FROM user_overrides WHERE user_id = $1 ORDER BY feature_key
            """,
            user_id,
        )
        sessions = await conn.fetchval(
            "SELECT count(*) FROM refresh_tokens WHERE user_id = $1 AND revoked_at IS NULL AND expires_at > now()",
            user_id,
        )
        codes_left = await mfa_repo.unused_recovery_codes(conn, user_id) if user.mfa_enabled else None
    return AdminUserDetail(
        **user.model_dump(),
        features=features,
        overrides=[UserOverride(**dict(r)) for r in overrides],
        active_sessions=sessions,
        recovery_codes_left=codes_left,
    )


@router.patch("/{user_id}", response_model=AdminUser)
async def update_user(
    user_id: uuid.UUID, body: AdminUserUpdate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminUser:
    """Change name, role, status or plan. Role/status changes sign the user out."""
    meta = request_meta(request)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(400, "nothing_to_change", "No changes given.")
    if user_id == current.id and ("role" in changes or "status" in changes):
        raise ApiError(409, "self_change", "You cannot change your own role or status.")
    async with get_pool().acquire() as conn:
        before = await _fetch_user(conn, user_id)
        new_plan_id = body.plan_id
        if body.role == "admin" and before.role != "admin" and new_plan_id is None:
            # Promoting to admin moves the user to the admin plan (Lab) unless a plan was chosen.
            new_plan_id = await users.admin_plan_id(conn)
            if new_plan_id is not None:
                changes["plan_id"] = new_plan_id
        with db_errors():
            async with conn.transaction():
                if "plan_id" in changes:
                    await _require_active_plan(conn, changes["plan_id"])
                await conn.execute(
                    """
                    UPDATE users SET full_name = COALESCE($2, full_name), role = COALESCE($3, role),
                                     status = COALESCE($4, status), plan_id = COALESCE($5, plan_id)
                    WHERE id = $1
                    """,
                    user_id, body.full_name, body.role, body.status, new_plan_id,
                )
                if "role" in changes or "status" in changes:
                    await refresh_tokens.revoke_all_for_user(conn, user_id, "admin_changed_access")
                await admin_audit(conn, current, meta, "admin.user_updated", target_type="user", target_id=user_id,
                                  before=before.model_dump(include=set(changes) | {"plan"}), after=changes)
        user_cache.invalidate(user_id)
        return await _fetch_user(conn, user_id)


@router.post("/{user_id}/password-reset-link", response_model=PasswordLink)
async def password_reset_link(
    user_id: uuid.UUID, request: Request, response: Response, current: CurrentUser = Depends(require_admin)
) -> PasswordLink:
    """One-time link (24 h) for the user to choose a new password. Shown once."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        await _fetch_user(conn, user_id)
        async with conn.transaction():
            url, expires_at = await create_reset_link(conn, user_id, current.id)
            await admin_audit(conn, current, meta, "admin.password_reset_link", target_type="user", target_id=user_id)
    response.headers["Cache-Control"] = "no-store"
    return PasswordLink(url=url, expires_at=expires_at)


@router.post("/{user_id}/revoke-sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_sessions(user_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_admin)) -> Response:
    """Sign the user out everywhere (refresh and access tokens)."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        await _fetch_user(conn, user_id)
        async with conn.transaction():
            revoked = await refresh_tokens.revoke_all_for_user(conn, user_id, "admin_revoked")
            await users.bump_token_version(conn, user_id)
            await admin_audit(conn, current, meta, "admin.sessions_revoked", target_type="user", target_id=user_id,
                              after={"revoked_sessions": revoked})
    user_cache.invalidate(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{user_id}/mfa/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_mfa(user_id: uuid.UUID, request: Request, current: CurrentUser = Depends(require_admin)) -> Response:
    """Remove a user's two-factor setup (lost phone). They re-enroll at next login."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        await _fetch_user(conn, user_id)
        async with conn.transaction():
            await mfa_repo.reset(conn, user_id)
            await refresh_tokens.revoke_all_for_user(conn, user_id, "mfa_reset")
            await admin_audit(conn, current, meta, "admin.mfa_reset", target_type="user", target_id=user_id)
    user_cache.invalidate(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{user_id}/overrides/{feature_key}", response_model=UserOverride)
async def set_override(
    user_id: uuid.UUID,
    feature_key: str,
    body: OverrideUpdate,
    request: Request,
    current: CurrentUser = Depends(require_admin),
) -> UserOverride:
    """Give one user a different on/off or limit for one feature than their plan."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        await _fetch_user(conn, user_id)
        if not await conn.fetchval("SELECT 1 FROM features WHERE key = $1", feature_key):
            raise not_found("Feature")
        with db_errors():
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_overrides
                        (user_id, feature_key, enabled, daily_limit_set, daily_limit,
                         monthly_limit_set, monthly_limit, expires_at, reason, set_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (user_id, feature_key) DO UPDATE SET
                        enabled = EXCLUDED.enabled, daily_limit_set = EXCLUDED.daily_limit_set,
                        daily_limit = EXCLUDED.daily_limit, monthly_limit_set = EXCLUDED.monthly_limit_set,
                        monthly_limit = EXCLUDED.monthly_limit, expires_at = EXCLUDED.expires_at,
                        reason = EXCLUDED.reason, set_by = EXCLUDED.set_by
                    RETURNING feature_key, enabled, daily_limit_set, daily_limit, monthly_limit_set,
                              monthly_limit, expires_at, reason, updated_at
                    """,
                    user_id, feature_key, body.enabled, body.daily_limit_set, body.daily_limit,
                    body.monthly_limit_set, body.monthly_limit, body.expires_at, body.reason, current.id,
                )
                await admin_audit(conn, current, meta, "admin.override_set", target_type="user", target_id=user_id,
                                  after={"feature_key": feature_key, **body.model_dump(mode="json")})
    entitlements.invalidate()
    return UserOverride(**dict(row))


@router.delete("/{user_id}/overrides/{feature_key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(
    user_id: uuid.UUID, feature_key: str, request: Request, current: CurrentUser = Depends(require_admin)
) -> Response:
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchval(
                "DELETE FROM user_overrides WHERE user_id = $1 AND feature_key = $2 RETURNING 1", user_id, feature_key
            )
            if not deleted:
                raise not_found("Override")
            await admin_audit(conn, current, meta, "admin.override_removed", target_type="user", target_id=user_id,
                              after={"feature_key": feature_key})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{user_id}/usage/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_usage(
    user_id: uuid.UUID, body: UsageReset, request: Request, current: CurrentUser = Depends(require_admin)
) -> Response:
    """Zero the current day and/or month counters (history in usage_events is kept)."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        await _fetch_user(conn, user_id)
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE usage_counters SET count = 0, updated_at = now()
                WHERE subject_type = 'user' AND subject_id = $1
                  AND ($2::text IS NULL OR feature_key = $2)
                  AND ($3::text IS NULL OR period = $3)
                  AND ((period = 'day' AND period_start = timezone('UTC', now())::date)
                       OR (period = 'month' AND period_start = date_trunc('month', timezone('UTC', now()))::date))
                """,
                user_id, body.feature_key, body.period,
            )
            await admin_audit(conn, current, meta, "admin.usage_reset", target_type="user", target_id=user_id,
                              after=body.model_dump())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
