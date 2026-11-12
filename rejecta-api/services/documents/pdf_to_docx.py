"""A working Word copy of a PDF paper that preserves the original layout, fonts, tables and figures.

Converts the PDF in-memory via ``pdf2docx`` (multi-column sections, typography, tables, and embedded
figures/drawings) and post-processes merged heading/line-break paragraphs so headings keep their own
style and stay separate from calibratable prose. Falls back to a plain parsed rebuild if layout
conversion fails or produces an empty document.
"""

from __future__ import annotations

import copy
import io
import logging
import re
from collections import Counter

import fitz
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from models import ParsedDocument
from services.calibrator.vault import ScientificVault
from services.documents.docx_ops import W_P, W_PPR, W_R, W_RPR, W_T, _is_toggle_on, save_docx
from services.parser import _heading_title, _leading_cover_pages, _normalize_pdf_text

logger = logging.getLogger(__name__)

SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit for superscript
_MARKER_TEXT = re.compile(r"^[\d,–\-a-z ]{1,12}$")
_CAPTION_START = re.compile(
    r"^(?:Fig(?:ure)?s?\.?|Scheme|Table|Chart|Graph)\s*S?\d+[A-Za-z]?\b",
    re.IGNORECASE,
)
_NUMBERED_HEADING_PREFIX = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]{1,4}\.)\s*[A-Z]")
MAX_FIGURES = 30
MAX_FIGURE_BYTES = 2_500_000
MIN_FIGURE_HEIGHT = 40.0  # points; anything flatter is a rule or a header, not a figure
FIGURE_DPI = 150
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W_BR = _W_NS + "br"
_W_SECT_PR = _W_NS + "sectPr"
_MAX_HEADING_PREFIX_CHARS = 140
_MIN_TRAILING_PROSE_CHARS = 20
NOTE = (
    "Working copy made by Rejecta from the PDF: layout, tables and figures are preserved where possible. "
    "Check complex equations and page breaks before submitting."
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

    The figure is the area above the caption, across the caption's width, up to the nearest non-empty
    text block above it. Only areas that contain a drawing or an image are kept.
    """
    images: dict[str, bytes] = {}
    for page in pdf.pages(first_page):
        # Ignore whitespace-only text blocks (e.g. empty anchors around images) so they don't collapse crop height.
        blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
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


def _children_text(elements: list) -> str:
    parts: list[str] = []
    for el in elements:
        for t in el.iter(W_T):
            parts.append(t.text or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _is_soft_break_run(el) -> bool:
    if el.tag != W_R:
        return False
    children = [c for c in el.iterchildren() if c.tag != W_RPR]
    if len(children) != 1 or children[0].tag != _W_BR:
        return False
    br_type = (children[0].get(qn("w:type")) or "").lower()
    return br_type in ("", "textwrapping")


def _all_runs_bold(elements: list) -> bool:
    saw_text = False
    for el in elements:
        if el.tag != W_R:
            continue
        txt = "".join(t.text or "" for t in el.iter(W_T)).strip()
        if not txt:
            continue
        saw_text = True
        if not _is_toggle_on(el.find(W_RPR), "b"):
            return False
    return saw_text


def _first_run_bold(elements: list) -> bool:
    for el in elements:
        if el.tag != W_R:
            continue
        txt = "".join(t.text or "" for t in el.iter(W_T)).strip()
        if txt:
            return _is_toggle_on(el.find(W_RPR), "b")
    return False


def _looks_like_heading_prefix(prefix_els: list, suffix_els: list) -> bool:
    prefix_text = _children_text(prefix_els)
    suffix_text = _children_text(suffix_els)
    if not (1 <= len(prefix_text) <= _MAX_HEADING_PREFIX_CHARS) or len(suffix_text) < _MIN_TRAILING_PROSE_CHARS:
        return False
    if _CAPTION_START.match(prefix_text):
        return False
    if _heading_title(prefix_text) is not None:
        return True
    if prefix_text.endswith((".", "!", "?", ";", ",")):
        return False
    if _NUMBERED_HEADING_PREFIX.match(prefix_text) and len(prefix_text.split()) <= 16:
        return True
    return _all_runs_bold(prefix_els) and not _first_run_bold(suffix_els)


def _split_heading_breaks(docx_bytes: bytes) -> bytes:
    """Split paragraphs where pdf2docx joined a heading and opening prose via a soft ``<w:br/>``."""
    document = Document(io.BytesIO(docx_bytes))
    body = document.element.body
    changed = False
    for p_el in list(body.iterchildren(W_P)):
        children = [c for c in p_el.iterchildren() if c.tag != W_PPR]
        break_idx = next((i for i, c in enumerate(children) if _is_soft_break_run(c)), None)
        if break_idx is None or break_idx == 0:
            continue
        prefix_els = children[:break_idx]
        tail_start = break_idx + 1
        while tail_start < len(children) and _is_soft_break_run(children[tail_start]):
            tail_start += 1
        suffix_els = children[tail_start:]
        if not _looks_like_heading_prefix(prefix_els, suffix_els):
            continue

        heading_p = OxmlElement("w:p")
        ppr = p_el.find(W_PPR)
        if ppr is not None:
            heading_ppr = copy.deepcopy(ppr)
            for sect_pr in list(heading_ppr.iterchildren(_W_SECT_PR)):
                heading_ppr.remove(sect_pr)
            heading_p.append(heading_ppr)
        for el in prefix_els:
            p_el.remove(el)
            heading_p.append(el)
        for br_el in children[break_idx:tail_start]:
            p_el.remove(br_el)
        p_el.addprevious(heading_p)
        changed = True
    return save_docx(document) if changed else docx_bytes


def _convert_with_pdf2docx(pdf_bytes: bytes) -> bytes:
    """Convert PDF bytes to a layout-preserving .docx using pdf2docx."""
    from pdf2docx import Converter

    out = io.BytesIO()
    cv = Converter(stream=pdf_bytes)
    try:
        cv.convert(
            out,
            parse_stream_table=False,
            parse_lattice_table=True,
            delete_end_line_hyphen=True,
            ignore_page_error=True,
        )
    finally:
        cv.close()
    return _split_heading_breaks(out.getvalue())


def _has_usable_content(docx_bytes: bytes) -> bool:
    doc = Document(io.BytesIO(docx_bytes))
    has_text = any(p.text.strip() for p in doc.paragraphs)
    return has_text or bool(doc.tables) or bool(doc.inline_shapes)


def _build_fallback_copy(pdf_bytes: bytes, parsed: ParsedDocument) -> bytes:
    """Plain-text + cropped-figure fallback builder when layout conversion cannot process a PDF."""
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
        paragraph_key = _key(text)
        for caption_key in [k for k in figures if k in paragraph_key]:
            document.add_picture(io.BytesIO(figures.pop(caption_key)), width=Inches(6))
        _add_text_paragraph(document, text, markers)

    if parsed.references:
        document.add_heading("References", level=1)
        for number, reference in enumerate(parsed.references, start=1):
            document.add_paragraph(f"{number}. {reference}")
    return save_docx(document)


def build_working_copy(pdf_bytes: bytes, parsed: ParsedDocument) -> bytes:
    """The Word working copy for a parsed PDF, preserving layout, tables and figures via pdf2docx."""
    try:
        docx_bytes = _convert_with_pdf2docx(pdf_bytes)
        if _has_usable_content(docx_bytes) or not (parsed.body.strip() or parsed.references):
            return docx_bytes
        logger.warning("pdf2docx produced an empty document for non-empty PDF; falling back to parsed rebuild")
    except Exception:
        logger.warning("pdf2docx conversion failed; falling back to parsed rebuild", exc_info=True)
    return _build_fallback_copy(pdf_bytes, parsed)
