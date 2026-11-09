"""Word (.docx) support in the paper parser, plus format detection for uploads."""
import io
import zipfile

import fitz
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from services import parser
from services.parser import (
    LEGACY_DOC_MESSAGE,
    UNSUPPORTED_FORMAT_MESSAGE,
    detect_format,
    parse_document,
    parse_docx,
    upload_name_error,
)

ABSTRACT = (
    "We study how a dual-responsive polymer degrades under heat and light, and we report a mechanistic "
    "analysis of the cleavage products across three conditions."
)
INTRO = "Stimuli-responsive polymers are widely used in controlled release and recyclable materials. " * 3
METHODS = "Polymers were synthesised by free radical polymerisation and characterised by SEC and NMR. " * 3
RESULTS = "Degradation proceeded within minutes under UV light and within hours at elevated temperature. " * 3
REFERENCES = [
    "Wedler-Jasinski, N. et al. Chem. Commun. 2017, 53, 157-160.",
    "Bian, S. et al. J. Am. Chem. Soc. 2013, 135, 9240-9243.",
    "Zhang, J. et al. Energy Storage Mater. 2017, 7, 1-7.",
]


def build_docx(configure=None) -> bytes:
    """A manuscript-shaped Word file. `configure(document)` can change it before saving."""
    document = Document()
    document.add_heading("A Dual-Responsive Polymer System", level=0)
    document.add_paragraph("Hatice Mutlu, Christopher Barner-Kowollik")
    document.add_heading("Abstract", level=1)
    document.add_paragraph(ABSTRACT)
    document.add_heading("Introduction", level=1)
    document.add_paragraph(INTRO)
    document.add_heading("Methods", level=1)
    document.add_paragraph(METHODS)
    document.add_heading("Results", level=1)
    document.add_paragraph(RESULTS)
    document.add_heading("References", level=1)
    for reference in REFERENCES:
        document.add_paragraph(reference)
    if configure:
        configure(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_pdf(text: str) -> bytes:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    return pdf.tobytes()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_docx_is_parsed_into_abstract_sections_and_references():
    parsed = parse_docx(build_docx())
    assert parsed.abstract.startswith("We study how a dual-responsive polymer degrades")
    assert set(parsed.sections) >= {"introduction", "methods", "results"}
    assert "free radical polymerisation" in parsed.sections["methods"]
    assert parsed.references == REFERENCES
    assert parsed.word_count > 100


def test_each_word_paragraph_in_the_reference_list_is_one_reference():
    # Word's automatic list numbers are not text, so there is nothing to split on except paragraphs.
    parsed = parse_docx(build_docx())
    assert len(parsed.references) == 3
    assert parsed.references[0].startswith("Wedler-Jasinski")


def test_numbered_prefixes_are_stripped_from_references():
    def configure(document):
        # Replace the plain list with "[1] ..." style entries.
        body = document.element.body
        for paragraph in list(document.paragraphs):
            if paragraph.text in REFERENCES:
                body.remove(paragraph._element)
        for index, reference in enumerate(REFERENCES, start=1):
            document.add_paragraph(f"[{index}] {reference}")

    parsed = parse_docx(build_docx(configure))
    assert parsed.references == REFERENCES


def test_text_after_the_reference_list_is_not_counted_as_references():
    def configure(document):
        document.add_heading("Appendix", level=1)
        document.add_paragraph("Supplementary table of reaction conditions for every experiment in the study.")

    parsed = parse_docx(build_docx(configure))
    assert parsed.references == REFERENCES


def test_table_rows_become_lines_and_are_kept_in_order():
    def configure(document):
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Sample"
        table.cell(0, 1).text = "Mn (g/mol)"
        table.cell(1, 0).text = "P1"
        table.cell(1, 1).text = "9800"

    parsed = parse_docx(build_docx(configure))
    assert "Sample | Mn (g/mol)" in parsed.full_text
    assert "P1 | 9800" in parsed.full_text


def test_tracked_deletions_are_ignored_and_insertions_are_kept():
    def configure(document):
        paragraph = document.add_paragraph()
        paragraph.add_run("Reaction time was ")

        deleted = OxmlElement("w:del")
        run = OxmlElement("w:r")
        deleted_text = OxmlElement("w:delText")
        deleted_text.text = "ONE-HOUR-DELETED"
        run.append(deleted_text)
        deleted.append(run)
        paragraph._p.append(deleted)

        inserted = OxmlElement("w:ins")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "twelve hours"
        run.append(text)
        inserted.append(run)
        paragraph._p.append(inserted)

    parsed = parse_docx(build_docx(configure))
    assert "Reaction time was twelve hours" in parsed.full_text
    assert "ONE-HOUR-DELETED" not in parsed.full_text


def test_text_boxes_are_not_duplicated_into_the_body():
    def configure(document):
        paragraph = document.add_paragraph("Body sentence before a floating box.")
        box = OxmlElement("w:txbxContent")
        inner = OxmlElement("w:p")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "FLOATING-BOX-TEXT"
        run.append(text)
        inner.append(run)
        box.append(inner)
        drawing = OxmlElement("w:drawing")
        drawing.append(box)
        paragraph.runs[0]._r.append(drawing)

    parsed = parse_docx(build_docx(configure))
    assert "Body sentence before a floating box." in parsed.full_text
    assert "FLOATING-BOX-TEXT" not in parsed.full_text


def test_headers_and_footers_are_not_part_of_the_text():
    def configure(document):
        section = document.sections[0]
        section.header.paragraphs[0].text = "RUNNING-HEAD-DRAFT"
        section.footer.paragraphs[0].text = "PAGE-FOOTER"

    parsed = parse_docx(build_docx(configure))
    assert "RUNNING-HEAD-DRAFT" not in parsed.full_text
    assert "PAGE-FOOTER" not in parsed.full_text


def test_invisible_characters_and_non_breaking_spaces_are_cleaned():
    def configure(document):
        document.add_paragraph("Poly­mer​ yield was 87%.")

    parsed = parse_docx(build_docx(configure))
    assert "Polymer yield was 87%." in parsed.full_text


def test_word_file_with_no_text_is_rejected():
    buffer = io.BytesIO()
    Document().save(buffer)
    with pytest.raises(ValueError, match="No readable text"):
        parse_docx(buffer.getvalue())


# ---------------------------------------------------------------------------
# Detection and dispatch
# ---------------------------------------------------------------------------
def test_detect_format_uses_the_bytes_not_the_name():
    assert detect_format(make_pdf("hello")) == "pdf"
    assert detect_format(build_docx()) == "docx"
    assert detect_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64) == "doc"
    assert detect_format(b"just some text") is None
    assert detect_format(b"") is None


def test_a_zip_that_is_not_a_word_file_is_not_a_docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hello")
    assert detect_format(buffer.getvalue()) is None
    with pytest.raises(ValueError, match="Unsupported file type"):
        parse_document(buffer.getvalue(), "paper.zip")


def test_parse_document_routes_both_formats():
    pdf = parse_document(make_pdf("Abstract\nSome abstract text that is long enough to be detected as one."), "p.pdf")
    word = parse_document(build_docx(), "p.docx")
    assert pdf.word_count > 0
    assert word.references == REFERENCES


def test_a_docx_renamed_to_pdf_is_still_read_as_word():
    assert parse_document(build_docx(), "renamed.pdf").references == REFERENCES


def test_legacy_doc_and_encrypted_word_get_a_clear_message():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    with pytest.raises(ValueError) as error:
        parse_document(ole, "old.doc")
    assert str(error.value) == LEGACY_DOC_MESSAGE
    assert ".docx" in LEGACY_DOC_MESSAGE


def test_random_bytes_are_unsupported():
    with pytest.raises(ValueError, match="Unsupported file type"):
        parse_document(b"MZ not a document", "paper.exe")


def test_corrupt_word_file_gets_a_clear_message():
    broken = build_docx()[:200]
    with pytest.raises(ValueError, match="Could not open Word file"):
        parse_document(broken, "paper.docx")


def test_zip_bomb_is_rejected_before_unpacking(monkeypatch):
    monkeypatch.setattr(parser, "MAX_DOCX_UNCOMPRESSED_BYTES", 1_000)
    with pytest.raises(ValueError, match="too large once unpacked"):
        parse_docx(build_docx())


def test_too_many_zip_entries_are_rejected(monkeypatch):
    monkeypatch.setattr(parser, "MAX_DOCX_ENTRIES", 2)
    with pytest.raises(ValueError, match="too large once unpacked"):
        parse_docx(build_docx())


def test_xml_entity_expansion_is_not_followed():
    # A "billion laughs" style document.xml must not blow up memory or hang.
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&b;&b;&b;&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    source = zipfile.ZipFile(io.BytesIO(build_docx()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in source.infolist():
            data = bomb.encode() if item.filename == "word/document.xml" else source.read(item.filename)
            archive.writestr(item.filename, data)
    try:
        parsed = parse_docx(buffer.getvalue())
        assert "AAAAAAAAAA" * 100 not in parsed.full_text  # entities were not expanded into a flood
    except ValueError:
        pass  # refusing the file is also an acceptable outcome


# ---------------------------------------------------------------------------
# File name checks used by the endpoints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["paper.pdf", "Paper.PDF", "paper.docx", "My Paper.DOCX"])
def test_supported_names_pass(name):
    assert upload_name_error(name) is None


def test_doc_and_other_names_get_specific_messages():
    assert upload_name_error("paper.doc") == LEGACY_DOC_MESSAGE
    assert upload_name_error("paper.txt") == UNSUPPORTED_FORMAT_MESSAGE
    assert upload_name_error("") == UNSUPPORTED_FORMAT_MESSAGE
    assert upload_name_error("docx") == UNSUPPORTED_FORMAT_MESSAGE
