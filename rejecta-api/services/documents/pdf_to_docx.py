"""A working Word copy of a PDF paper: one column, the paper's text, headings, references and figures.

This is not a copy of the journal layout. It is an editable manuscript built from what the parser read,
so that calibrated sections can be written back into it:
  * paragraphs and headings come from the parser's body text;
  * citation numbers the PDF prints as superscript (it marks them in its font data) become real superscripts;
  * each figure or scheme is cut from the page as an image and placed before its caption (best effort);
  * equations stay as text, and tables are not rebuilt.
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter

import fitz
from docx import Document
from docx.shared import Inches, Pt

from models import ParsedDocument
from services.calibrator.vault import ScientificVault
from services.documents.docx_ops import save_docx
from services.parser import _heading_title, _leading_cover_pages, _normalize_pdf_text

logger = logging.getLogger(__name__)

SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit for superscript
_MARKER_TEXT = re.compile(r"^[\d,–\-a-z ]{1,12}$")
_CAPTION_START = re.compile(r"^(?:Fig(?:ure)?s?\.?|Scheme)\s*\d+[A-Za-z]?\b", re.IGNORECASE)
MAX_FIGURES = 30
MAX_FIGURE_BYTES = 2_500_000
MIN_FIGURE_HEIGHT = 40.0  # points; anything flatter is a rule or a header, not a figure
FIGURE_DPI = 150
NOTE = (
    "Working copy made by Rejecta from the PDF: one column, without the journal layout. "
    "Check figures, schemes, equations and tables against the original before submitting."
)


def _key(text: str) -> str:
    """Letters and digits only, ligatures expanded, so PDF spans and parsed text compare equal."""
    return re.sub(r"[^a-z0-9]+", "", _normalize_pdf_text(text).lower())


def superscript_markers(pdf: fitz.Document, first_page: int) -> Counter[tuple[str, str]]:
    """(last letters before the mark, the mark) for every citation number the PDF prints as superscript."""
    found: Counter[tuple[str, str]] = Counter()
    for page in pdf.pages(first_page):
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                before = ""
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    mark = text.strip()
                    if span.get("flags", 0) & SUPERSCRIPT_FLAG and _MARKER_TEXT.match(mark) and re.search(r"\d", mark):
                        found[(_key(before)[-6:], mark.replace(" ", ""))] += 1
                    before += text
    return found


def figure_images(pdf: fitz.Document, first_page: int) -> dict[str, bytes]:
    """Picture of each figure or scheme, keyed by the start of its caption.

    The figure is the area above the caption, across the caption's width, up to the nearest text above it.
    Only areas that contain a drawing or an image are kept.
    """
    images: dict[str, bytes] = {}
    for page in pdf.pages(first_page):
        blocks = [b for b in page.get_text("blocks") if b[6] == 0]
        drawn = [d["rect"] for d in page.get_drawings()] + [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
        for x0, y0, x1, y1, text, _, _ in blocks:
            caption = re.sub(r"\s+", " ", text).strip()
            if not _CAPTION_START.match(caption) or len(images) >= MAX_FIGURES:
                continue
            if x1 - x0 > page.rect.width * 0.6:  # a caption across both columns: the figure is too
                x0, x1 = page.rect.x0 + 30, page.rect.x1 - 30
            above = [b[3] for b in blocks if b[3] <= y0 - 1 and b[2] > x0 and b[0] < x1]
            top = max(above) if above else page.rect.y0 + 30
            area = fitz.Rect(x0, top + 2, x1, y0 - 2)
            if area.height < MIN_FIGURE_HEIGHT or not any(area.intersects(r) for r in drawn):
                continue
            try:
                png = page.get_pixmap(clip=area, dpi=FIGURE_DPI).tobytes("png")
            except Exception:  # an odd page must not stop the copy
                logger.debug("Could not render figure area", exc_info=True)
                continue
            if len(png) <= MAX_FIGURE_BYTES:
                images.setdefault(_key(caption)[:30], png)
    return images


def _add_text_paragraph(document, text: str, markers: Counter[tuple[str, str]]) -> None:
    """A body paragraph whose citation numbers are superscript where the PDF had them so."""
    paragraph = document.add_paragraph()
    position = 0
    for match in ScientificVault._CITATION_MARKER_PATTERN.finditer(text):
        mark = match.group(1)
        key = (_key(text[: match.start(1)])[-6:], mark.replace(" ", ""))
        if markers[key] <= 0:
            continue
        markers[key] -= 1
        paragraph.add_run(text[position : match.start(1)])
        paragraph.add_run(mark).font.superscript = True
        position = match.end(1)
    paragraph.add_run(text[position:])


def build_working_copy(pdf_bytes: bytes, parsed: ParsedDocument) -> bytes:
    """The Word working copy for a parsed PDF."""
    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        first = _leading_cover_pages([page.get_text() for page in pdf])
        markers = superscript_markers(pdf, first)
        figures = figure_images(pdf, first)
    finally:
        pdf.close()

    document = Document()
    note = document.add_paragraph()
    note_run = note.add_run(NOTE)
    note_run.italic = True
    note_run.font.size = Pt(8)

    for block in parsed.body.split("\n\n"):
        text = block.strip()
        if not text:
            continue
        if _heading_title(text) is not None and len(text) <= 60:
            document.add_heading(text, level=1)
            continue
        # a caption can sit inside a longer paragraph of the PDF text, so look for it anywhere in the paragraph
        paragraph_key = _key(text)
        for caption_key in [k for k in figures if k in paragraph_key]:
            document.add_picture(io.BytesIO(figures.pop(caption_key)), width=Inches(6))
        _add_text_paragraph(document, text, markers)

    if parsed.references:
        document.add_heading("References", level=1)
        for number, reference in enumerate(parsed.references, start=1):
            document.add_paragraph(f"{number}. {reference}")
    return save_docx(document)
