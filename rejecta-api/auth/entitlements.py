"""Effective feature access for a subject: plan + per-user override + global switch.

Resolution for one feature:

1. ``features.globally_enabled = false``  -> disabled for everyone.
2. The plan row (no row = not in plan).
3. A non-expired user override replaces individual fields: ``enabled`` if
   not NULL; a limit if its ``*_set`` flag is true (NULL then = unlimited).

Plan matrices are cached for a few seconds per process; every admin change
to plans or features calls ``invalidate()``.
"""

import time
import uuid
from dataclasses import dataclass, replace

import asyncpg

from db.repos import entitlements as repo

PLAN_CACHE_SECONDS = 30.0

DISABLED_GLOBALLY = "feature_disabled"
NOT_IN_PLAN = "feature_not_in_plan"


@dataclass(frozen=True)
class Entitlement:
    feature_key: str
    name: str
    description: str
    enabled: bool
    reason: str | None  # why it is disabled
    daily_limit: int | None  # None = unlimited
    monthly_limit: int | None
    overridden: bool = False


_plan_cache: dict[uuid.UUID, tuple[float, list[Entitlement]]] = {}


def invalidate() -> None:
    _plan_cache.clear()


async def _plan_entitlements(conn: asyncpg.Connection, plan_id: uuid.UUID) -> list[Entitlement]:
    now = time.monotonic()
    hit = _plan_cache.get(plan_id)
    if hit and hit[0] > now:
        return hit[1]
    result = []
    for row in await repo.plan_matrix(conn, plan_id):
        if not row["globally_enabled"]:
            enabled, reason = False, DISABLED_GLOBALLY
        elif not row["enabled"]:
            enabled, reason = False, NOT_IN_PLAN
        else:
            enabled, reason = True, None
        result.append(
            Entitlement(
                feature_key=row["feature_key"],
                name=row["name"],
                description=row["description"],
                enabled=enabled,
                reason=reason,
                daily_limit=row["daily_limit"],
                monthly_limit=row["monthly_limit"],
            )
        )
    _plan_cache[plan_id] = (now + PLAN_CACHE_SECONDS, result)
    return result


def _apply_override(ent: Entitlement, row: asyncpg.Record) -> Entitlement:
    if ent.reason == DISABLED_GLOBALLY:
        return ent  # the global switch always wins
    enabled = ent.enabled if row["enabled"] is None else bool(row["enabled"])
    return replace(
        ent,
        enabled=enabled,
        reason=None if enabled else NOT_IN_PLAN,
        daily_limit=row["daily_limit"] if row["daily_limit_set"] else ent.daily_limit,
        monthly_limit=row["monthly_limit"] if row["monthly_limit_set"] else ent.monthly_limit,
        overridden=True,
    )


async def resolve(
    conn: asyncpg.Connection, *, plan_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> dict[str, Entitlement]:
    entitlements = {e.feature_key: e for e in await _plan_entitlements(conn, plan_id)}
    if user_id is not None:
        for row in await repo.active_overrides(conn, user_id):
            if row["feature_key"] in entitlements:
                entitlements[row["feature_key"]] = _apply_override(entitlements[row["feature_key"]], row)
    return entitlements
