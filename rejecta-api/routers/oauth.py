"""OAuth2 client-credentials token endpoint for partner servers (RFC 6749 §4.4).

    POST /oauth/token
    Content-Type: application/x-www-form-urlencoded
    grant_type=client_credentials&client_id=rjc_...&client_secret=rjs_...[&scope=analyse_paper voice_calibrate]

Client credentials may also be sent with HTTP Basic auth. Responses and
errors follow the RFC so standard OAuth libraries work unchanged.
"""

import base64
import binascii
from urllib.parse import unquote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from auth import limits, partners
from auth.request_meta import request_meta
from auth.tokens import TokenConfigError, issue_partner_token
from db.pool import DatabaseUnavailableError, get_pool
from db.repos import audit
from db.repos import partners as partners_repo

router = APIRouter(tags=["partners"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(status: int, error: str, description: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={**NO_STORE, **(headers or {})},
    )


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, secret = decoded.partition(":")
    return (unquote(client_id), unquote(secret)) if sep else None


@router.post("/oauth/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    client_id: str | None = Form(default=None, max_length=100),
    client_secret: str | None = Form(default=None, max_length=200),
    scope: str | None = Form(default=None, max_length=500),
) -> JSONResponse:
    meta = request_meta(request)
    wait = limits.oauth_per_ip.hit(meta.ip_key)
    if wait:
        return _error(429, "slow_down", "Too many token requests.", {"Retry-After": str(int(wait) + 1)})
    if grant_type != "client_credentials":
        return _error(400, "unsupported_grant_type", "Only client_credentials is supported.")

    basic = _basic_credentials(request)
    if basic:
        client_id, client_secret = basic
    if not client_id or not client_secret:
        return _error(401, "invalid_client", "Client authentication failed.", {"WWW-Authenticate": "Basic"})
    if limits.oauth_failures_per_client.retry_after(client_id):
        return _error(429, "slow_down", "Too many failed attempts for this client.")

    try:
        async with get_pool().acquire() as conn:
            client = await partners_repo.authenticate(conn, client_id, partners.hash_secret(client_secret))
            if client is None:
                limits.oauth_failures_per_client.record(client_id)
                return _error(401, "invalid_client", "Client authentication failed.", {"WWW-Authenticate": "Basic"})

            requested = [s for s in (scope or "").split(" ") if s]
            granted = list(client.scopes) if not requested else requested
            if not set(granted) <= set(client.scopes):
                return _error(400, "invalid_scope", "Requested scope is not allowed for this client.")
            access_token, expires_in = issue_partner_token(
                client_uuid=client.id, client_id=client.client_id,
                token_version=client.token_version, scopes=granted,
            )
            await audit.record(
                conn, action="partner.token_issued", actor_type="client", actor_id=client.id,
                target_type="api_client", target_id=client.id, after={"scope": granted},
                ip=meta.ip, user_agent=meta.user_agent,
            )
    except (TokenConfigError, DatabaseUnavailableError):
        return _error(503, "temporarily_unavailable", "Token service unavailable.")

    limits.oauth_failures_per_client.reset(client_id)
    return JSONResponse(
        {"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in, "scope": " ".join(granted)},
        headers=NO_STORE,
    )
