"""Working copies of papers: calibrate a section, apply it to the paper, undo, download.

A copy is created by POST /analyse-paper with keep_copy=true. Signed-in users and partner clients
(other platforms) use the same endpoints; each only ever sees its own copies.
"""

import uuid

from fastapi import APIRouter, Depends, Response

from auth import features
from auth.errors import ApiError
from auth.subject import get_subject
from auth.usage import Subject, consume
from db.pool import DatabaseUnavailableError, get_pool
from models import ApplyResult, DocumentCalibrateRequest, DocumentInfo, DocumentProposal
from services.documents import service
from services.documents.store import DocumentStore, Owner, PostgresDocumentStore
from services.llm import get_provider_cached

router = APIRouter(prefix="/documents", tags=["documents"])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def get_document_store() -> DocumentStore:
    try:
        return PostgresDocumentStore(get_pool())
    except DatabaseUnavailableError as exc:
        raise ApiError(503, "documents_unavailable", "Working copies need the database, which is not configured.") from exc


def owner_of(subject: Subject) -> Owner:
    return Owner(kind=subject.kind, id=subject.id)


@router.get("/{document_id}", response_model=DocumentInfo)
async def get_document(
    document_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> DocumentInfo:
    """The copy's current version and its sections."""
    stored = await store.get(owner_of(subject), document_id)
    if stored is None:
        raise ApiError(404, "document_not_found", "This working copy does not exist or has expired (copies are kept 24 hours).")
    return service.document_info(stored)


@router.post("/{document_id}/sections/{section_id}/calibrate", response_model=DocumentProposal)
async def calibrate_section(
    document_id: uuid.UUID,
    section_id: str,
    body: DocumentCalibrateRequest,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> DocumentProposal:
    """Calibrate a section with the text from the copy itself. Nothing changes in the paper until it is applied.

    Uses the Voice Calibrator quota.
    """
    async with consume(subject, features.VOICE_CALIBRATE):
        return await service.calibrate_section(
            store, owner_of(subject), document_id, section_id, body, get_provider_cached()
        )


@router.post("/{document_id}/proposals/{proposal_id}/apply", response_model=ApplyResult)
async def apply_proposal(
    document_id: uuid.UUID,
    proposal_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> ApplyResult:
    """Write the calibrated text into the paper (a new version)."""
    return await service.apply_proposal(store, owner_of(subject), document_id, proposal_id)


@router.post("/{document_id}/proposals/{proposal_id}/reject", status_code=204)
async def reject_proposal(
    document_id: uuid.UUID,
    proposal_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> Response:
    """Leave the paper as it is."""
    await service.reject_proposal(store, owner_of(subject), document_id, proposal_id)
    return Response(status_code=204)


@router.post("/{document_id}/undo", response_model=DocumentInfo)
async def undo(
    document_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> DocumentInfo:
    """Go back to the version before the last change."""
    return await service.undo(store, owner_of(subject), document_id)


@router.get("/{document_id}/download")
async def download(
    document_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> Response:
    """The current version as a Word file."""
    data, filename = await service.download(store, owner_of(subject), document_id)
    return Response(
        content=data,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    subject: Subject = Depends(get_subject),
    store: DocumentStore = Depends(get_document_store),
) -> Response:
    """Delete the copy and all its versions now, without waiting for it to expire."""
    if not await store.delete(owner_of(subject), document_id):
        raise ApiError(404, "document_not_found", "This working copy does not exist or has expired.")
    return Response(status_code=204)
