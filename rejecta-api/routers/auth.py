"""Authentication routes: signup, login, refresh, logout, password change.

Browser flow: the access token (15 min) is returned in the JSON body and
kept in memory by the app; the refresh token is only ever an HttpOnly
cookie. In production the browser reaches these routes through the Netlify
proxy at /api/auth/*, so the cookie is first-party to the app's domain.

Admins must also pass TOTP (see routers/auth_mfa.py); a password alone
never gives an admin a session.
"""

from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from auth import limits, mfa, user_cache
from auth.cookies import clear_refresh_cookie, read_refresh_cookie, require_web_client, set_refresh_cookie
from auth.dependencies import CurrentUser, bearer_scheme, get_current_user
from auth.errors import ApiError, service_unavailable, too_many_requests, unauthorized
from auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_new_password,
    verify_dummy,
    verify_password,
)
from auth.request_meta import request_meta
from auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    MfaChallengeResponse,
    ResetPasswordRequest,
    SignupRequest,
    UserPublic,
)
from auth.sessions import (
    IssuedSession,
    RefreshFailure,
    end_session,
    hash_refresh_token,
    looks_like_refresh_token,
    rotate_session,
    start_session,
)
from auth.tokens import (
    AccessTokenError,
    TokenConfigError,
    decode_access_token,
    get_keyring,
    issue_access_token,
    issue_mfa_token,
)
from config import settings
from db.pool import DatabaseUnavailableError, get_pool
from db.repos import audit, refresh_tokens, users
from db.repos.users import AuthState, EmailTakenError

INVALID_CREDENTIALS = "Invalid email or password."


async def require_auth_ready() -> asyncpg.Pool:
    """Fail with 503 before doing any work if keys or the database are missing."""
    try:
        get_keyring()
        return get_pool()
    except (TokenConfigError, DatabaseUnavailableError) as exc:
        raise service_unavailable() from exc


router = APIRouter(prefix="/auth", tags=["auth"])
cookie_route = [Depends(require_web_client)]


def _auth_response(user: AuthState, session: IssuedSession, response: Response) -> AuthResponse:
    token, expires_in = issue_access_token(
        user_id=user.id,
        role=user.role,
        token_version=user.token_version,
        session_id=session.session_id,
        aal=session.aal,
    )
    set_refresh_cookie(response, session.refresh_token, session.expires_at)
    response.headers["Cache-Control"] = "no-store"
    return AuthResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserPublic(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,  # type: ignore[arg-type]
            created_at=user.created_at,
        ),
    )


def _weak_password(exc: PasswordPolicyError) -> ApiError:
    return ApiError(400, "weak_password", str(exc))


@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=cookie_route,
)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthResponse:
    """Create an account (role=user, default plan) and sign in."""
    meta = request_meta(request)
    try:
        validate_new_password(body.password, email=body.email)
    except PasswordPolicyError as exc:
        raise _weak_password(exc) from exc

    wait = limits.signup_per_ip.hit(meta.ip_key)
    if wait:
        raise too_many_requests(wait, "Too many sign-ups from this network. Try again later.")

    password_hash = await hash_password(body.password)
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                user = await users.create_user(
                    conn,
                    email=body.email,
                    password_hash=password_hash,
                    full_name=body.full_name.strip(),
                )
                await users.record_login_success(conn, user.id)
                session = await start_session(conn, user.auth_state, aal=1, meta=meta)
                await audit.record(
                    conn,
                    action="auth.signup",
                    actor_id=user.id,
                    target_type="user",
                    target_id=user.id,
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                )
        except EmailTakenError as exc:
            raise ApiError(409, "email_taken", "An account with this email already exists.") from exc
    return _auth_response(user.auth_state, session, response)


@router.post("/login", response_model=AuthResponse | MfaChallengeResponse, dependencies=cookie_route)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthResponse | MfaChallengeResponse:
    """Sign in with email and password.

    Users get tokens immediately. Admins get ``mfa_required: true`` and an
    ``mfa_token`` instead, and must complete /auth/mfa/verify (or, the first
    time, /auth/mfa/enroll/start + /auth/mfa/enroll/confirm).
    """
    meta = request_meta(request)
    email_key = body.email.strip().lower()

    wait = limits.login_per_ip.hit(meta.ip_key)
    if wait:
        raise too_many_requests(wait, "Too many login attempts. Try again later.")
    wait = limits.login_failures_per_email.retry_after(email_key)
    if wait:
        raise too_many_requests(wait, "Too many failed attempts. Try again later.")

    async with pool.acquire() as conn:
        user = await users.get_by_email(conn, email_key)
        if user is None:
            await verify_dummy(body.password)
            limits.login_failures_per_email.record(email_key)
            raise unauthorized("invalid_credentials", INVALID_CREDENTIALS)

        now = datetime.now(timezone.utc)
        if user.locked_until is not None and user.locked_until > now:
            raise too_many_requests(
                (user.locked_until - now).total_seconds(),
                "Too many failed attempts. Try again later.",
            )

        if not await verify_password(user.password_hash, body.password):
            limits.login_failures_per_email.record(email_key)
            async with conn.transaction():
                await users.record_login_failure(
                    conn,
                    user.id,
                    max_failures=settings.LOGIN_MAX_FAILURES,
                    lockout_minutes=settings.LOGIN_LOCKOUT_MINUTES,
                )
                await audit.record(
                    conn,
                    action="auth.login_failed",
                    actor_type="system",
                    target_type="user",
                    target_id=user.id,
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                )
            raise unauthorized("invalid_credentials", INVALID_CREDENTIALS)

        if user.status != "active":
            raise ApiError(403, "account_suspended", "This account is suspended. Contact support.")

        if user.role == "admin":
            # Admins never get a session from a password alone: hand out a
            # 5-minute MFA token and finish at /auth/mfa/verify (or enroll).
            try:
                mfa.ensure_configured()
            except mfa.MfaConfigError as exc:
                raise service_unavailable() from exc
            purpose = "verify" if user.mfa_enabled else "enroll"
            mfa_token, mfa_expires_in = issue_mfa_token(
                user_id=user.id, token_version=user.token_version, purpose=purpose
            )
            await audit.record(
                conn,
                action="auth.mfa_challenge_issued",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                after={"purpose": purpose},
                ip=meta.ip,
                user_agent=meta.user_agent,
            )
            limits.login_failures_per_email.reset(email_key)
            response.headers["Cache-Control"] = "no-store"
            return MfaChallengeResponse(
                mfa_enrollment_required=not user.mfa_enabled,
                mfa_token=mfa_token,
                expires_in=mfa_expires_in,
            )

        async with conn.transaction():
            await users.record_login_success(conn, user.id)
            session = await start_session(conn, user.auth_state, aal=1, meta=meta)
            await audit.record(
                conn,
                action="auth.login",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                ip=meta.ip,
                user_agent=meta.user_agent,
            )

    limits.login_failures_per_email.reset(email_key)
    return _auth_response(user.auth_state, session, response)


@router.post("/refresh", response_model=AuthResponse, dependencies=cookie_route)
async def refresh(
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthResponse:
    """Rotate the refresh cookie and return a new access token."""
    meta = request_meta(request)
    wait = limits.refresh_per_ip.hit(meta.ip_key)
    if wait:
        raise too_many_requests(wait, "Too many requests. Try again shortly.")

    raw = read_refresh_cookie(request)
    if not raw:
        raise unauthorized("missing_refresh_token", "Not signed in.")

    async with pool.acquire() as conn:
        result = await rotate_session(conn, raw, meta)

    if result.failure is RefreshFailure.SUPERSEDED:
        # Another tab refreshed a moment ago; its new cookie is already in the
        # browser. Do not clear it: the client should simply retry once.
        raise unauthorized(RefreshFailure.SUPERSEDED.value, "Session was refreshed elsewhere. Retry.")
    if result.failure is not None or result.user is None or result.session is None:
        error = unauthorized(RefreshFailure.INVALID.value, "Session expired. Please sign in again.")
        error.headers["Set-Cookie"] = _expired_cookie_header()
        raise error

    return _auth_response(result.user, result.session, response)


def _expired_cookie_header() -> str:
    """Set-Cookie value that deletes the refresh cookie (for error responses)."""
    probe = Response()
    clear_refresh_cookie(probe)
    return probe.headers["set-cookie"]


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=cookie_route)
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> Response:
    """End this tab's sign-in. Always succeeds.

    The refresh cookie is shared by every tab of the browser. If the caller sends
    its access token and the cookie belongs to a *different* sign-in (another tab
    signed in as someone else), the cookie is left alone: one tab must never
    sign another tab's account out.
    """
    meta = request_meta(request)
    raw = read_refresh_cookie(request)
    own_session = None
    if credentials is not None:
        try:
            own_session = decode_access_token(credentials.credentials).session_id
        except AccessTokenError:
            own_session = None
    async with pool.acquire() as conn:
        if own_session is not None and looks_like_refresh_token(raw):
            found = await refresh_tokens.family_of(conn, hash_refresh_token(raw or ""))
            if found is not None and found[0] != own_session:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        async with conn.transaction():
            user_id = await end_session(conn, read_refresh_cookie(request))
            if user_id is not None:
                await audit.record(
                    conn,
                    action="auth.logout",
                    actor_id=user_id,
                    target_type="user",
                    target_id=user_id,
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(response)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> Response:
    """Sign out every device: revokes all sessions and all access tokens."""
    meta = request_meta(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            revoked = await refresh_tokens.revoke_all_for_user(conn, current.id, "logout_all")
            await users.bump_token_version(conn, current.id)
            await audit.record(
                conn,
                action="auth.logout_all",
                actor_id=current.id,
                target_type="user",
                target_id=current.id,
                after={"revoked_sessions": revoked},
                ip=meta.ip,
                user_agent=meta.user_agent,
            )
    user_cache.invalidate(current.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(response)
    return response


@router.post("/change-password", response_model=AuthResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    current: CurrentUser = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> AuthResponse:
    """Change password. Signs out every other device; this one gets a new session."""
    meta = request_meta(request)
    user_key = str(current.id)
    wait = limits.password_change_per_user.hit(user_key)
    if wait:
        raise too_many_requests(wait, "Too many attempts. Try again later.")

    try:
        validate_new_password(body.new_password, email=current.email)
    except PasswordPolicyError as exc:
        raise _weak_password(exc) from exc

    async with pool.acquire() as conn:
        user = await users.get_by_id(conn, current.id)
        if user is None:
            raise unauthorized()
        if not await verify_password(user.password_hash, body.current_password):
            raise ApiError(400, "invalid_current_password", "Current password is incorrect.")
        if await verify_password(user.password_hash, body.new_password):
            raise ApiError(400, "weak_password", "New password must be different from the current one.")

        new_hash = await hash_password(body.new_password)
        async with conn.transaction():
            await users.update_password(conn, user.id, new_hash)  # bumps token_version
            await refresh_tokens.revoke_all_for_user(conn, user.id, "password_changed")
            fresh = await users.get_auth_state(conn, user.id)
            assert fresh is not None
            session = await start_session(conn, fresh, aal=current.aal, meta=meta)
            await audit.record(
                conn,
                action="auth.password_changed",
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                ip=meta.ip,
                user_agent=meta.user_agent,
            )
    user_cache.invalidate(current.id)
    return _auth_response(fresh, session, response)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT, dependencies=cookie_route)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    pool: asyncpg.Pool = Depends(require_auth_ready),
) -> Response:
    """Set a new password with a one-time link created by an admin.

    The link works once and for 24 hours. Every existing session of the user
    is ended and the account is unlocked. The user then signs in normally
    (admins still need their authenticator code).
    """
    from routers.admin_common import RESET_TOKEN_PREFIX, hash_reset_token

    meta = request_meta(request)
    wait = limits.password_reset_per_ip.hit(meta.ip_key)
    if wait:
        raise too_many_requests(wait, "Too many attempts. Try again later.")
    invalid = ApiError(400, "invalid_reset_token", "This link is invalid or has expired. Ask an admin for a new one.")
    if not body.token.startswith(RESET_TOKEN_PREFIX):
        raise invalid

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, u.email FROM password_reset_tokens t JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = $1 AND t.used_at IS NULL AND t.expires_at > now()
                FOR UPDATE OF t
                """,
                hash_reset_token(body.token),
            )
            if row is None:
                raise invalid
            try:
                validate_new_password(body.new_password, email=row["email"])
            except PasswordPolicyError as exc:
                raise _weak_password(exc) from exc
            await users.update_password(conn, row["user_id"], await hash_password(body.new_password))
            await conn.execute("UPDATE password_reset_tokens SET used_at = now() WHERE id = $1", row["id"])
            await refresh_tokens.revoke_all_for_user(conn, row["user_id"], "password_reset")
            await audit.record(
                conn, action="auth.password_reset", actor_id=row["user_id"], target_type="user",
                target_id=row["user_id"], ip=meta.ip, user_agent=meta.user_agent,
            )
    user_cache.invalidate(row["user_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
