"""Admin two-factor authentication (TOTP).

Flow after ``POST /auth/login`` returns ``mfa_required: true``:

* First time (``mfa_enrollment_required: true``):
  ``/auth/mfa/enroll/start`` returns a secret + otpauth URI (QR code), then
  ``/auth/mfa/enroll/confirm`` with a code from the app enables MFA, signs
  the admin in (aal=2) and returns 10 recovery codes, shown once.
* Afterwards: ``/auth/mfa/verify`` with a 6-digit code, or a recovery code.

Protections: the MFA token is bound to the user's token_version (a password
or role change voids it), expires in 5 minutes and is single-use; 5 code
attempts per token and 10 failures per admin per 15 minutes; a TOTP step
can never be used twice (replay); recovery codes are single-use.
"""

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from auth import limits, mfa, user_cache
from auth.cookies import require_web_client
from auth.dependencies import CurrentUser, require_admin
from auth.errors import ApiError, service_unavailable, too_many_requests, unauthorized
from auth.request_meta import RequestMeta, request_meta
from auth.schemas import (
    AuthResponse,
    AuthWithRecoveryCodes,
    MfaCodeRequest,
    MfaEnrollStartResponse,
    MfaTokenRequest,
    MfaVerifyRequest,
    RecoveryCodesResponse,
)
from auth.sessions import start_session
from auth.tokens import AccessTokenError, MfaClaims, decode_mfa_token
from config import settings
from db.repos import audit, users
from db.repos import mfa as mfa_repo
from db.repos.users import UserRecord
from routers.auth import _auth_response, require_auth_ready

router = APIRouter(prefix="/auth/mfa", tags=["auth"])
cookie_route = [Depends(require_web_client)]


def _invalid_token() -> ApiError:
    return unauthorized("invalid_mfa_token", "Two-factor step expired. Sign in again.")


def _require_configured() -> None:
    try:
        mfa.ensure_configured()
    except mfa.MfaConfigError as exc:
        raise service_unavailable() from exc


async def _load_challenge(conn: asyncpg.Connection, token: str, purpose: str) -> tuple[UserRecord, MfaClaims]:
    try:
        claims = decode_mfa_token(token)
    except AccessTokenError as exc:
        raise _invalid_token() from exc
    if claims.purpose != purpose or limits.mfa_tokens_consumed.retry_after(claims.jti):
        raise _invalid_token()
    user = await users.get_by_id(conn, claims.user_id)
    if (
        user is None
        or user.status != "active"
        or user.role != "admin"
        or user.token_version != claims.token_version
    ):
        raise _invalid_token()
    return user, claims


def _guard_attempt(user: UserRecord, claims: MfaClaims) -> None:
    wait = limits.mfa_failures_per_user.retry_after(str(user.id))
    if wait:
        raise too_many_requests(wait, "Too many incorrect codes. Try again later.")
    wait = limits.mfa_attempts_per_token.hit(claims.jti)
    if wait:
        raise too_many_requests(wait, "Too many attempts. Sign in again.")


async def _record_failure(conn: asyncpg.Connection, user: UserRecord, meta: RequestMeta, method: str) -> ApiError:
    limits.mfa_failures_per_user.record(str(user.id))
    await audit.record(
        conn,
        action="auth.mfa_failed",
        actor_type="system",
        target_type="user",
        target_id=user.id,
        after={"method": method},
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
    return unauthorized("invalid_mfa_code", "That code is not valid.")


def _decrypt(secret_enc: str | None) -> str:
    if not secret_enc:
        raise ApiError(409, "mfa_state_conflict", "Two-factor setup is not in the expected state.")
    try:
        return mfa.decrypt_secret(secret_enc)
    except mfa.MfaConfigError as exc:
        raise service_unavailable() from exc


@router.post("/enroll/start", response_model=MfaEnrollStartResponse, dependencies=cookie_route)
async def enroll_start(
    body: MfaTokenRequest,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> MfaEnrollStartResponse:
    """Create a new (pending) TOTP secret for an admin without MFA."""
    _require_configured()
    async with pool.acquire() as conn:
        user, claims = await _load_challenge(conn, body.mfa_token, "enroll")
        if user.mfa_enabled:
            raise ApiError(409, "mfa_already_enabled", "Two-factor authentication is already set up.")
        _guard_attempt(user, claims)
        secret = mfa.new_secret()
        await mfa_repo.set_pending_secret(conn, user.id, mfa.encrypt_secret(secret))
    response.headers["Cache-Control"] = "no-store"
    return MfaEnrollStartResponse(
        secret=secret,
        otpauth_uri=mfa.provisioning_uri(secret, user.email),
        issuer=settings.MFA_ISSUER_NAME,
        account=user.email,
    )


@router.post("/enroll/confirm", response_model=AuthWithRecoveryCodes, dependencies=cookie_route)
async def enroll_confirm(
    body: MfaCodeRequest,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthWithRecoveryCodes:
    """Confirm the authenticator with one code: enables MFA and signs in (aal=2)."""
    _require_configured()
    meta = request_meta(request)
    async with pool.acquire() as conn:
        user, claims = await _load_challenge(conn, body.mfa_token, "enroll")
        if user.mfa_enabled:
            raise ApiError(409, "mfa_already_enabled", "Two-factor authentication is already set up.")
        if not user.mfa_pending_secret_enc:
            raise ApiError(409, "mfa_enrollment_not_started", "Start two-factor setup first.")
        _guard_attempt(user, claims)

        step = mfa.match_step(_decrypt(user.mfa_pending_secret_enc), body.code)
        if step is None:
            raise await _record_failure(conn, user, meta, "enroll")

        codes = mfa.new_recovery_codes()
        async with conn.transaction():
            if await mfa_repo.activate(conn, user.id, step) is None:
                raise ApiError(409, "mfa_state_conflict", "Two-factor setup changed. Sign in again.")
            await mfa_repo.replace_recovery_codes(conn, user.id, [mfa.hash_recovery_code(c) for c in codes])
            await users.record_login_success(conn, user.id)
            fresh = await users.get_auth_state(conn, user.id)
            assert fresh is not None
            session = await start_session(conn, fresh, aal=2, meta=meta)
            for action in ("auth.mfa_enrolled", "auth.login"):
                await audit.record(
                    conn, action=action, actor_id=user.id, target_type="user", target_id=user.id,
                    after={"aal": 2}, ip=meta.ip, user_agent=meta.user_agent,
                )

    limits.mfa_tokens_consumed.record(claims.jti)
    user_cache.invalidate(user.id)
    auth = _auth_response(fresh, session, response)
    return AuthWithRecoveryCodes(**auth.model_dump(), recovery_codes=codes)


@router.post("/verify", response_model=AuthResponse, dependencies=cookie_route)
async def verify(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthResponse:
    """Finish an admin login with an authenticator code or a recovery code."""
    _require_configured()
    meta = request_meta(request)
    async with pool.acquire() as conn:
        user, claims = await _load_challenge(conn, body.mfa_token, "verify")
        if not user.mfa_enabled:
            raise ApiError(409, "mfa_state_conflict", "Two-factor authentication is not set up.")
        _guard_attempt(user, claims)

        if body.code is not None:
            method = "totp"
            step = mfa.match_step(_decrypt(user.mfa_secret_enc), body.code)
            # claim_step refuses a step at or before the last one used (replay).
            ok = step is not None and await mfa_repo.claim_step(conn, user.id, step)
        else:
            method = "recovery_code"
            ok = await mfa_repo.use_recovery_code(conn, user.id, mfa.hash_recovery_code(body.recovery_code or ""))
        if not ok:
            raise await _record_failure(conn, user, meta, method)

        async with conn.transaction():
            await users.record_login_success(conn, user.id)
            session = await start_session(conn, user.auth_state, aal=2, meta=meta)
            remaining = await mfa_repo.unused_recovery_codes(conn, user.id)
            await audit.record(
                conn, action="auth.login", actor_id=user.id, target_type="user", target_id=user.id,
                after={"aal": 2, "method": method, "recovery_codes_left": remaining},
                ip=meta.ip, user_agent=meta.user_agent,
            )

    limits.mfa_tokens_consumed.record(claims.jti)
    return _auth_response(user.auth_state, session, response)


@router.post("/recovery-codes", response_model=RecoveryCodesResponse)
async def regenerate_recovery_codes(
    request: Request,
    response: Response,
    current: CurrentUser = Depends(require_admin),
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> RecoveryCodesResponse:
    """Replace all recovery codes (old ones stop working). Shown once."""
    _require_configured()
    meta = request_meta(request)
    codes = mfa.new_recovery_codes()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await mfa_repo.replace_recovery_codes(conn, current.id, [mfa.hash_recovery_code(c) for c in codes])
            await audit.record(
                conn, action="auth.mfa_recovery_codes_regenerated", actor_id=current.id,
                target_type="user", target_id=current.id, ip=meta.ip, user_agent=meta.user_agent,
            )
    response.headers["Cache-Control"] = "no-store"
    return RecoveryCodesResponse(recovery_codes=codes)
