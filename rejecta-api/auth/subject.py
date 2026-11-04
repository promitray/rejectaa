"""Who is calling a metered endpoint: a signed-in user or a partner API client.

Tools accept either kind of Bearer token. The token's ``typ`` header picks
the verifier; each verifier checks everything again, so a forged header only
leads to rejection. Partner tokens are accepted ONLY here (tool endpoints),
never on /me, /auth/* or /admin/*.
"""

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from auth import limits, partners
from auth.dependencies import _bearer, get_current_user
from auth.errors import service_unavailable, too_many_requests, unauthorized
from auth.request_meta import request_meta
from auth.tokens import (
    PARTNER_HEADER_TYP,
    AccessTokenError,
    TokenConfigError,
    decode_partner_token,
    token_header_typ,
)
from auth.usage import Subject
from db.pool import DatabaseUnavailableError


async def _partner_subject(token: str) -> Subject:
    try:
        claims = decode_partner_token(token)
        client = await partners.get_client(claims.client_uuid)
    except AccessTokenError as exc:
        raise unauthorized("invalid_token", "Invalid or expired token") from exc
    except (TokenConfigError, DatabaseUnavailableError) as exc:
        raise service_unavailable() from exc
    if client is None or client.status != "active" or client.token_version != claims.token_version:
        raise unauthorized("invalid_token", "Invalid or expired token")
    # Scopes = what the client may use now AND what this token was issued for.
    scopes = frozenset(client.scopes) & frozenset(claims.scopes)
    return Subject(kind="client", id=client.id, plan_id=client.plan_id, label=client.name, scopes=scopes)


async def get_subject(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Subject:
    if credentials is not None and token_header_typ(credentials.credentials) == PARTNER_HEADER_TYP:
        return await _partner_subject(credentials.credentials)
    user = await get_current_user(credentials)
    return Subject(kind="user", id=user.id, plan_id=user.plan_id, label=user.email)


async def public_rate_limit(request: Request) -> None:
    """IP limit for the free, unauthenticated tools (Scout tier)."""
    meta = request_meta(request)
    wait = limits.public_per_ip.hit(meta.ip_key)
    if wait:
        raise too_many_requests(wait, "Too many requests from this network. Try again in a minute.")
