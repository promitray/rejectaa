"""The signed-in user's own profile, plan and feature usage."""

import uuid

import asyncpg
from fastapi import APIRouter, Depends, Response

from auth import entitlements
from auth.dependencies import CurrentUser, get_current_user
from auth.schemas import FeatureUsage, MeResponse, PlanPublic
from auth.usage import period_reset
from db.pool import get_pool
from db.repos import entitlements as entitlements_repo
from db.repos import users

router = APIRouter(tags=["me"])


def _remaining(limit: int | None, used: int) -> int | None:
    return None if limit is None else max(0, limit - used)


async def feature_usage(
    conn: asyncpg.Connection, *, subject_type: str, subject_id: uuid.UUID, plan_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> list[FeatureUsage]:
    """Effective entitlements plus current-period usage (also used by the admin API)."""
    ents = await entitlements.resolve(conn, plan_id=plan_id, user_id=user_id)
    usage = await entitlements_repo.current_usage(conn, subject_type, subject_id)
    day_reset, month_reset = period_reset("day"), period_reset("month")
    result = []
    for ent in ents.values():
        used_day = usage.get((ent.feature_key, "day"), 0)
        used_month = usage.get((ent.feature_key, "month"), 0)
        result.append(
            FeatureUsage(
                key=ent.feature_key,
                name=ent.name,
                description=ent.description,
                enabled=ent.enabled,
                reason=ent.reason,
                daily_limit=ent.daily_limit,
                monthly_limit=ent.monthly_limit,
                used_today=used_day,
                used_this_month=used_month,
                remaining_today=_remaining(ent.daily_limit, used_day),
                remaining_this_month=_remaining(ent.monthly_limit, used_month),
                day_resets_at=day_reset,
                month_resets_at=month_reset,
            )
        )
    return result


@router.get("/me", response_model=MeResponse)
async def me(response: Response, current: CurrentUser = Depends(get_current_user)) -> MeResponse:
    """Profile, role, plan and what the user can use right now."""
    async with get_pool().acquire() as conn:
        plan = await users.get_plan_summary(conn, current.plan_id)
        features = await feature_usage(
            conn, subject_type="user", subject_id=current.id, plan_id=current.plan_id, user_id=current.id
        )
    response.headers["Cache-Control"] = "no-store"
    return MeResponse(
        id=current.id,
        email=current.email,
        full_name=current.full_name,
        role=current.role,  # type: ignore[arg-type]
        created_at=current.created_at,
        status=current.status,  # type: ignore[arg-type]
        mfa_enabled=current.mfa_enabled,
        aal=current.aal,
        plan=PlanPublic(id=plan.id, key=plan.key, name=plan.name) if plan else None,
        features=features,
    )
