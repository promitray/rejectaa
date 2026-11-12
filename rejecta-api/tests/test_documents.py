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

import fitz
import pytest
from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.shared import Pt
from fastapi.testclient import TestClient

from auth.errors import ApiError
from models import DocumentCalibrateRequest
from services.documents import docx_ops, service
from services.documents.pdf_to_docx import (
    _add_text_paragraph,
    _split_heading_breaks,
    build_working_copy,
    figure_images,
)
from services.documents.store import MemoryDocumentStore, Owner
from services.llm.base import AbstractAnalysisProvider
from services.parser import parse_pdf

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


# ------------------------------------------------------------------ Option B (pdf2docx) & formatting preservation

def _sample_pdf_bytes() -> bytes:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "1. Introduction", fontsize=12, fontname="hebo")
    page.insert_text(
        (72, 92),
        "Self-reporting systems are used in medical materials and in polymer chemistry today.",
        fontsize=10,
        fontname="helv",
    )
    # Embed a raster figure image and draw a bordered 2x2 table plus caption on the page.
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (180, 200, 230))
    page.insert_image(fitz.Rect(72, 130, 260, 220), stream=pix.tobytes("png"))
    page.insert_text((72, 238), "Fig. 1 Schematic diagram of the setup.", fontsize=9, fontname="helv")
    # 2x2 lattice table
    for y in (270, 290, 310):
        page.draw_line((72, y), (272, y))
    for x in (72, 172, 272):
        page.draw_line((x, 270), (x, 310))
    page.insert_text((80, 284), "Sample", fontsize=9, fontname="hebo")
    page.insert_text((180, 284), "Yield", fontsize=9, fontname="hebo")
    page.insert_text((80, 304), "P1", fontsize=9, fontname="helv")
    page.insert_text((180, 304), "92%", fontsize=9, fontname="helv")
    page.insert_text((295, 780), "1", fontsize=9, fontname="helv")
    data = pdf.tobytes()
    pdf.close()
    return data


def test_pdf2docx_working_copy_preserves_tables_drawings_and_splits_heading_breaks() -> None:
    pdf_bytes = _sample_pdf_bytes()
    parsed = parse_pdf(pdf_bytes)
    docx_bytes = build_working_copy(pdf_bytes, parsed)
    doc = Document(io.BytesIO(docx_bytes))

    assert len(doc.tables) == 1
    assert len(doc.inline_shapes) >= 1
    table_text = " | ".join(cell.text.strip() for row in doc.tables[0].rows for cell in row.cells)
    assert "Sample" in table_text and "92%" in table_text
    texts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    assert any(t.startswith("1. Introduction") for t in texts)
    # Heading must not remain glued to the body paragraph via <w:br/>
    assert not any(t.startswith("1. Introduction") and "Self-reporting systems" in t for t in texts)


def test_split_heading_breaks_separates_bold_heading_and_keeps_sect_pr_on_body() -> None:
    doc = Document()
    p = doc.add_paragraph()
    r1 = p.add_run("2.1. Polycondensation ")
    r1.bold = True
    br_run = p.add_run()
    br_run.add_break()
    p.add_run("The seminal synthesis of a polythioester was reported in 1951 by Marvel.")
    ppr = p._element.get_or_add_pPr()
    ppr.append(OxmlElement("w:sectPr"))

    buf = io.BytesIO()
    doc.save(buf)
    split_doc = Document(io.BytesIO(_split_heading_breaks(buf.getvalue())))
    paras = [para for para in split_doc.paragraphs if para.text.strip()]
    assert [para.text.strip() for para in paras] == [
        "2.1. Polycondensation",
        "The seminal synthesis of a polythioester was reported in 1951 by Marvel.",
    ]
    assert paras[0]._element.find(docx_ops.W_PPR + "/" + docx_ops.W_SECT_PR) is None
    assert paras[1]._element.find(docx_ops.W_PPR + "/" + docx_ops.W_SECT_PR) is not None


def test_toggle_off_bold_italic_runs_are_not_tokenized_but_small_scripts_are_kept() -> None:
    p = parse_xml(
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '  <w:r><w:rPr><w:b w:val="0"/><w:i w:val="0"/><w:sz w:val="24"/></w:rPr><w:t xml:space="preserve">The polymer </w:t></w:r>'
        '  <w:r><w:rPr><w:b w:val="0"/><w:i w:val="0"/><w:sz w:val="24"/></w:rPr><w:t xml:space="preserve">and </w:t></w:r>'
        '  <w:r><w:rPr><w:b w:val="0"/><w:i w:val="0"/><w:sz w:val="14"/></w:rPr><w:t>1,2</w:t></w:r>'
        '  <w:r><w:rPr><w:b w:val="0"/><w:i w:val="0"/><w:sz w:val="24"/></w:rPr><w:t xml:space="preserve"> in CO</w:t></w:r>'
        '  <w:r><w:rPr><w:b w:val="0"/><w:i w:val="0"/><w:sz w:val="14"/></w:rPr><w:t>2</w:t></w:r>'
        '</w:p>'
    )
    sel = docx_ops.tokenize([p])
    # "and " (with explicit b=0, i=0) must NOT be tokenized, while sz=14 scripts ("1,2", "2") ARE preserved.
    assert list(sel.token_text.values()) == ["1,2", "2"]
    assert "The polymer and __REJ_INV_5000__ in CO__REJ_INV_5001__" == sel.masked_text


def test_per_paragraph_formatting_and_sect_pr_survive_multi_paragraph_apply() -> None:
    doc = Document()
    doc.add_heading("Introduction", level=1)
    p1 = doc.add_paragraph()
    p1.paragraph_format.left_indent = Pt(18)
    r1 = p1.add_run("First body paragraph has its own indentation and font size.")
    r1.font.size = Pt(11)

    p2 = doc.add_paragraph()
    p2.paragraph_format.left_indent = Pt(36)
    r2 = p2.add_run("Second body paragraph has wider indentation and smaller font.")
    r2.font.size = Pt(9.5)
    p2._element.get_or_add_pPr().append(OxmlElement("w:sectPr"))

    doc.add_heading("References", level=1)
    doc.add_paragraph("1. A. Author, J. Chem., 2001, 1, 100-110.")
    buf = io.BytesIO()
    doc.save(buf)

    store = MemoryDocumentStore()
    info = run(
        service.create_working_copy(
            store,
            OWNER,
            buf.getvalue(),
            "styles.docx",
            None,
            [("introduction", p1.text + "\n\n" + p2.text, False)],
        )
    )
    doc_id = uuid.UUID(info.id)
    llm = FakeLLM(rewrite=lambda t: t.replace("body paragraph", "revised paragraph"))
    proposal = run(service.calibrate_section(store, OWNER, doc_id, "s1", request(), llm))
    run(service.apply_proposal(store, OWNER, doc_id, uuid.UUID(proposal.proposal_id)))

    paras = paper_paragraphs(store, info.id)
    assert paras[1].paragraph_format.left_indent == Pt(18)
    assert paras[1].runs[0].font.size == Pt(11)
    assert paras[2].paragraph_format.left_indent == Pt(36)
    assert paras[2].runs[0].font.size == Pt(9.5)
    # Section break on paragraph 2 is kept exactly once (on p2, never duplicated onto p1).
    assert paras[1]._element.find(docx_ops.W_PPR + "/" + docx_ops.W_SECT_PR) is None
    assert paras[2]._element.find(docx_ops.W_PPR + "/" + docx_ops.W_SECT_PR) is not None


def test_page_numbers_and_bold_numbered_headings_are_skipped_by_calibration() -> None:
    doc = Document()
    doc.add_heading("Introduction", level=1)
    h_sub = doc.add_paragraph()
    r_sub = h_sub.add_run("1.1. Historical Context")
    r_sub.bold = True
    body_p = doc.add_paragraph("Self-reporting systems are used in medical materials for clinical imaging.")
    page_num = doc.add_paragraph("2")
    assert docx_ops.paragraph_kind(h_sub._element) == "heading"
    assert docx_ops.paragraph_kind(page_num._element) == "page_number"
    assert docx_ops.paragraph_kind(body_p._element) == "text"

    buf = io.BytesIO()
    doc.save(buf)
    store = MemoryDocumentStore()
    info = run(
        service.create_working_copy(
            store,
            OWNER,
            buf.getvalue(),
            "headings.docx",
            None,
            [("introduction", "1.1. Historical Context\n\n" + body_p.text + "\n\n2", False)],
        )
    )
    proposal = run(service.calibrate_section(store, OWNER, uuid.UUID(info.id), "s1", request(), FakeLLM()))
    assert proposal.original_text == body_p.text
    assert proposal.notes == ["1 heading(s) in this section are kept exactly as they are."]


def test_fallback_figure_images_ignores_empty_whitespace_blocks() -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.draw_rect(fitz.Rect(72, 100, 300, 220), color=(0, 0, 0), fill=(0.85, 0.85, 0.85))
    # PyMuPDF often emits an invisible whitespace-only block right above the caption.
    page.insert_text((295, 225), "   ", fontsize=10, fontname="helv")
    page.insert_text((72, 240), "Fig. 1 Vector diagram above a whitespace anchor.", fontsize=10, fontname="helv")
    images = figure_images(pdf, 0)
    pdf.close()
    assert any(k.startswith("fig1vectordiagram") for k in images)
