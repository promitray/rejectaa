"""Request and response bodies for auth routes.

Request models forbid unknown fields, so a client cannot smuggle in
``role``, ``plan_id`` or similar (mass assignment).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

# Upper bound before hashing (the policy max is 128); stops huge payloads.
PASSWORD_FIELD = Field(min_length=1, max_length=256)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupRequest(_Strict):
    email: EmailStr
    password: str = PASSWORD_FIELD
    full_name: str = Field(default="", max_length=120)


class LoginRequest(_Strict):
    email: EmailStr
    password: str = PASSWORD_FIELD


class ResetPasswordRequest(_Strict):
    token: str = Field(min_length=10, max_length=128)
    new_password: str = PASSWORD_FIELD


class ChangePasswordRequest(_Strict):
    current_password: str = PASSWORD_FIELD
    new_password: str = PASSWORD_FIELD


class UserPublic(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Literal["admin", "user"]
    created_at: datetime


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    user: UserPublic


class PlanPublic(BaseModel):
    id: uuid.UUID
    key: str
    name: str


class MfaChallengeResponse(BaseModel):
    """Login step one passed for an admin; a TOTP step must follow."""

    mfa_required: Literal[True] = True
    mfa_enrollment_required: bool
    mfa_token: str
    expires_in: int


MFA_TOKEN_FIELD = Field(min_length=1, max_length=4096)


class MfaTokenRequest(_Strict):
    mfa_token: str = MFA_TOKEN_FIELD


class MfaEnrollStartResponse(BaseModel):
    secret: str
    otpauth_uri: str
    issuer: str
    account: str


class MfaCodeRequest(_Strict):
    mfa_token: str = MFA_TOKEN_FIELD
    code: str = Field(min_length=6, max_length=12)


class MfaVerifyRequest(_Strict):
    """Exactly one of ``code`` (authenticator) or ``recovery_code``."""

    mfa_token: str = MFA_TOKEN_FIELD
    code: str | None = Field(default=None, min_length=6, max_length=12)
    recovery_code: str | None = Field(default=None, min_length=10, max_length=20)

    @model_validator(mode="after")
    def _exactly_one(self) -> "MfaVerifyRequest":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Provide either code or recovery_code")
        return self


class AuthWithRecoveryCodes(AuthResponse):
    """Returned once, when MFA is enrolled or codes are regenerated."""

    recovery_codes: list[str]


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class FeatureUsage(BaseModel):
    """One feature as the signed-in user sees it. Limits: null = unlimited."""

    key: str
    name: str
    description: str
    enabled: bool
    reason: str | None
    daily_limit: int | None
    monthly_limit: int | None
    used_today: int
    used_this_month: int
    remaining_today: int | None
    remaining_this_month: int | None
    day_resets_at: datetime
    month_resets_at: datetime


class MeResponse(UserPublic):
    status: Literal["active", "suspended"]
    mfa_enabled: bool
    aal: int
    plan: PlanPublic | None
    features: list[FeatureUsage] = []
