"""Metered access to a feature.

    async with usage.consume(subject, features.ANALYSE_PAPER):
        ...do the expensive work...

On entry: checks the feature is enabled for the subject (403 otherwise) and
atomically reserves one unit of the daily and monthly quota (429 when
exhausted). On normal exit the reservation is committed. If the body raises
(bad input, LLM failure, client disconnect) it is refunded, so users are
never charged for work that did not complete. The database connection is
held only for the reservation, not during the work.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from auth import entitlements
from auth.entitlements import Entitlement
from auth.errors import ApiError
from db.pool import get_pool

logger = logging.getLogger("rejecta.usage")


@dataclass(frozen=True)
class Subject:
    """Who is being metered: a signed-in user or a partner API client."""

    kind: str  # 'user' | 'client'
    id: uuid.UUID
    plan_id: uuid.UUID
    label: str  # email or client name, for logs
    scopes: frozenset[str] | None = None  # partners: features the client may call

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.id if self.kind == "user" else None


def period_reset(period: str, now: datetime | None = None) -> datetime:
    """Start of the next UTC day or month."""
    now = now or datetime.now(timezone.utc)
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)
    return (month_start + timedelta(days=32)).replace(day=1)


def _denied(ent: Entitlement | None, feature_key: str) -> ApiError:
    name = ent.name if ent else feature_key
    if ent is not None and ent.reason == entitlements.DISABLED_GLOBALLY:
        return ApiError(403, "feature_disabled", f"{name} is temporarily unavailable.", extra={"feature": feature_key})
    return ApiError(
        403, "feature_not_in_plan", f"{name} is not included in your plan.", extra={"feature": feature_key}
    )


async def _finalize(event_id: int, success: bool, duration_ms: int) -> None:
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT finalize_usage($1, $2, $3)", event_id, success, duration_ms)
    except Exception:  # noqa: BLE001 - never mask the real outcome
        logger.exception("Could not finalize usage event %s (success=%s)", event_id, success)


@asynccontextmanager
async def consume(subject: Subject, feature_key: str, *, request_id: str | None = None) -> AsyncIterator[Entitlement]:
    if subject.scopes is not None and feature_key not in subject.scopes:
        raise ApiError(403, "insufficient_scope", f"This API client is not allowed to use {feature_key}.",
                       extra={"feature": feature_key})
    async with get_pool().acquire() as conn:
        ents = await entitlements.resolve(conn, plan_id=subject.plan_id, user_id=subject.user_id)
        ent = ents.get(feature_key)
        if ent is None or not ent.enabled:
            raise _denied(ent, feature_key)
        row = await conn.fetchrow(
            "SELECT * FROM consume_quota($1, $2, $3, $4, $5, $6)",
            subject.kind,
            subject.id,
            feature_key,
            ent.daily_limit,
            ent.monthly_limit,
            request_id,
        )

    if not row["allowed"]:
        period = row["exceeded_period"]
        limit = ent.daily_limit if period == "day" else ent.monthly_limit
        resets_at = period_reset(period)
        wait = max(1, int((resets_at - datetime.now(timezone.utc)).total_seconds()))
        label = "Daily" if period == "day" else "Monthly"
        raise ApiError(
            429,
            "quota_exceeded",
            f"{label} limit reached for {ent.name} ({limit}). It resets {resets_at:%d %b %Y %H:%M} UTC.",
            headers={"Retry-After": str(wait)},
            extra={
                "feature": feature_key,
                "period": period,
                "limit": limit,
                "used": row["day_count"] if period == "day" else row["month_count"],
                "resets_at": resets_at.isoformat(),
            },
        )

    event_id = row["event_id"]
    started = time.monotonic()
    try:
        yield ent
    except BaseException:
        # shield: a cancelled request (client disconnect) must still refund.
        await asyncio.shield(_finalize(event_id, False, int((time.monotonic() - started) * 1000)))
        raise
    await _finalize(event_id, True, int((time.monotonic() - started) * 1000))


async def refund_stale_reservations(older_than_minutes: int = 30) -> int:
    """Refund reservations left open by a crash or redeploy mid-request.

    A normal request finalizes its reservation within seconds; anything still
    'reserved' after ``older_than_minutes`` never completed, so the user gets
    the unit back. Called on startup.
    """
    async with get_pool().acquire() as conn:
        ids = await conn.fetch(
            "SELECT id FROM usage_events WHERE status = 'reserved' "
            "AND created_at < now() - make_interval(mins => $1)",
            older_than_minutes,
        )
        for row in ids:
            await conn.fetchval("SELECT finalize_usage($1, false, NULL)", row["id"])
    return len(ids)
