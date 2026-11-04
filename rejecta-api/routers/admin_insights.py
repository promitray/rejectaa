"""Admin: dashboard stats, usage analytics, audit log."""

import uuid

from fastapi import APIRouter, Depends, Query

from auth.dependencies import CurrentUser, require_admin
from db.pool import get_pool
from routers.admin_common import jsonish
from routers.admin_schemas import AuditEntry, AuditPage, Stats, UsageByPlan, UsagePoint, UsageReport

router = APIRouter(prefix="/admin", tags=["admin"])

_TODAY = "timezone('UTC', now())::date"
_MONTH = "date_trunc('month', timezone('UTC', now()))::date"


@router.get("/stats", response_model=Stats)
async def stats(_: CurrentUser = Depends(require_admin)) -> Stats:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT count(*) AS users_total,
                   count(*) FILTER (WHERE status = 'active') AS users_active,
                   count(*) FILTER (WHERE status = 'suspended') AS users_suspended,
                   count(*) FILTER (WHERE role = 'admin') AS admins,
                   count(*) FILTER (WHERE created_at > now() - interval '7 days') AS signups_last_7_days,
                   (SELECT count(*) FROM audit_log WHERE action = 'auth.login'
                     AND created_at >= {_TODAY}::timestamp AT TIME ZONE 'UTC') AS logins_today,
                   (SELECT COALESCE(sum(count), 0) FROM usage_counters
                     WHERE period = 'day' AND period_start = {_TODAY}) AS usage_today,
                   (SELECT COALESCE(sum(count), 0) FROM usage_counters
                     WHERE period = 'month' AND period_start = {_MONTH}) AS usage_this_month
            FROM users
            """
        )
        by_plan = await conn.fetch(
            """
            SELECT p.key, p.name, p.status, count(u.id) AS users
            FROM plans p LEFT JOIN users u ON u.plan_id = p.id
            GROUP BY p.id ORDER BY p.sort_order, p.name
            """
        )
    return Stats(**dict(row), users_by_plan=[dict(r) for r in by_plan])


@router.get("/usage", response_model=UsageReport)
async def usage(days: int = Query(default=30, ge=1, le=365), _: CurrentUser = Depends(require_admin)) -> UsageReport:
    """Daily committed/refunded units per feature, and totals per plan (current plan)."""
    async with get_pool().acquire() as conn:
        series = await conn.fetch(
            """
            SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS day, feature_key,
                   count(*) FILTER (WHERE status = 'committed') AS committed,
                   count(*) FILTER (WHERE status = 'refunded') AS refunded
            FROM usage_events
            WHERE created_at >= now() - make_interval(days => $1)
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            days,
        )
        by_plan = await conn.fetch(
            """
            SELECT p.key AS plan_key, p.name AS plan_name, e.feature_key, count(*) AS committed
            FROM usage_events e
            LEFT JOIN users u ON e.subject_type = 'user' AND u.id = e.subject_id
            LEFT JOIN api_clients a ON e.subject_type = 'client' AND a.id = e.subject_id
            JOIN plans p ON p.id = COALESCE(u.plan_id, a.plan_id)
            WHERE e.status = 'committed' AND e.created_at >= now() - make_interval(days => $1)
            GROUP BY 1, 2, 3 ORDER BY 1, 3
            """,
            days,
        )
    return UsageReport(
        days=days,
        series=[UsagePoint(**dict(r)) for r in series],
        by_plan=[UsageByPlan(**dict(r)) for r in by_plan],
    )


@router.get("/audit-log", response_model=AuditPage)
async def audit_log(
    action: str | None = Query(default=None, max_length=80, description="Prefix, e.g. 'admin.' or 'auth.login'"),
    actor_id: uuid.UUID | None = None,
    target_id: str | None = Query(default=None, max_length=80),
    before_id: int | None = Query(default=None, description="Page backwards from this id"),
    limit: int | None = Query(default=None, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: CurrentUser = Depends(require_admin),
) -> AuditPage:
    effective_limit = limit if limit is not None else page_size
    offset = (page - 1) * effective_limit if before_id is None else 0
    async with get_pool().acquire() as conn:
        if before_id is not None:
            rows = await conn.fetch(
                """
                SELECT a.id, a.created_at, a.actor_type, a.actor_id, u.email AS actor_email, a.action,
                       a.target_type, a.target_id, a.before, a.after, host(a.ip) AS ip, a.user_agent
                FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
                WHERE ($1::text IS NULL OR starts_with(a.action, $1))
                  AND ($2::uuid IS NULL OR a.actor_id = $2)
                  AND ($3::text IS NULL OR a.target_id = $3)
                  AND a.id < $4
                ORDER BY a.id DESC
                LIMIT $5
                """,
                action, actor_id, target_id, before_id, effective_limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT a.id, a.created_at, a.actor_type, a.actor_id, u.email AS actor_email, a.action,
                       a.target_type, a.target_id, a.before, a.after, host(a.ip) AS ip, a.user_agent
                FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
                WHERE ($1::text IS NULL OR starts_with(a.action, $1))
                  AND ($2::uuid IS NULL OR a.actor_id = $2)
                  AND ($3::text IS NULL OR a.target_id = $3)
                ORDER BY a.id DESC
                LIMIT $4 OFFSET $5
                """,
                action, actor_id, target_id, effective_limit, offset,
            )
        total = await conn.fetchval(
            """
            SELECT count(*) FROM audit_log a
            WHERE ($1::text IS NULL OR starts_with(a.action, $1))
              AND ($2::uuid IS NULL OR a.actor_id = $2)
              AND ($3::text IS NULL OR a.target_id = $3)
            """,
            action, actor_id, target_id,
        )
    items = [
        AuditEntry(**{**dict(r), "before": jsonish(r["before"]), "after": jsonish(r["after"])}) for r in rows
    ]
    return AuditPage(
        items=items,
        total=total or 0,
        page=page,
        page_size=effective_limit,
        next_before_id=items[-1].id if len(items) == effective_limit else None,
    )
