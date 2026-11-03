"""Queries behind entitlements: plan feature matrix, overrides, usage counters."""

import uuid

import asyncpg


async def plan_matrix(conn: asyncpg.Connection, plan_id: uuid.UUID) -> list[asyncpg.Record]:
    """Every feature with this plan's setting (missing rows = not in plan)."""
    return await conn.fetch(
        """
        SELECT f.key AS feature_key, f.name, f.description, f.sort_order, f.globally_enabled,
               COALESCE(pf.enabled, false) AS enabled, pf.daily_limit, pf.monthly_limit
        FROM features f
        LEFT JOIN plan_features pf ON pf.feature_key = f.key AND pf.plan_id = $1
        ORDER BY f.sort_order, f.key
        """,
        plan_id,
    )


async def active_overrides(conn: asyncpg.Connection, user_id: uuid.UUID) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT feature_key, enabled, daily_limit_set, daily_limit, monthly_limit_set, monthly_limit,
               expires_at, reason, set_by, updated_at
        FROM user_overrides
        WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > now())
        """,
        user_id,
    )


async def current_usage(
    conn: asyncpg.Connection, subject_type: str, subject_id: uuid.UUID
) -> dict[tuple[str, str], int]:
    """{(feature_key, 'day'|'month'): count} for the current UTC day and month."""
    rows = await conn.fetch(
        """
        SELECT feature_key, period, count FROM usage_counters
        WHERE subject_type = $1 AND subject_id = $2
          AND ((period = 'day' AND period_start = timezone('UTC', now())::date)
               OR (period = 'month' AND period_start = date_trunc('month', timezone('UTC', now()))::date))
        """,
        subject_type,
        subject_id,
    )
    return {(r["feature_key"], r["period"]): r["count"] for r in rows}
