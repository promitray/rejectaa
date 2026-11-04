"""API errors with a stable machine-readable ``code``.

Response body: ``{"detail": "<human message>", "code": "<code>", ...extra}``.
``detail`` stays a plain string so the existing frontend error handling
(which reads ``detail``) keeps working.
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra or {}


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """FastAPI exception handler for :class:`ApiError`."""
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code, **exc.extra},
        headers=exc.headers,
    )


def unauthorized(code: str = "invalid_token", detail: str = "Not authenticated") -> ApiError:
    return ApiError(401, code, detail, headers={"WWW-Authenticate": "Bearer"})


def forbidden(code: str, detail: str) -> ApiError:
    return ApiError(403, code, detail)


def too_many_requests(retry_after_seconds: float, detail: str) -> ApiError:
    seconds = max(1, int(retry_after_seconds + 0.999))
    return ApiError(
        429,
        "rate_limited",
        detail,
        headers={"Retry-After": str(seconds)},
        extra={"retry_after_seconds": seconds},
    )


def service_unavailable(detail: str = "Authentication is temporarily unavailable.") -> ApiError:
    return ApiError(503, "auth_unavailable", detail)
