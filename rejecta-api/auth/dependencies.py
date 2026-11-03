"""FastAPI dependencies that authenticate requests.

``get_current_user`` accepts only ``Authorization: Bearer <access JWT>``.
After verifying the signature and claims it re-checks the user in the
database (cached briefly): the account must exist, be active, and still
have the token_version the token was issued for.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth import user_cache
from auth.errors import forbidden, service_unavailable, unauthorized
from auth.tokens import AccessTokenError, TokenConfigError, decode_access_token
from db.pool import DatabaseUnavailableError

_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")
bearer_scheme = _bearer  # public name, for routes that accept an optional Bearer token


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    status: str
    plan_id: uuid.UUID
    token_version: int
    mfa_enabled: bool
    created_at: datetime
    aal: int
    session_id: uuid.UUID

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized("missing_token", "Not authenticated")
    try:
        claims = decode_access_token(credentials.credentials)
        state = await user_cache.get(claims.user_id)
    except AccessTokenError as exc:
        raise unauthorized("invalid_token", "Invalid or expired token") from exc
    except (TokenConfigError, DatabaseUnavailableError) as exc:
        raise service_unavailable() from exc

    if state is None or state.status != "active" or state.token_version != claims.token_version:
        raise unauthorized("invalid_token", "Invalid or expired token")

    return _current_user(state, claims)


def _current_user(state, claims) -> CurrentUser:
    return CurrentUser(
        id=state.id,
        email=state.email,
        full_name=state.full_name,
        role=state.role,
        status=state.status,
        plan_id=state.plan_id,
        token_version=state.token_version,
        mfa_enabled=state.mfa_enabled,
        created_at=state.created_at,
        aal=claims.aal,
        session_id=claims.session_id,
    )


async def require_admin(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Admins only, and only with a two-factor session (aal=2).

    The role comes from the database, not the token. An admin whose session
    was not opened with TOTP (or whose MFA was reset) is refused.
    """
    if current.role != "admin":
        raise forbidden("admin_required", "Admin access required.")
    if current.aal < 2 or not current.mfa_enabled:
        raise forbidden("admin_mfa_required", "Sign in again with two-factor authentication.")
    return current
