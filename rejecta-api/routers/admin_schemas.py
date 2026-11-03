"""Request/response bodies for the admin API. Requests forbid unknown fields."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from auth.schemas import FeatureUsage

Role = Literal["admin", "user"]
UserStatus = Literal["active", "suspended"]
PlanStatus = Literal["active", "archived"]
Limit = int | None  # None = unlimited
LIMIT_FIELD = Field(default=None, ge=0, le=1_000_000)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- users ------------------------------------------------------------------


class PlanRef(BaseModel):
    id: uuid.UUID
    key: str
    name: str


class AdminUser(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    status: UserStatus
    plan: PlanRef
    mfa_enabled: bool
    locked: bool
    created_at: datetime
    last_login_at: datetime | None


class AdminUserList(BaseModel):
    items: list[AdminUser]
    total: int
    page: int
    page_size: int


class UserOverride(BaseModel):
    feature_key: str
    enabled: bool | None
    daily_limit_set: bool
    daily_limit: Limit
    monthly_limit_set: bool
    monthly_limit: Limit
    expires_at: datetime | None
    reason: str
    updated_at: datetime


class AdminUserDetail(AdminUser):
    features: list[FeatureUsage]
    overrides: list[UserOverride]
    active_sessions: int
    recovery_codes_left: int | None


class AdminUserCreate(_Strict):
    email: EmailStr
    full_name: str = Field(default="", max_length=120)
    role: Role = "user"
    plan_id: uuid.UUID | None = None


class AdminUserUpdate(_Strict):
    full_name: str | None = Field(default=None, max_length=120)
    role: Role | None = None
    status: UserStatus | None = None
    plan_id: uuid.UUID | None = None


class PasswordLink(BaseModel):
    url: str
    expires_at: datetime


class AdminUserCreated(BaseModel):
    user: AdminUser
    password_setup: PasswordLink


class OverrideUpdate(_Strict):
    """Per-field: enabled None = inherit. For a limit, *_set=false = inherit,
    *_set=true with value None = unlimited, with a number = that limit."""

    enabled: bool | None = None
    daily_limit_set: bool = False
    daily_limit: Limit = LIMIT_FIELD
    monthly_limit_set: bool = False
    monthly_limit: Limit = LIMIT_FIELD
    expires_at: datetime | None = None
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> "OverrideUpdate":
        if self.daily_limit is not None and not self.daily_limit_set:
            raise ValueError("daily_limit needs daily_limit_set=true")
        if self.monthly_limit is not None and not self.monthly_limit_set:
            raise ValueError("monthly_limit needs monthly_limit_set=true")
        if (
            self.daily_limit is not None
            and self.monthly_limit is not None
            and self.daily_limit > self.monthly_limit
        ):
            raise ValueError("daily_limit cannot exceed monthly_limit")
        return self


class UsageReset(_Strict):
    feature_key: str | None = None  # None = all features
    period: Literal["day", "month"] | None = None  # None = both


# --- plans & features -------------------------------------------------------


class PlanFeature(BaseModel):
    feature_key: str
    name: str
    globally_enabled: bool
    enabled: bool
    daily_limit: Limit
    monthly_limit: Limit


class AdminPlan(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str
    is_default: bool
    status: PlanStatus
    sort_order: int
    version: int
    user_count: int
    client_count: int
    usage_this_month: int
    created_at: datetime
    updated_at: datetime
    features: list[PlanFeature]


class PlanCreate(_Strict):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    sort_order: int = 0
    copy_from_plan_id: uuid.UUID | None = None


class PlanUpdate(_Strict):
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    sort_order: int | None = None
    is_default: Literal[True] | None = None  # make this the default (cannot "unset")
    status: PlanStatus | None = None


class PlanFeatureUpdate(_Strict):
    feature_key: str
    enabled: bool
    daily_limit: Limit = LIMIT_FIELD
    monthly_limit: Limit = LIMIT_FIELD

    @model_validator(mode="after")
    def _daily_le_monthly(self) -> "PlanFeatureUpdate":
        if self.daily_limit is not None and self.monthly_limit is not None and self.daily_limit > self.monthly_limit:
            raise ValueError(f"{self.feature_key}: daily limit cannot exceed monthly limit")
        return self


class PlanFeaturesUpdate(_Strict):
    version: int
    features: list[PlanFeatureUpdate] = Field(min_length=1)


class PlanDuplicate(_Strict):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)


class PlanMigrateUsers(_Strict):
    to_plan_id: uuid.UUID


class MigrateResult(BaseModel):
    moved_users: int
    moved_clients: int


class PlanUsageRow(BaseModel):
    feature_key: str
    name: str
    used: int
    subjects: int


class PlanUsage(BaseModel):
    plan_id: uuid.UUID
    period: Literal["day", "month"]
    period_start: datetime
    rows: list[PlanUsageRow]


class AdminFeature(BaseModel):
    key: str
    name: str
    description: str
    globally_enabled: bool
    sort_order: int


class FeatureUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    globally_enabled: bool | None = None


# --- analytics --------------------------------------------------------------


class Stats(BaseModel):
    users_total: int
    users_active: int
    users_suspended: int
    admins: int
    signups_last_7_days: int
    logins_today: int
    usage_today: int
    usage_this_month: int
    users_by_plan: list[dict[str, Any]]


class UsagePoint(BaseModel):
    day: datetime
    feature_key: str
    committed: int
    refunded: int


class UsageByPlan(BaseModel):
    plan_key: str
    plan_name: str
    feature_key: str
    committed: int


class UsageReport(BaseModel):
    days: int
    series: list[UsagePoint]
    by_plan: list[UsageByPlan]


class AuditEntry(BaseModel):
    id: int
    created_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    before: Any
    after: Any
    ip: str | None
    user_agent: str | None


class AuditPage(BaseModel):
    items: list[AuditEntry]
    total: int = 0
    page: int = 1
    page_size: int = 25
    next_before_id: int | None = None


# --- partners ---------------------------------------------------------------


class PartnerSecret(BaseModel):
    id: uuid.UUID
    prefix: str
    last4: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class AdminPartner(BaseModel):
    id: uuid.UUID
    client_id: str
    name: str
    description: str
    plan: PlanRef
    scopes: list[str]
    status: Literal["active", "disabled"]
    created_at: datetime
    last_used_at: datetime | None
    secrets: list[PartnerSecret]


class AdminPartnerList(BaseModel):
    items: list[AdminPartner]
    total: int
    page: int
    page_size: int


class AdminPartnerDetail(AdminPartner):
    features: list[FeatureUsage]


class PartnerCreate(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    plan_id: uuid.UUID
    scopes: list[str] = Field(min_length=1)


class PartnerUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    plan_id: uuid.UUID | None = None
    scopes: list[str] | None = Field(default=None, min_length=1)
    status: Literal["active", "disabled"] | None = None


class NewPartnerSecret(BaseModel):
    """The secret is shown exactly once."""

    client_id: str
    client_secret: str
    secret: PartnerSecret


class PartnerCreated(BaseModel):
    partner: AdminPartner
    credentials: NewPartnerSecret
