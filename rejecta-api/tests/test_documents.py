"""Working copies: keep a copy, calibrate a section from it, apply to the paper, undo, download."""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
from contextlib import asynccontextmanager
from collections import Counter
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient

from auth.errors import ApiError
from models import DocumentCalibrateRequest
from services.documents import docx_ops, service
from services.documents.pdf_to_docx import _add_text_paragraph
from services.documents.store import MemoryDocumentStore, Owner
from services.llm.base import AbstractAnalysisProvider

OWNER = Owner("user", uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER = Owner("user", uuid.UUID("22222222-2222-2222-2222-222222222222"))

INTRO_1 = "Self-reporting systems are used in medical materials and in imaging of many kinds today."
INTRO_2 = "Pyrene fluorescence is quenched by nitroxide radicals through space at short distances."
RESULTS_1 = "The copolymer P1 was obtained by free radical copolymerisation with a molar mass of 8700 g mol."
CAPTION = "Fig. 1 SEC traces of the polymers P1, P2 and P3."
RESULTS_2 = "Oxidation gave the open-shell copolymer P2, whose fluorescence is strongly quenched."


def make_paper() -> bytes:
    doc = Document()
    doc.add_heading("Introduction", level=1)
    p = doc.add_paragraph("Self-reporting systems are used in medical materials")
    p.add_run("1").font.superscript = True
    p.add_run(" and in imaging of many kinds today.")
    doc.add_paragraph(INTRO_2)
    doc.add_heading("Results", level=1)
    doc.add_paragraph(RESULTS_1)
    doc.add_paragraph(CAPTION)
    doc.add_paragraph(RESULTS_2)
    doc.add_heading("References", level=1)
    doc.add_paragraph("1. A. Author, J. Chem., 2001, 1, 100-110.")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


SECTIONS = [
    ("introduction", "Self-reporting systems are used in medical materials1 and in imaging of many kinds today.\n\n" + INTRO_2, False),
    ("results", RESULTS_1 + "\n\n" + CAPTION + "\n\n" + RESULTS_2, False),
]


class FakeLLM(AbstractAnalysisProvider):
    """Rewrites the masked text it gets, keeping every token, unless told to drop one."""

    def __init__(self, rewrite=lambda text: text.replace("are used in", "serve"), drop_tokens: bool = False) -> None:
        self.rewrite = rewrite
        self.drop_tokens = drop_tokens
        self.prompts: list[str] = []

    async def analyse(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(user_prompt)
        masked = user_prompt.split("\n\n", 1)[1].split("\n\nCRITICAL FIX", 1)[0]
        out = self.rewrite(masked)
        if self.drop_tokens:
            out = re.sub(r"__REJ_INV_\d{4}__", "", out)
        return json.dumps({"calibrated_masked_text": out, "modifications": ["tightened"]})


def run(coro):
    return asyncio.run(coro)


def new_copy(store: MemoryDocumentStore) -> str:
    info = run(service.create_working_copy(store, OWNER, make_paper(), "paper.docx", None, SECTIONS))
    return info.id


def request(**overrides: Any) -> DocumentCalibrateRequest:
    values: dict[str, Any] = {"journal": "Polymer Chemistry", "field": "Polymer chemistry"}
    values.update(overrides)
    return DocumentCalibrateRequest(**values)


def paper_paragraphs(store: MemoryDocumentStore, document_id: str) -> list:
    stored = run(store.get(OWNER, uuid.UUID(document_id)))
    return Document(io.BytesIO(stored.docx)).paragraphs


# ------------------------------------------------------------------ creating the copy

def test_sections_of_the_analysis_are_found_in_the_copy() -> None:
    store = MemoryDocumentStore()
    info = run(service.create_working_copy(store, OWNER, make_paper(), "paper.docx", None, SECTIONS))
    assert info.source_format == "docx" and info.version == 1 and not info.can_undo
    assert [(s.id, s.name, s.mapped, s.paragraphs) for s in info.sections] == [
        ("s1", "introduction", True, 2),
        ("s2", "results", True, 3),
    ]


def test_a_section_that_is_not_in_the_copy_is_marked_unmapped() -> None:
    entries = service.map_sections(["Some paragraph of text that is in the copy."], [("ghost", "Text that is nowhere in this document at all.", False)])
    assert entries[0]["start"] == -1


def test_the_last_section_stops_before_the_references() -> None:
    texts = ["Introduction", "First section text that is long enough to be found.", "References", "1. A. Author, 2001."]
    entries = service.map_sections(texts, [("intro", "First section text that is long enough to be found.", True)])
    assert (entries[0]["start"], entries[0]["end"]) == (1, 1)


# ------------------------------------------------------------------ calibrate, apply, reject

def test_calibrating_takes_the_text_from_the_copy_and_changes_nothing_yet() -> None:
    store = MemoryDocumentStore()
    document_id = new_copy(store)
    llm = FakeLLM()
    proposal = run(service.calibrate_section(store, OWNER, uuid.UUID(document_id), "s1", request(), llm))
    assert proposal.proposal_id is not None
    assert proposal.original_text.startswith("Self-reporting systems are used in medical materials1")
    assert proposal.proposed_text.startswith("Self-reporting systems serve medical materials1")
    assert "__REJ_INV_5000__" in llm.prompts[0]  # the superscript went to the model as a protected token
    assert paper_paragraphs(store, document_id)[1].text.startswith("Self-reporting systems are used in")  # not applied yet


def test_applying_writes_the_text_into_the_paper_and_keeps_the_superscript() -> None:
    store = MemoryDocumentStore()
    document_id = new_copy(store)
    proposal = run(service.calibrate_section(store, OWNER, uuid.UUID(document_id), "s1", request(), FakeLLM()))
    result = run(service.apply_proposal(store, OWNER, uuid.UUID(document_id), uuid.UUID(proposal.proposal_id)))
    assert result.document.version == 2 and result.document.can_undo
    paragraph = paper_paragraphs(store, document_id)[1]
    assert paragraph.text == "Self-reporting systems serve medical materials1 and in imaging of many kinds today."
    assert [(r.text, r.font.superscript) for r in paragraph.runs if r.font.superscript] == [("1", True)]
    assert result.section_text.startswith("Self-reporting systems serve")


def test_captions_inside_a_section_are_kept_and_later_sections_move_when_paragraphs_are_added() -> None:
    store = MemoryDocumentStore()
    document_id = new_copy(store)
    split = FakeLLM(rewrite=lambda t: t.replace("Pyrene fluorescence", "Extra paragraph here.\n\nPyrene fluorescence"))
    proposal = run(service.calibrate_section(store, OWNER, uuid.UUID(document_id), "s1", request(), split))
    result = run(service.apply_proposal(store, OWNER, uuid.UUID(document_id), uuid.UUID(proposal.proposal_id)))
    assert result.applied_paragraphs == 3
    sections = {s.id: s for s in result.document.sections}
    assert sections["s1"].paragraphs == 3
    results = run(service.calibrate_section(store, OWNER, uuid.UUID(document_id), "s2", request(), FakeLLM()))
    assert results.original_text.startswith(RESULTS_1[:30])  # still the right paragraphs after the shift
    assert results.notes == ["1 caption(s) in this section are kept exactly as they are."]
    assert CAPTION not in results.original_text


def test_a_proposal_can_be_applied_only_once_and_a_rejected_one_not_at_all() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    proposal = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM()))
    run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(proposal.proposal_id)))
    with pytest.raises(ApiError) as again:
        run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(proposal.proposal_id)))
    assert again.value.status_code == 409
    other = run(service.calibrate_section(store, OWNER, document_id, "s2", request(), FakeLLM()))
    run(service.reject_proposal(store, OWNER, document_id, uuid.UUID(other.proposal_id)))
    with pytest.raises(ApiError):
        run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(other.proposal_id)))


def test_a_proposal_for_a_section_that_changed_since_is_refused_as_stale() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    first = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM()))
    second = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM(rewrite=lambda t: t.replace("today", "now"))))
    run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(first.proposal_id)))
    with pytest.raises(ApiError) as stale:
        run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(second.proposal_id)))
    assert stale.value.code == "section_changed"


def test_a_proposal_for_another_section_still_applies_after_the_paper_changed() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    intro = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM()))
    results = run(service.calibrate_section(store, OWNER, document_id, "s2", request(), FakeLLM(rewrite=lambda t: t.replace("Oxidation gave", "Oxidation yielded"))))
    run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(intro.proposal_id)))
    applied = run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(results.proposal_id)))
    assert applied.document.version == 3
    texts = [p.text for p in paper_paragraphs(store, str(document_id))]
    assert any(t.startswith("Oxidation yielded") for t in texts) and CAPTION in texts


def test_a_calibration_that_loses_a_protected_citation_is_blocked_and_gives_nothing_to_apply() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    proposal = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM(drop_tokens=True)))
    assert proposal.proposal_id is None and proposal.proposed_text is None
    assert proposal.calibration.status == "blocked"


def test_warnings_point_at_shorter_text_placeholders_and_new_numbers() -> None:
    warnings = service.proposal_warnings("one two three four five six seven eight nine ten 8700", "one [AUTHOR TO ADD: yield] 95")
    assert any("shorter" in w for w in warnings)
    assert any("AUTHOR TO ADD" in w for w in warnings)
    assert any("95" in w for w in warnings)


# ------------------------------------------------------------------ long sections, undo, download, owners

def test_a_long_section_is_calibrated_in_parts() -> None:
    doc = Document()
    doc.add_heading("Introduction", level=1)
    long_paragraphs = [f"Paragraph {i} " + "with plenty of ordinary words in it " * 60 for i in range(4)]
    for text in long_paragraphs:
        doc.add_paragraph(text)
    buffer = io.BytesIO()
    doc.save(buffer)
    store = MemoryDocumentStore()
    info = run(service.create_working_copy(store, OWNER, buffer.getvalue(), "long.docx", None, [("introduction", "\n\n".join(long_paragraphs), False)]))
    document_id = uuid.UUID(info.id)
    first = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM()))
    assert first.next_from is not None and first.next_from < 4
    applied = run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(first.proposal_id)))
    rest = run(service.calibrate_section(store, OWNER, document_id, "s1", request(from_paragraph=applied.next_from), FakeLLM()))
    assert rest.original_text.startswith(f"Paragraph {first.next_from} ")


def test_undo_goes_back_one_version_and_stops_at_the_original() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    proposal = run(service.calibrate_section(store, OWNER, document_id, "s1", request(), FakeLLM()))
    run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(proposal.proposal_id)))
    info = run(service.undo(store, OWNER, document_id))
    assert info.version == 1
    assert paper_paragraphs(store, str(document_id))[1].text.startswith("Self-reporting systems are used in")
    with pytest.raises(ApiError) as nothing:
        run(service.undo(store, OWNER, document_id))
    assert nothing.value.code == "nothing_to_undo"


def test_download_names_the_file_after_the_upload_and_version() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    data, filename = run(service.download(store, OWNER, document_id))
    assert filename == "paper-rejecta-v1.docx" and data[:2] == b"PK"


def test_another_account_cannot_see_or_change_the_copy() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    with pytest.raises(ApiError) as missing:
        run(service.calibrate_section(store, OTHER, document_id, "s1", request(), FakeLLM()))
    assert missing.value.status_code == 404
    assert run(store.get(OTHER, document_id)) is None
    assert run(store.delete(OTHER, document_id)) is False


# ------------------------------------------------------------------ PDF rebuild: superscripts only where the PDF had them

def test_only_citation_numbers_the_pdf_printed_as_superscript_become_superscript() -> None:
    doc = Document()
    _add_text_paragraph(doc, "Used in medical materials1 and in polymer P2 with step3 here.", Counter({("erials", "1"): 1}))
    runs = doc.paragraphs[0].runs
    assert [(r.text, bool(r.font.superscript)) for r in runs if r.font.superscript] == [("1", True)]
    assert doc.paragraphs[0].text == "Used in medical materials1 and in polymer P2 with step3 here."


# ------------------------------------------------------------------ HTTP

@pytest.fixture
def client(monkeypatch):
    import main
    from auth.subject import get_subject
    from auth.usage import Subject
    from routers import documents as routes

    store = MemoryDocumentStore()

    @asynccontextmanager
    async def no_quota(subject, feature):
        yield

    monkeypatch.setattr(routes, "consume", no_quota)
    monkeypatch.setattr(routes, "get_provider_cached", lambda: FakeLLM())
    subject = Subject(kind="user", id=OWNER.id, plan_id=uuid.uuid4(), label="t@example.com")
    main.app.dependency_overrides[get_subject] = lambda: subject
    main.app.dependency_overrides[routes.get_document_store] = lambda: store
    yield TestClient(main.app), store
    main.app.dependency_overrides.clear()


def test_the_endpoints_calibrate_apply_download_undo_and_delete(client) -> None:
    http, store = client
    document_id = new_copy(store)
    proposal = http.post(f"/documents/{document_id}/sections/s1/calibrate", json={"journal": "Polymer Chemistry"})
    assert proposal.status_code == 200, proposal.text
    applied = http.post(f"/documents/{document_id}/proposals/{proposal.json()['proposal_id']}/apply")
    assert applied.status_code == 200 and applied.json()["document"]["version"] == 2
    download = http.get(f"/documents/{document_id}/download")
    assert download.status_code == 200 and "rejecta-v2.docx" in download.headers["content-disposition"]
    assert http.post(f"/documents/{document_id}/undo").json()["version"] == 1
    assert http.delete(f"/documents/{document_id}").status_code == 204
    assert http.get(f"/documents/{document_id}").status_code == 404


def test_a_section_id_that_does_not_exist_is_a_404(client) -> None:
    http, store = client
    document_id = new_copy(store)
    response = http.post(f"/documents/{document_id}/sections/s9/calibrate", json={"journal": "Polymer Chemistry"})
    assert response.status_code == 404


def test_fix_with_calibrator_starts_at_the_paragraph_the_rejection_reason_points_to() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    evidence = "Oxidation gave the open-shell copolymer P2"
    proposal = run(service.calibrate_section(store, OWNER, document_id, "s2", request(evidence=evidence), FakeLLM()))
    assert proposal.original_text.startswith("Oxidation gave the open-shell copolymer P2")
    assert "free radical copolymerisation" not in proposal.original_text
    applied = run(service.apply_proposal(store, OWNER, document_id, uuid.UUID(proposal.proposal_id)))
    assert applied.applied_paragraphs == 1
    texts = [p.text for p in paper_paragraphs(store, str(document_id))]
    assert RESULTS_1 in texts  # the paragraphs before the evidence are untouched


def test_evidence_that_is_not_in_the_section_starts_at_its_beginning() -> None:
    store = MemoryDocumentStore()
    document_id = uuid.UUID(new_copy(store))
    proposal = run(service.calibrate_section(store, OWNER, document_id, "s2", request(evidence="words that appear nowhere in this paper at all"), FakeLLM()))
    assert proposal.original_text.startswith(RESULTS_1[:30])
