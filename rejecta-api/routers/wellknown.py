"""Public signing keys (JWKS), so other services can verify Rejecta tokens."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from auth.errors import service_unavailable
from auth.tokens import TokenConfigError, get_keyring

router = APIRouter(tags=["auth"])


@router.get("/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    try:
        keys = get_keyring().jwks()
    except TokenConfigError as exc:
        raise service_unavailable() from exc
    return JSONResponse(keys, headers={"Cache-Control": "public, max-age=300"})
