"""Working copies: keep an editable copy, calibrate one section, apply or reject the result, undo, download.

Everything happens on the server. The client never sends the paper's text: it names a document and a
section, the server reads that section from its own copy, calibrates it, and stores the result as a
proposal. Applying a proposal writes it into the copy as a new version, only if the section is still
exactly as it was when it was calibrated.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import PurePath
from typing import Any

from auth.errors import ApiError
from models import (
    ApplyResult,
    DocumentCalibrateRequest,
    DocumentInfo,
    DocumentProposal,
    DocumentSection,
    ParsedDocument,
    VoiceCalibrateResponse,
)
from services.analysis_guard import alnum
from services.calibrator import VoiceCalibratorService
from services.documents import docx_ops
from services.documents.pdf_to_docx import build_working_copy
from services.documents.store import DocumentStore, Owner, StoredDocument, VersionConflict
from services.parser import BACK_MATTER_TITLES, REFERENCE_TITLES, _heading_title, detect_format

logger = logging.getLogger(__name__)

SELECTION_CHARS = 6000  # prose sent in one calibration, so the rewrite fits in the model's answer
_MATCH_CHARS = 60
_TAIL_SLACK = 400  # letters the copy may differ by from the analysed text before the section's end
SHORTER_WARNING_RATIO = 0.6
PDF_NOTE = (
    "This copy was converted from the PDF, preserving its layout, tables and figures. "
    "Check complex equations and page breaks before submitting."
)


# --------------------------------------------------------------------------- shapes

def document_info(stored: StoredDocument) -> DocumentInfo:
    sections = [
        DocumentSection(
            id=s["id"], name=s["name"], mapped=s["start"] >= 0,
            paragraphs=(s["end"] - s["start"] + 1) if s["start"] >= 0 else 0,
        )
        for s in stored.sections
    ]
    return DocumentInfo(
        id=str(stored.id), filename=stored.filename, source_format=stored.source_format, version=stored.version,
        can_undo=stored.version > 1, expires_at=stored.expires_at.isoformat(), sections=sections,
        notes=[PDF_NOTE] if stored.source_format == "pdf" else [],
    )


def calibration_response(result) -> VoiceCalibrateResponse:
    return VoiceCalibrateResponse(
        calibrated_text=result.calibrated_text,
        original_text=result.original_text,
        status=result.status,
        input_type=result.input_type,
        invariants_verified=result.invariants_verified,
        total_detected_invariants=result.total_detected_invariants,
        total_restored_invariants=result.total_restored_invariants,
        word_count_original=result.word_count_original,
        word_count_calibrated=result.word_count_calibrated,
        synthetic_markers_removed=result.synthetic_markers_removed,
        intensity_applied=result.intensity_applied,
        audit_notes=result.audit_notes,
        block_reasons=result.block_reasons,
        guidance_items=result.guidance_items,
    )


# --------------------------------------------------------------------------- creating a copy

def map_sections(texts: list[str], sections: list[tuple[str, str, bool]]) -> list[dict[str, Any]]:
    """Paragraph range of each analysed section in the copy: [{id, name, start, end}], start -1 when not found.

    `sections` is (name, content, content_truncated) in the order of the analysis. A section starts at the
    paragraph holding the first words of its content and ends at the paragraph holding its last words,
    or just before the next section, or before the references.
    """
    corpus_chars: list[str] = []
    owner_of: list[int] = []
    for index, text in enumerate(texts):
        for char in alnum(text):
            corpus_chars.append(char)
            owner_of.append(index)
    corpus = "".join(corpus_chars)

    def find(piece: str, start: int = 0) -> int:
        position = corpus.find(piece, start) if len(piece) >= 20 else -1
        if position < 0 and len(piece) >= 24:
            position = corpus.find(piece[:24], start)
        return position

    entries: list[dict[str, Any]] = []
    for number, (name, content, truncated) in enumerate(sections, start=1):
        entry: dict[str, Any] = {"id": f"s{number}", "name": name, "start": -1, "end": -1}
        letters = alnum(content.rstrip(" …"))
        position = find(letters[:_MATCH_CHARS])
        if position >= 0:
            entry["start"] = owner_of[position]
            if not truncated:
                tail = letters[-_MATCH_CHARS:]
                # the last words sit about one section-length after the first ones; searching from there
                # keeps a phrase that repeats earlier in the section from cutting it short
                expected = position + len(letters) - len(tail)
                tail_at = corpus.find(tail, max(position, expected - _TAIL_SLACK))
                if tail_at >= 0:
                    entry["end"] = owner_of[tail_at + len(tail) - 1]
        entries.append(entry)

    mapped = sorted((e for e in entries if e["start"] >= 0), key=lambda e: e["start"])
    stop = len(texts) - 1
    for index, text in enumerate(texts):  # the last section ends before the references or back matter
        if mapped and index > mapped[-1]["start"] and _heading_title(text) in set(REFERENCE_TITLES) | BACK_MATTER_TITLES:
            stop = index - 1
            break
    for i, entry in enumerate(mapped):
        limit = mapped[i + 1]["start"] - 1 if i + 1 < len(mapped) else stop
        if entry["end"] < entry["start"] or entry["end"] > limit:
            entry["end"] = limit
        if entry["end"] < entry["start"]:  # two sections start in the same paragraph: keep the first
            entry["start"] = entry["end"] = -1
    return entries


async def create_working_copy(
    store: DocumentStore,
    owner: Owner,
    file_bytes: bytes,
    filename: str,
    parsed: ParsedDocument,
    sections: list[tuple[str, str, bool]],
) -> DocumentInfo:
    """Keep an editable Word copy of an analysed upload and link the analysis sections to its paragraphs."""
    source_format = detect_format(file_bytes)
    if source_format == "docx":
        docx = file_bytes
    elif source_format == "pdf":
        docx = await asyncio.to_thread(build_working_copy, file_bytes, parsed)
    else:
        raise ApiError(400, "unsupported_document", "Only PDF and Word (.docx) files can be kept as a working copy.")
    document = docx_ops.open_docx(docx)
    texts = [docx_ops.paragraph_text(p) for p in docx_ops.body_paragraphs(document)]
    stored = await store.create(owner, PurePath(filename or "manuscript").name, source_format, docx, map_sections(texts, sections))
    return document_info(stored)


# --------------------------------------------------------------------------- calibrating a section

async def _load(store: DocumentStore, owner: Owner, document_id: uuid.UUID) -> StoredDocument:
    stored = await store.get(owner, document_id)
    if stored is None:
        raise ApiError(404, "document_not_found", "This working copy does not exist or has expired (copies are kept 24 hours).")
    return stored


def _section(stored: StoredDocument, section_id: str) -> dict[str, Any]:
    for section in stored.sections:
        if section["id"] == section_id:
            if section["start"] < 0:
                raise ApiError(409, "section_not_in_copy", f'"{section["name"]}" could not be found in the working copy.')
            return section
    raise ApiError(404, "section_not_found", "No such section in this working copy.")


def _prose(paragraphs: list, section: dict[str, Any]) -> tuple[list[int], dict[str, int]]:
    """Indices of the section's prose paragraphs, and how many of each other kind it has."""
    prose: list[int] = []
    kept: dict[str, int] = {}
    for index in range(section["start"], section["end"] + 1):
        kind = docx_ops.paragraph_kind(paragraphs[index])
        if kind == "text":
            prose.append(index)
        elif kind not in ("empty", "page_number"):
            kept[kind] = kept.get(kind, 0) + 1
    return prose, kept


def evidence_start(paragraphs: list, section: dict[str, Any], evidence: str) -> int:
    """How many prose paragraphs of the section come before the one holding `evidence` (0 if not found)."""
    wanted = alnum(evidence)[:48]
    if len(wanted) < 20:
        return 0
    prose, _ = _prose(paragraphs, section)
    for number, index in enumerate(prose):
        if wanted in alnum(docx_ops.paragraph_text(paragraphs[index])):
            return number
    return 0


def select_paragraphs(paragraphs: list, section: dict[str, Any], skip: int) -> tuple[list[int], list[str], int | None]:
    """Prose paragraphs of the section to calibrate now: (indices, notes, where the next part starts or None)."""
    prose, kept = _prose(paragraphs, section)
    chosen: list[int] = []
    used = 0
    for index in prose[skip:]:
        length = len(docx_ops.paragraph_text(paragraphs[index]))
        if chosen and used + length > SELECTION_CHARS:
            break
        chosen.append(index)
        used += length
    notes = [f"{count} {kind}(s) in this section are kept exactly as they are." for kind, count in kept.items()]
    next_from = skip + len(chosen) if skip + len(chosen) < len(prose) else None
    if next_from is not None:
        notes.append(f"Long section: paragraphs {skip + 1}-{next_from} of {len(prose)} are calibrated now. Calibrate again for the rest.")
    return chosen, notes, next_from


def proposal_warnings(original: str, proposed: str) -> list[str]:
    """What the user should look at before applying the text."""
    warnings: list[str] = []
    before, after = len(original.split()), len(proposed.split())
    if before and after < SHORTER_WARNING_RATIO * before:
        warnings.append(
            f"The new text is {round(100 * (1 - after / before))}% shorter ({before} to {after} words). "
            "Check that no finding, condition or reference was dropped."
        )
    placeholders = proposed.count("[AUTHOR TO ADD")
    if placeholders:
        warnings.append(f"{placeholders} place(s) are marked [AUTHOR TO ADD: ...]. Fill them in before submitting.")
    old_numbers = set(re.findall(r"\d+(?:\.\d+)?", original))
    new_numbers = sorted(set(re.findall(r"\d+(?:\.\d+)?", proposed)) - old_numbers)
    if new_numbers:
        warnings.append("Numbers that are not in the original text: " + ", ".join(new_numbers[:8]) + ".")
    return warnings


async def calibrate_section(
    store: DocumentStore,
    owner: Owner,
    document_id: uuid.UUID,
    section_id: str,
    request: DocumentCalibrateRequest,
    llm,
) -> DocumentProposal:
    stored = await _load(store, owner, document_id)
    section = _section(stored, section_id)
    document = docx_ops.open_docx(stored.docx)
    paragraphs = docx_ops.body_paragraphs(document)
    skip = request.from_paragraph
    if skip == 0 and request.evidence:
        # "Fix with Calibrator" on a rejection reason: start at the paragraph the reason is about
        skip = evidence_start(paragraphs, section, request.evidence)
    selected, notes, next_from = select_paragraphs(paragraphs, section, skip)
    if not selected:
        raise ApiError(422, "nothing_to_calibrate", "This part of the section has no prose paragraphs to calibrate.")

    selection = docx_ops.tokenize([paragraphs[i] for i in selected])
    original_text = docx_ops.unmask_for_display(selection.masked_text, selection.token_text)
    result = await VoiceCalibratorService(llm).calibrate(
        text=selection.masked_text,
        journal_name=request.journal,
        field=request.field,
        intensity=request.intensity,
        focus=request.focus or f"Section: {section['name']}.",
        extra_invariants=selection.token_text,
    )
    calibration = calibration_response(result)
    if result.calibrated_masked_text is None or result.calibrated_text is None:
        return DocumentProposal(
            proposal_id=None, section_id=section_id, base_version=stored.version, original_text=original_text,
            proposed_text=None, warnings=[], notes=notes, next_from=next_from, calibration=calibration,
        )

    warnings = proposal_warnings(original_text, result.calibrated_text)
    texts = [docx_ops.paragraph_text(p) for p in paragraphs]
    payload = {
        "range_start": section["start"],
        "range_texts": texts[section["start"] : section["end"] + 1],
        "selected": selected,
        "from_paragraph": skip,
        "masked_output": result.calibrated_masked_text,
        "format_tokens": selection.tokens,
        "plain_tokens": {k: v for k, v in result.vault_values.items() if k not in selection.tokens},
        "original_text": original_text,
        "proposed_text": result.calibrated_text,
        "has_more": next_from is not None,
    }
    proposal_id = await store.save_proposal(owner, document_id, section_id, stored.version, payload)
    return DocumentProposal(
        proposal_id=str(proposal_id), section_id=section_id, base_version=stored.version, original_text=original_text,
        proposed_text=result.calibrated_text, warnings=warnings, notes=notes, next_from=next_from, calibration=calibration,
    )


# --------------------------------------------------------------------------- applying, rejecting, undo

async def _open_proposal(store: DocumentStore, owner: Owner, document_id: uuid.UUID, proposal_id: uuid.UUID):
    proposal = await store.get_proposal(owner, document_id, proposal_id)
    if proposal is None:
        raise ApiError(404, "proposal_not_found", "No such proposal for this working copy.")
    if proposal.status != "open":
        raise ApiError(409, "proposal_closed", f"This proposal was already {proposal.status}.")
    return proposal


async def apply_proposal(store: DocumentStore, owner: Owner, document_id: uuid.UUID, proposal_id: uuid.UUID) -> ApplyResult:
    """Write a proposal into the paper as a new version."""
    proposal = await _open_proposal(store, owner, document_id, proposal_id)
    stored = await _load(store, owner, document_id)
    section = _section(stored, proposal.section_id)
    payload = proposal.payload
    document = docx_ops.open_docx(stored.docx)
    paragraphs = docx_ops.body_paragraphs(document)
    texts = [docx_ops.paragraph_text(p) for p in paragraphs]

    # The section must be exactly as it was when its text was calibrated; otherwise the proposal is out of date.
    if texts[section["start"] : section["end"] + 1] != payload["range_texts"]:
        await store.set_proposal_status(owner, document_id, proposal_id, "stale")
        raise ApiError(409, "section_changed", "This section changed after it was calibrated. Calibrate it again.")
    offset = section["start"] - payload["range_start"]
    selected = [index + offset for index in payload["selected"]]

    new_paragraphs = docx_ops.build_paragraphs(
        [paragraphs[i] for i in selected], payload["masked_output"], payload["format_tokens"], payload["plain_tokens"]
    )
    if not new_paragraphs:
        raise ApiError(422, "empty_proposal", "The calibrated text is empty.")
    delta = docx_ops.replace_paragraphs(document, selected, new_paragraphs)

    sections = []
    for entry in stored.sections:
        entry = dict(entry)
        if entry["id"] == section["id"]:
            entry["end"] += delta
        elif entry["start"] > section["end"]:
            entry["start"] += delta
            entry["end"] += delta
        sections.append(entry)
    try:
        saved = await store.add_version(
            owner, document_id, stored.version, docx_ops.save_docx(document), sections, f'{section["name"]} calibrated'
        )
    except VersionConflict as exc:
        raise ApiError(409, "document_changed", "The paper was changed at the same moment. Try again.") from exc
    await store.set_proposal_status(owner, document_id, proposal_id, "accepted")

    updated = docx_ops.body_paragraphs(docx_ops.open_docx(saved.docx))
    current = next(s for s in saved.sections if s["id"] == section["id"])
    section_text = "\n\n".join(
        t for t in (docx_ops.paragraph_text(updated[i]) for i in range(current["start"], current["end"] + 1)) if t
    )
    next_from = payload["from_paragraph"] + len(new_paragraphs) if payload.get("has_more") else None
    return ApplyResult(
        document=document_info(saved), section_id=section["id"], applied_paragraphs=len(new_paragraphs),
        section_text=section_text, next_from=next_from,
    )


async def reject_proposal(store: DocumentStore, owner: Owner, document_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
    await _open_proposal(store, owner, document_id, proposal_id)
    await store.set_proposal_status(owner, document_id, proposal_id, "rejected")


async def undo(store: DocumentStore, owner: Owner, document_id: uuid.UUID) -> DocumentInfo:
    await _load(store, owner, document_id)
    previous = await store.drop_latest_version(owner, document_id)
    if previous is None:
        raise ApiError(409, "nothing_to_undo", "There is no earlier version to go back to.")
    return document_info(previous)


async def download(store: DocumentStore, owner: Owner, document_id: uuid.UUID) -> tuple[bytes, str]:
    stored = await _load(store, owner, document_id)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", PurePath(stored.filename).stem)[:80] or "manuscript"
    return stored.docx, f"{stem}-rejecta-v{stored.version}.docx"
