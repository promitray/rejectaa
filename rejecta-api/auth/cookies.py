"""Refresh-token cookie and CSRF guard for cookie-based auth routes.

The cookie is HttpOnly (JavaScript cannot read it), Secure, SameSite=Strict
and scoped to the auth path only, so it is never sent with other requests.

Routes that read or set the cookie also require the header
``X-Rejecta-Client: web`` (a cross-site form cannot set custom headers, and a
cross-origin fetch with one needs a CORS preflight) and, when the browser
sends an ``Origin``, it must be one of the app origins.
"""

from datetime import datetime, timezone

from fastapi import Header, Request, Response

from auth.errors import forbidden
from config import settings

CLIENT_HEADER_VALUE = "web"


def set_refresh_cookie(response: Response, raw: str, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=raw,
        max_age=max_age,
        expires=max_age,
        path=settings.AUTH_COOKIE_PATH,
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.AUTH_COOKIE_NAME,
        path=settings.AUTH_COOKIE_PATH,
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def read_refresh_cookie(request: Request) -> str | None:
    return request.cookies.get(settings.AUTH_COOKIE_NAME)


async def require_web_client(
    request: Request,
    x_rejecta_client: str | None = Header(default=None, description="Must be 'web'"),
) -> None:
    """CSRF guard for routes that use the refresh cookie."""
    if x_rejecta_client != CLIENT_HEADER_VALUE:
        raise forbidden("csrf_check_failed", "Missing X-Rejecta-Client header.")
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in settings.allowed_origins():
        raise forbidden("csrf_check_failed", "Request origin is not allowed.")
