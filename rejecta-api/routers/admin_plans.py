"""Admin: plans (feature matrix, limits, default, archive) and the feature catalog.

Concurrency: every plan change must send the ``version`` it was based on.
If another admin saved in between, the request fails with 409
``version_conflict`` instead of silently overwriting their change.

Changes apply on the next request: the entitlement cache is cleared.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from auth import entitlements, user_cache
from auth.dependencies import CurrentUser, require_admin
from auth.errors import ApiError
from auth.request_meta import request_meta
from db.pool import get_pool
from routers.admin_common import admin_audit, db_errors, not_found
from routers.admin_schemas import (
    AdminFeature,
    AdminPlan,
    FeatureUpdate,
    MigrateResult,
    PlanCreate,
    PlanDuplicate,
    PlanFeature,
    PlanFeaturesUpdate,
    PlanMigrateUsers,
    PlanUpdate,
    PlanUsage,
    PlanUsageRow,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_PLAN_SELECT = """
    SELECT p.*,
           (SELECT count(*) FROM users u WHERE u.plan_id = p.id) AS user_count,
           (SELECT count(*) FROM api_clients a WHERE a.plan_id = p.id) AS client_count,
           (SELECT COALESCE(sum(c.count), 0) FROM usage_counters c
             WHERE c.period = 'month'
               AND c.period_start = date_trunc('month', timezone('UTC', now()))::date
               AND ((c.subject_type = 'user' AND c.subject_id IN (SELECT id FROM users WHERE plan_id = p.id))
                 OR (c.subject_type = 'client' AND c.subject_id IN (SELECT id FROM api_clients WHERE plan_id = p.id)))
           ) AS usage_this_month
    FROM plans p
"""

_MATRIX_SELECT = """
    SELECT f.key AS feature_key, f.name, f.globally_enabled,
           COALESCE(pf.enabled, false) AS enabled, pf.daily_limit, pf.monthly_limit
    FROM features f
    LEFT JOIN plan_features pf ON pf.feature_key = f.key AND pf.plan_id = $1
    ORDER BY f.sort_order, f.key
"""


async def _plan(conn: asyncpg.Connection, plan_id: uuid.UUID) -> AdminPlan:
    row = await conn.fetchrow(_PLAN_SELECT + " WHERE p.id = $1", plan_id)
    if row is None:
        raise not_found("Plan")
    matrix = await conn.fetch(_MATRIX_SELECT, plan_id)
    return AdminPlan(
        id=row["id"], key=row["key"], name=row["name"], description=row["description"],
        is_default=row["is_default"], status=row["status"], sort_order=row["sort_order"],
        version=row["version"], user_count=row["user_count"], client_count=row["client_count"],
        usage_this_month=row["usage_this_month"], created_at=row["created_at"], updated_at=row["updated_at"],
        features=[PlanFeature(**dict(m)) for m in matrix],
    )


async def _bump_version(conn: asyncpg.Connection, plan_id: uuid.UUID, version: int) -> None:
    """Optimistic lock: succeed only if nobody saved since ``version``."""
    bumped = await conn.fetchval(
        "UPDATE plans SET version = version + 1 WHERE id = $1 AND version = $2 RETURNING version", plan_id, version
    )
    if bumped is None:
        if await conn.fetchval("SELECT 1 FROM plans WHERE id = $1", plan_id) is None:
            raise not_found("Plan")
        raise ApiError(409, "version_conflict", "This plan was changed by someone else. Reload and try again.")


@router.get("/plans", response_model=list[AdminPlan])
async def list_plans(_: CurrentUser = Depends(require_admin)) -> list[AdminPlan]:
    async with get_pool().acquire() as conn:
        ids = await conn.fetch("SELECT id FROM plans ORDER BY status, sort_order, name")
        return [await _plan(conn, r["id"]) for r in ids]


@router.post("/plans", response_model=AdminPlan, status_code=201)
async def create_plan(body: PlanCreate, request: Request, current: CurrentUser = Depends(require_admin)) -> AdminPlan:
    """New plan. Features start disabled unless ``copy_from_plan_id`` is given."""
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        with db_errors():
            async with conn.transaction():
                plan_id = await conn.fetchval(
                    "INSERT INTO plans (key, name, description, sort_order) VALUES ($1, $2, $3, $4) RETURNING id",
                    body.key, body.name.strip(), body.description, body.sort_order,
                )
                await _copy_matrix(conn, body.copy_from_plan_id, plan_id)
                await admin_audit(conn, current, meta, "admin.plan_created", target_type="plan", target_id=plan_id,
                                  after=body.model_dump(mode="json"))
        return await _plan(conn, plan_id)


async def _copy_matrix(conn: asyncpg.Connection, source: uuid.UUID | None, target: uuid.UUID) -> None:
    if source is None:
        await conn.execute(
            "INSERT INTO plan_features (plan_id, feature_key, enabled) SELECT $1, key, false FROM features", target
        )
        return
    if await conn.fetchval("SELECT 1 FROM plans WHERE id = $1", source) is None:
        raise not_found("Source plan")
    await conn.execute(
        """
        INSERT INTO plan_features (plan_id, feature_key, enabled, daily_limit, monthly_limit)
        SELECT $2, f.key, COALESCE(pf.enabled, false), pf.daily_limit, pf.monthly_limit
        FROM features f LEFT JOIN plan_features pf ON pf.feature_key = f.key AND pf.plan_id = $1
        """,
        source, target,
    )


@router.get("/plans/{plan_id}", response_model=AdminPlan)
async def get_plan(plan_id: uuid.UUID, _: CurrentUser = Depends(require_admin)) -> AdminPlan:
    async with get_pool().acquire() as conn:
        return await _plan(conn, plan_id)


@router.patch("/plans/{plan_id}", response_model=AdminPlan)
async def update_plan(
    plan_id: uuid.UUID, body: PlanUpdate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminPlan:
    """Rename, reorder, make default, archive or restore a plan."""
    meta = request_meta(request)
    changes = body.model_dump(exclude_none=True, exclude={"version"})
    if not changes:
        raise ApiError(400, "nothing_to_change", "No changes given.")
    async with get_pool().acquire() as conn:
        before = await _plan(conn, plan_id)
        with db_errors():
            async with conn.transaction():
                await _bump_version(conn, plan_id, body.version)
                if body.is_default:
                    # Unset the old default first; the "exactly one default" rule
                    # is checked at commit, so the switch is atomic.
                    await conn.execute("UPDATE plans SET is_default = false WHERE is_default AND id <> $1", plan_id)
                await conn.execute(
                    """
                    UPDATE plans SET name = COALESCE($2, name), description = COALESCE($3, description),
                                     sort_order = COALESCE($4, sort_order), status = COALESCE($5, status),
                                     is_default = CASE WHEN $6 THEN true ELSE is_default END
                    WHERE id = $1
                    """,
                    plan_id, body.name, body.description, body.sort_order, body.status, bool(body.is_default),
                )
                await admin_audit(conn, current, meta, "admin.plan_updated", target_type="plan", target_id=plan_id,
                                  before=before.model_dump(include=set(changes), mode="json"), after=changes)
        entitlements.invalidate()
        return await _plan(conn, plan_id)


@router.put("/plans/{plan_id}/features", response_model=AdminPlan)
async def update_plan_features(
    plan_id: uuid.UUID, body: PlanFeaturesUpdate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminPlan:
    """Save the plan's feature matrix (on/off + daily/monthly limits) atomically."""
    meta = request_meta(request)
    keys = [f.feature_key for f in body.features]
    if len(keys) != len(set(keys)):
        raise ApiError(400, "duplicate_feature", "Each feature may appear only once.")
    async with get_pool().acquire() as conn:
        known = {r["key"] for r in await conn.fetch("SELECT key FROM features")}
        unknown = sorted(set(keys) - known)
        if unknown:
            raise ApiError(400, "unknown_feature", f"Unknown feature(s): {', '.join(unknown)}.")
        before = await _plan(conn, plan_id)
        with db_errors():
            async with conn.transaction():
                await _bump_version(conn, plan_id, body.version)
                await conn.executemany(
                    """
                    INSERT INTO plan_features (plan_id, feature_key, enabled, daily_limit, monthly_limit)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (plan_id, feature_key) DO UPDATE SET
                        enabled = EXCLUDED.enabled, daily_limit = EXCLUDED.daily_limit,
                        monthly_limit = EXCLUDED.monthly_limit
                    """,
                    [(plan_id, f.feature_key, f.enabled, f.daily_limit, f.monthly_limit) for f in body.features],
                )
                await admin_audit(
                    conn, current, meta, "admin.plan_features_updated", target_type="plan", target_id=plan_id,
                    before={"features": [f.model_dump() for f in before.features]},
                    after={"features": [f.model_dump() for f in body.features]},
                )
        entitlements.invalidate()
        return await _plan(conn, plan_id)


@router.post("/plans/{plan_id}/duplicate", response_model=AdminPlan, status_code=201)
async def duplicate_plan(
    plan_id: uuid.UUID, body: PlanDuplicate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminPlan:
    meta = request_meta(request)
    async with get_pool().acquire() as conn:
        source = await _plan(conn, plan_id)
        with db_errors():
            async with conn.transaction():
                new_id = await conn.fetchval(
                    "INSERT INTO plans (key, name, description, sort_order) VALUES ($1, $2, $3, $4) RETURNING id",
                    body.key, body.name.strip(), source.description, source.sort_order + 1,
                )
                await _copy_matrix(conn, plan_id, new_id)
                await admin_audit(conn, current, meta, "admin.plan_duplicated", target_type="plan", target_id=new_id,
                                  after={"from": str(plan_id), **body.model_dump()})
        return await _plan(conn, new_id)


@router.post("/plans/{plan_id}/migrate-users", response_model=MigrateResult)
async def migrate_users(
    plan_id: uuid.UUID, body: PlanMigrateUsers, request: Request, current: CurrentUser = Depends(require_admin)
) -> MigrateResult:
    """Move every user and partner client on this plan to another active plan."""
    meta = request_meta(request)
    if body.to_plan_id == plan_id:
        raise ApiError(400, "same_plan", "Choose a different plan.")
    async with get_pool().acquire() as conn:
        await _plan(conn, plan_id)
        target_status = await conn.fetchval("SELECT status FROM plans WHERE id = $1", body.to_plan_id)
        if target_status is None:
            raise not_found("Target plan")
        if target_status != "active":
            raise ApiError(400, "plan_archived", "The target plan is archived.")
        with db_errors():
            async with conn.transaction():
                moved_users = int((await conn.execute(
                    "UPDATE users SET plan_id = $2 WHERE plan_id = $1", plan_id, body.to_plan_id
                )).split()[-1])
                moved_clients = int((await conn.execute(
                    "UPDATE api_clients SET plan_id = $2 WHERE plan_id = $1", plan_id, body.to_plan_id
                )).split()[-1])
                await admin_audit(conn, current, meta, "admin.plan_users_migrated", target_type="plan",
                                  target_id=plan_id, after={"to_plan_id": str(body.to_plan_id),
                                                            "users": moved_users, "clients": moved_clients})
    user_cache.clear()
    return MigrateResult(moved_users=moved_users, moved_clients=moved_clients)


@router.get("/plans/{plan_id}/usage", response_model=PlanUsage)
async def plan_usage(
    plan_id: uuid.UUID,
    period: Literal["day", "month"] = Query(default="month"),
    _: CurrentUser = Depends(require_admin),
) -> PlanUsage:
    """Units used in the current UTC day or month by everyone on this plan."""
    async with get_pool().acquire() as conn:
        await _plan(conn, plan_id)
        rows = await conn.fetch(
            """
            SELECT f.key AS feature_key, f.name,
                   COALESCE(sum(c.count), 0) AS used,
                   count(DISTINCT c.subject_id) FILTER (WHERE c.count > 0) AS subjects
            FROM features f
            LEFT JOIN usage_counters c ON c.feature_key = f.key AND c.period = $2
                 AND c.period_start = CASE WHEN $2 = 'day' THEN timezone('UTC', now())::date
                                           ELSE date_trunc('month', timezone('UTC', now()))::date END
                 AND ((c.subject_type = 'user' AND c.subject_id IN (SELECT id FROM users WHERE plan_id = $1))
                   OR (c.subject_type = 'client' AND c.subject_id IN (SELECT id FROM api_clients WHERE plan_id = $1)))
            GROUP BY f.key, f.name, f.sort_order
            ORDER BY f.sort_order
            """,
            plan_id, period,
        )
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
    return PlanUsage(plan_id=plan_id, period=period, period_start=start,
                     rows=[PlanUsageRow(**dict(r)) for r in rows])


@router.get("/features", response_model=list[AdminFeature])
async def list_features(_: CurrentUser = Depends(require_admin)) -> list[AdminFeature]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, name, description, globally_enabled, sort_order FROM features ORDER BY sort_order, key"
        )
    return [AdminFeature(**dict(r)) for r in rows]


@router.patch("/features/{feature_key}", response_model=AdminFeature)
async def update_feature(
    feature_key: str, body: FeatureUpdate, request: Request, current: CurrentUser = Depends(require_admin)
) -> AdminFeature:
    """Rename / describe a feature, or switch it off for everyone (kill switch)."""
    meta = request_meta(request)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(400, "nothing_to_change", "No changes given.")
    async with get_pool().acquire() as conn:
        with db_errors():
            async with conn.transaction():
                before = await conn.fetchrow("SELECT * FROM features WHERE key = $1 FOR UPDATE", feature_key)
                if before is None:
                    raise not_found("Feature")
                row = await conn.fetchrow(
                    """
                    UPDATE features SET name = COALESCE($2, name), description = COALESCE($3, description),
                                        globally_enabled = COALESCE($4, globally_enabled)
                    WHERE key = $1
                    RETURNING key, name, description, globally_enabled, sort_order
                    """,
                    feature_key, body.name, body.description, body.globally_enabled,
                )
                await admin_audit(conn, current, meta, "admin.feature_updated", target_type="feature",
                                  target_id=feature_key, before={k: before[k] for k in changes}, after=changes)
    entitlements.invalidate()
    return AdminFeature(**dict(row))
