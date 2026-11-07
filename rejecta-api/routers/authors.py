"""Author profiles for the upload page (ORCID -> publication record)."""

import logging

from fastapi import APIRouter, Depends, Query, Response

from auth import limits
from auth.dependencies import CurrentUser, get_current_user
from auth.errors import ApiError, too_many_requests
from services.authors import AuthorLookupError, AuthorProfileResponse, get_author_profile, normalise_orcid

logger = logging.getLogger("rejecta.authors")

router = APIRouter(prefix="/authors", tags=["authors"])


@router.get("/profile", response_model=AuthorProfileResponse)
async def author_profile(
    response: Response,
    orcid: str = Query(..., min_length=1, max_length=80, description="ORCID iD, bare or as a URL"),
    current: CurrentUser = Depends(get_current_user),
) -> AuthorProfileResponse:
    """Publication record for one ORCID. Signed-in users only; not metered against a plan.

    Results are stored in the database and shared by all users, so repeat lookups
    do not call ORCID or OpenAlex again until the stored record expires.
    """
    wait = limits.authors_per_user.hit(str(current.id))
    if wait:
        raise too_many_requests(wait, "Too many author lookups. Try again in a minute.")

    normalised = normalise_orcid(orcid)
    if normalised is None:
        raise ApiError(400, "invalid_orcid", "That is not a valid ORCID iD (expected 0000-0000-0000-0000).")

    try:
        result = await get_author_profile(normalised)
    except AuthorLookupError as exc:
        logger.warning("author lookup failed orcid=%s: %s", normalised, exc)
        raise ApiError(
            502,
            "author_source_unavailable",
            "ORCID or OpenAlex did not answer. Try again in a moment.",
        ) from exc

    response.headers["Cache-Control"] = "private, max-age=300"
    return result
