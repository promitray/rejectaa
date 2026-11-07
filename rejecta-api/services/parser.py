"""Document parsing service: extracts structured academic paper content from PDF or Word (.docx)."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections import Counter, defaultdict

import fitz
from docx import Document as open_docx

from models import ParsedDocument
from services import paper_profile
from services.citation_refs import trim_after_citation

logger = logging.getLogger(__name__)

MAX_ABSTRACT_CHARS = 1800
MAX_SECTION_CHARS = 14000
MAX_REFERENCES = 60
MIN_REFERENCE_LENGTH = 20
MAX_FILE_SIZE_BYTES = 10_485_760
FALLBACK_ABSTRACT_CHARS = 1200
MIN_SECTION_CONTENT_LENGTH = 50
FALLBACK_BODY_CHARS = 8000

# Heading titles (matched on a line by themselves, optional leading number)
# mapped to the section the analysis prompt expects.
SECTION_TITLES: dict[str, tuple[str, ...]] = {
    "introduction": ("introduction",),
    "methods": (
        "methods", "method", "methodology", "materials and methods", "methods and materials",
        "model architecture", "approach", "experimental setup",
        "training", "experimental design", "study design",
        # chemistry
        "experimental", "experimental section", "experimental procedures", "experimental details",
        "experimental methods", "materials", "general procedures", "computational details",
        "computational methods",
    ),
    "results": (
        "results", "findings", "experiments", "evaluation",
        "experimental results", "results and discussion", "results and discussions",
    ),
    "discussion": (
        "discussion", "conclusion", "conclusions", "discussion and conclusion",
        "conclusions and future work", "concluding remarks",
        # Conclusion / Outlook
        "outlook", "conclusion and outlook", "conclusions and outlook", "summary and outlook",
        "summary", "conclusion and perspectives", "conclusions and perspectives", "perspectives",
    ),
}
# Headings that end a section without being one we keep.
OTHER_TITLES = (
    "abstract", "background", "related work", "literature review",
    "why self-attention", "acknowledgements", "acknowledgments",
    "acknowledgement", "appendix", "supplementary material", "keywords",
    "index terms", "limitations", "future work",
    # back matter that must not be glued to the last section
    "supporting information", "supplementary information", "supplementary data",
    "electronic supplementary information", "electronic supplementary information (esi)",
    "graphical abstract", "table of contents", "toc", "author contributions",
    "conflicts of interest", "conflict of interest", "competing interests",
    "declaration of competing interest", "data availability", "data availability statement",
    "funding", "author information", "associated content", "abbreviations", "notes",
    "credit authorship contribution statement",
)
REFERENCE_TITLES = ("references", "bibliography", "literature cited", "notes and references")
MAX_THEMATIC_SECTIONS = 8
BODY_FALLBACK_MIN_SECTION_CHARS = 200

_TITLE_TO_SECTION = {t: name for name, titles in SECTION_TITLES.items() for t in titles}
_KNOWN_TITLES = set(_TITLE_TO_SECTION) | set(OTHER_TITLES) | set(REFERENCE_TITLES)
# Optional heading prefix: "2.", "II)", or a letter with punctuation ("D. References", "A) Methods")
_HEADING_NUMBER = r"(?:(?:(?:\d{1,2}|[IVX]{1,4})[\.\)]?|[A-Z][\.\)])\s+)?"
_NUMBER_ONLY = re.compile(r"^\d{1,2}$")
_GENERIC_TITLE = re.compile(r"^[A-Z][A-Za-z\-, ]{2,45}$")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
REF_SPLIT_PATTERN_A = r"\n(?=\[\d{1,3}\])"
REF_SPLIT_PATTERN_B = r"\n(?=\d{1,3}[\.\)]\s+[A-Z])"
REF_SPLIT_PATTERN_C = r"\n\n+"
REFERENCE_PREFIX_PATTERN = r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*"
_REF_END_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\.$")

# Upload formats. The format is decided from the file's content, not its name.
SUPPORTED_EXTENSIONS = (".pdf", ".docx")
UNSUPPORTED_FORMAT_MESSAGE = "Unsupported file type. Upload a PDF or a Word (.docx) file."
LEGACY_DOC_MESSAGE = (
    "Old Word (.doc) files and password-protected Word files are not supported. "
    "In Word use File > Save As > .docx (without a password) and upload that."
)
# A .docx is a zip file. Reject zip bombs before unpacking anything.
MAX_DOCX_UNCOMPRESSED_BYTES = 60_000_000
MAX_DOCX_ENTRIES = 2_000
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M_TEXT = "{http://schemas.openxmlformats.org/officeDocument/2006/math}t"
# Parts of a paragraph that must not contribute text: tracked deletions, text boxes and
# drawings (their text is duplicated in fallbacks), and field instructions.
_DOCX_SKIPPED = frozenset(
    _W + name for name in ("del", "moveFrom", "txbxContent", "drawing", "pict", "object", "instrText")
)
_DOCX_SPACE_TAGS = frozenset(_W + name for name in ("tab", "br", "cr"))
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def has_supported_extension(filename: str) -> bool:
    """True when the file name ends in an accepted extension (.pdf or .docx)."""
    return filename.lower().endswith(SUPPORTED_EXTENSIONS)


def upload_name_error(filename: str) -> str | None:
    """User-facing message when the file name is not an accepted type, else None."""
    if has_supported_extension(filename):
        return None
    if filename.lower().endswith(".doc"):
        return LEGACY_DOC_MESSAGE
    return UNSUPPORTED_FORMAT_MESSAGE


def detect_format(file_bytes: bytes) -> str | None:
    """Identify the file from its bytes: "pdf", "docx", "doc" (legacy/encrypted Word) or None."""
    if b"%PDF-" in file_bytes[:1024]:
        return "pdf"
    if file_bytes[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
        return None
    if file_bytes[:8] == _OLE_MAGIC:
        return "doc"
    return None


def parse_document(file_bytes: bytes, filename: str = "", article_type: str | None = None) -> ParsedDocument:
    """Parse a PDF or Word (.docx) paper, deciding the format from the file's content.

    `article_type` is the type the user chose; it wins over a label found in the file.

    Raises:
        ValueError: With a user-facing message when the file is unsupported or unreadable.
    """
    detected = detect_format(file_bytes)
    if detected == "pdf":
        return parse_pdf(file_bytes, article_type)
    if detected == "docx":
        return parse_docx(file_bytes, article_type)
    if detected == "doc":
        raise ValueError(LEGACY_DOC_MESSAGE)
    if filename.lower().endswith(".pdf"):
        return parse_pdf(file_bytes, article_type)  # raises the usual "could not open PDF" message
    if filename.lower().endswith(".docx"):
        raise ValueError("Could not open Word file. It may be corrupted; try saving it again as .docx")
    raise ValueError(UNSUPPORTED_FORMAT_MESSAGE)


# A journal cover page ("As featured in", charity number, ISSN, issue number) is not part of the paper.
_COVER_CUES = (
    re.compile(r"registered charity number", re.IGNORECASE),
    re.compile(r"rsc\.li/", re.IGNORECASE),
    re.compile(r"as featured in", re.IGNORECASE),
    re.compile(r"\bISSN\s+\d{4}-\d{3}[\dX]", re.IGNORECASE),
    re.compile(r"\bVolume\s+\d+\s+Number\s+\d+", re.IGNORECASE),
    re.compile(r"\bPages\s+\d+\s*[–\-]\s*\d+", re.IGNORECASE),
)
_MIN_COVER_CUES = 3
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}
_LIGATURE_TABLE = str.maketrans(_LIGATURES)
# Lines of journal furniture and article metadata that are neither abstract nor body.
_META_LINE = re.compile(
    r"(?i)(registered charity|rsc\.li|view article online|view journal|view issue|cite this|received\b|accepted\b"
    r"|doi\s*:|published on|downloaded by|©|this journal is|\bissn\b|as featured in)"
)
# Text that marks a block as authors, affiliations or notes rather than an abstract.
_NOT_ABSTRACT = re.compile(
    r"(?i)(university|institute|laboratory|department|faculty|school of|electronic supplementary|received\b"
    r"|accepted\b|doi\s*:|@|†|‡)"
)
_MIN_STYLE_ABSTRACT_WORDS = 30


def _is_cover_page(page_text: str) -> bool:
    return sum(1 for cue in _COVER_CUES if cue.search(page_text)) >= _MIN_COVER_CUES


def _leading_cover_pages(page_texts: list[str]) -> int:
    """1 when the first page is a journal cover and the document has real pages after it, else 0."""
    return 1 if len(page_texts) > 2 and _is_cover_page(page_texts[0]) else 0


def _normalize_pdf_text(text: str) -> str:
    """Join ligature letters the PDF put on a line of their own ("ﬂ" / "uorescence") and expand ligatures."""
    text = re.sub(r"([ﬀ-ﬆ])[ \t]*\n[ \t]*(?=[a-z])", r"\1", text)
    return text.translate(_LIGATURE_TABLE)


def _abstract_by_style(page: "fitz.Page") -> str | None:
    """Abstract of a paper that has no "Abstract" heading (Communications, Letters).

    Such journals print the abstract in a different font or size from the body text. The abstract is
    the first run of prose lines in a font other than the body font, long enough, and free of the
    author, affiliation and footnote markers.
    """
    lines: list[tuple[tuple[str, float], str]] = []
    weight: Counter[tuple[str, float]] = Counter()
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            dominant = max(spans, key=lambda s: len(s["text"]))
            style = (str(dominant.get("font", "")).split("+")[0], round(float(dominant.get("size", 0)), 1))
            text = "".join(s["text"] for s in spans)
            lines.append((style, text))
            weight[style] += len(text)
    if not lines:
        return None
    body_style = weight.most_common(1)[0][0]
    # runs of consecutive lines in one non-body style: the abstract, the authors and the metadata
    # block each have their own style, so they fall into separate groups
    groups: list[list[str]] = []
    current: tuple[str, float] | None = None
    for style, text in lines:
        if style != body_style and 7.0 <= style[1] <= 12.5:
            if style == current and groups:
                groups[-1].append(text)
            else:
                groups.append([text])
                current = style
        else:
            current = None
    for group in groups:
        joined = _normalize_pdf_text(re.sub(r"\s+", " ", " ".join(group))).strip()
        joined = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "-", joined)
        words = joined.split()
        if len(words) < _MIN_STYLE_ABSTRACT_WORDS or _NOT_ABSTRACT.search(joined):
            continue
        if sum(1 for w in words if w[:1].isalpha()) / len(words) < 0.8 or not joined.endswith("."):
            continue
        return joined[:MAX_ABSTRACT_CHARS].strip()
    return None


def parse_pdf(file_bytes: bytes, article_type: str | None = None) -> ParsedDocument:
    """Parse a PDF academic paper and extract structured content.

    Args:
        file_bytes: Raw bytes of the PDF file.
        article_type: Article type chosen by the user, if any.

    Returns:
        ParsedDocument with extracted text, abstract, sections,
        references, and word count.

    Raises:
        ValueError: If the PDF cannot be opened or parsed.
    """
    document: fitz.Document | None = None
    style_abstract: str | None = None
    first_page_lines: list[str] = []
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        page_texts = [page.get_text() for page in document]
        skip = _leading_cover_pages(page_texts)
        try:
            style_abstract = _abstract_by_style(document[skip])
        except Exception:  # an odd layout must never stop the upload
            logger.debug("Style-based abstract detection failed", exc_info=True)
        page_texts = page_texts[skip:]
        # The "COMMUNICATION" label is also a running header on later pages, so it is read from the
        # first page before running lines are removed.
        first_page_lines = page_texts[0].split("\n") if page_texts else []
        full_text = _normalize_pdf_text("\n".join(_drop_running_lines(page_texts))).strip()
        logger.debug("Extracted text from %d pages (skipped %d cover page)", len(page_texts), skip)
    except (ValueError, RuntimeError, TypeError, fitz.FileDataError) as exc:
        raise ValueError(
            "Could not open PDF — file may be corrupted or encrypted"
        ) from exc
    finally:
        if document is not None:
            document.close()

    return _build_document(
        full_text,
        one_reference_per_line=False,
        article_type_hint=article_type,
        reflow=True,
        style_abstract=style_abstract,
        label_lines=first_page_lines,
    )


def parse_docx(file_bytes: bytes, article_type: str | None = None) -> ParsedDocument:
    """Parse a Word (.docx) academic paper and extract structured content.

    Reads the document body in order (paragraphs, tables, content controls) and ignores
    headers, footers, footnotes, comments, tracked deletions and text boxes. Each paragraph
    becomes one line, so a heading is a line by itself, as the section detector expects.

    Raises:
        ValueError: If the file cannot be opened, is too large when unpacked, or has no text.
    """
    paragraphs = _docx_paragraphs(file_bytes)
    full_text = "\n".join(paragraphs).strip()
    if not full_text:
        raise ValueError("No readable text found in this Word file")
    logger.debug("Extracted %d paragraphs from docx", len(paragraphs))
    # In Word every reference is its own paragraph, and automatic list numbers are not text.
    return _build_document(full_text, one_reference_per_line=True, article_type_hint=article_type)


_SHORT_LINE_WORDS = 2
_MIN_SHORT_RUN = 3


def _reflow_lines(lines: list[str]) -> list[str]:
    """Join the one-word lines a justified PDF line leaves behind.

    A run of three or more very short lines ("fields / ranging / from / degradable") is a sentence
    broken apart, not headings, so it is joined together with the line that continues it. A real
    heading is a single short line between long lines and stays alone.
    """

    def is_short(line: str) -> bool:
        text = line.strip()
        if not text or len(text.split()) > _SHORT_LINE_WORDS or _NUMBER_ONLY.match(text):
            return False
        return _heading_title(text) is None

    out: list[str] = []
    index = 0
    while index < len(lines):
        if is_short(lines[index]):
            end = index
            while end < len(lines) and is_short(lines[end]):
                end += 1
            if end - index >= _MIN_SHORT_RUN:
                parts = [line.strip() for line in lines[index:end]]
                if end < len(lines) and lines[end].strip()[:1].islower():
                    parts.append(lines[end].strip())
                    end += 1
                out.append(" ".join(parts))
                index = end
                continue
        out.append(lines[index])
        index += 1
    return out


def _body_text(lines: list[str]) -> str:
    """The body as one block of text, without bare page numbers."""
    kept = [line.strip() for line in lines if line.strip() and not _NUMBER_ONLY.match(line.strip())]
    text = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "-", text)  # "neuro- nal" broken at a line end


def _build_document(
    full_text: str,
    one_reference_per_line: bool,
    article_type_hint: str | None = None,
    reflow: bool = False,
    style_abstract: str | None = None,
    label_lines: list[str] | None = None,
) -> ParsedDocument:
    """Shared text-to-structure step for every input format."""
    lines = full_text.split("\n")
    ref_start = _find_reference_line(lines)
    body_lines = lines[:ref_start] if ref_start is not None else lines
    ref_lines = lines[ref_start + 1 :] if ref_start is not None else []
    if reflow:
        body_lines = _reflow_lines(body_lines)

    user_type = paper_profile.coerce_article_type(article_type_hint)
    label_type = paper_profile.detect_article_type(label_lines if label_lines else body_lines)
    article_type = user_type or label_type
    type_source = "user" if user_type else ("label" if label_type else "none")

    abstract = _extract_abstract(body_lines, style_abstract)
    sections, roles, covered_chars = _extract_sections_detailed(body_lines, article_type)
    body = _body_text(body_lines)
    structure, coverage = paper_profile.assess_structure(roles, covered_chars, len(body), article_type)
    references = _extract_references(ref_lines, one_per_line=one_reference_per_line)
    word_count = len(full_text.split())

    logger.debug("Detected sections: %s (structure=%s, type=%s)", sorted(sections.keys()), structure, article_type)
    logger.debug("Extracted %d references", len(references))

    return ParsedDocument(
        full_text=full_text,
        abstract=abstract,
        sections=sections,
        references=references,
        word_count=word_count,
        article_type=article_type,
        article_type_source=type_source,
        structure=structure,
        coverage=round(coverage, 3),
        refers_to_si=paper_profile.refers_to_si(full_text),
        section_roles=roles,
        body=body,
    )


def _docx_paragraphs(file_bytes: bytes) -> list[str]:
    """Body text of a .docx as a list of non-empty paragraph strings, in reading order."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES or sum(e.file_size for e in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("This Word file is too large once unpacked. Save a smaller copy and try again")
        document = open_docx(io.BytesIO(file_bytes))
    except ValueError:
        raise
    except Exception as exc:  # corrupt zip, missing parts, bad XML
        raise ValueError(
            "Could not open Word file. It may be corrupted or password-protected"
        ) from exc

    lines: list[str] = []
    _collect_docx_blocks(document.element.body, lines)
    return lines


def _collect_docx_blocks(parent, lines: list[str]) -> None:
    """Append the text of paragraphs, tables and content controls under `parent`, in order."""
    for child in parent.iterchildren():
        tag = child.tag
        if tag == _W + "p":
            text = _docx_paragraph_text(child)
            if text:
                lines.append(text)
        elif tag == _W + "tbl":
            for row in child.iterchildren(_W + "tr"):
                cells: list[str] = []
                for cell in row.iterchildren(_W + "tc"):
                    inner: list[str] = []
                    _collect_docx_blocks(cell, inner)
                    if inner:
                        cells.append(" ".join(inner))
                if cells:
                    lines.append(" | ".join(cells))
        elif tag == _W + "sdt":  # content control (cover pages, tables of contents)
            content = child.find(_W + "sdtContent")
            if content is not None:
                _collect_docx_blocks(content, lines)


def _docx_paragraph_text(paragraph) -> str:
    parts: list[str] = []
    _collect_docx_text(paragraph, parts)
    text = "".join(parts).translate(_INVISIBLE_CHARS).replace("\u00a0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _collect_docx_text(element, parts: list[str]) -> None:
    for child in element.iterchildren():
        tag = child.tag
        if tag in (_W + "t", _M_TEXT):
            parts.append(child.text or "")
        elif tag in _DOCX_SPACE_TAGS:
            parts.append(" ")
        elif tag == _W + "noBreakHyphen":
            parts.append("-")
        elif tag in _DOCX_SKIPPED:
            continue
        else:
            _collect_docx_text(child, parts)


def _heading_title(line: str) -> str | None:
    """Return the lowercase heading title if the line is a heading, else None."""
    stripped = line.strip().rstrip(".:").strip()
    if not stripped or len(stripped) > 60:
        return None
    match = re.fullmatch(_HEADING_NUMBER + r"(.+)", stripped)
    if not match:
        return None
    raw_title = match.group(1).strip()
    # A heading starts with a capital. A lowercase word on a line by itself ("results", in the middle
    # of a sentence a justified PDF broke apart) is not a heading.
    if not raw_title[:1].isupper():
        return None
    title = raw_title.lower()
    return title if title in _KNOWN_TITLES else None


def _is_generic_heading(lines: list[str], index: int) -> bool:
    """A lone top-level number followed by a short capitalised title line."""
    if not _NUMBER_ONLY.match(lines[index].strip()):
        return False
    # A real heading has its title on the very next line. A page number is
    # followed by a blank line, so "3 / blank / Figure label" is not a heading.
    if index + 1 >= len(lines):
        return False
    text = lines[index + 1].strip()
    return bool(_GENERIC_TITLE.match(text)) and not text.endswith(".")


def _find_reference_line(lines: list[str]) -> int | None:
    """Index of the last line that is a References/Bibliography heading."""
    found: int | None = None
    for index, line in enumerate(lines):
        if _heading_title(line) in REFERENCE_TITLES:
            found = index
    return found


def _extract_abstract(lines: list[str], style_abstract: str | None = None) -> str:
    """Take the text between the Abstract heading and the next heading.

    Without an Abstract heading (Communications, Letters) use the abstract found by its typeface,
    and only then the opening lines of the paper without the journal's front-page furniture.
    """
    start: int | None = None
    inline = ""
    for index, line in enumerate(lines[:200]):
        stripped = line.strip()
        if re.fullmatch(r"(?i)abstract[\s\.:\-—]*", stripped):
            start = index + 1
            break
        match = re.match(r"(?i)^abstract[\s\.:\-—]+(.{40,})$", stripped)
        if match:
            start = index + 1
            inline = match.group(1)
            break

    if start is not None:
        collected = [inline] if inline else []
        for index in range(start, len(lines)):
            if _heading_title(lines[index]) or _is_generic_heading(lines, index):
                break
            if re.match(r"(?i)^\s*(?:keywords?|index terms)\b", lines[index]):
                break
            if re.match(r"^\s*[∗†‡*]", lines[index]):  # author footnotes
                break
            collected.append(lines[index])
        text = re.sub(r"\s+", " ", " ".join(collected)).strip()
        if len(text) >= 100:
            return text[:MAX_ABSTRACT_CHARS].strip()

    if style_abstract and len(style_abstract) >= 120:
        return style_abstract[:MAX_ABSTRACT_CHARS].strip()

    head_lines = [line for line in lines[:80] if not _META_LINE.search(line)]
    head = re.sub(r"\s+", " ", _EMAIL.sub("", " ".join(head_lines))).strip()
    fallback = head[:FALLBACK_ABSTRACT_CHARS].strip()
    return fallback if fallback else "Abstract not found."


_LEADING_NUMBER = re.compile(r"^\s*(\d{1,2})[\.\)]?\s+(\S.*)$")


def _topic_heading(line: str, wanted_number: int) -> str | None:
    """Title of a numbered top-level topic heading ("3 Polymer brushes"), or None.

    Only the next number in sequence counts, and sub-headings ("3.1 ...") never match, so a figure
    label or a sentence that happens to start with a digit is not mistaken for a heading.
    """
    match = _LEADING_NUMBER.match(line)
    if not match or int(match.group(1)) != wanted_number:
        return None
    title = match.group(2).strip().rstrip(":")
    if not (3 <= len(title) <= 90) or not title[:1].isupper() or title.endswith("."):
        return None
    if re.match(r"^\d", title):  # "2.1 ..." style sub-heading
        return None
    return title


def _extract_sections_detailed(
    lines: list[str], article_type: str | None = None
) -> tuple[dict[str, str], dict[str, str], int]:
    """Sections under real headings: (sections, role of each, characters they cover).

    Standard headings map to introduction / methods / results / discussion. For perspectives and
    reviews every numbered top-level heading between them is a "thematic" section named by its title.
    """
    thematic_paper = article_type in paper_profile.THEMATIC_TYPES
    boundaries: list[tuple[int, str | None, str | None]] = []  # (line, role, key)
    next_number = 1
    for index, line in enumerate(lines):
        title = _heading_title(line)
        if title is not None:
            role = _TITLE_TO_SECTION.get(title)
            number = _LEADING_NUMBER.match(line)
            if number and int(number.group(1)) == next_number:
                next_number += 1
            boundaries.append((index, role, role))
        elif _is_generic_heading(lines, index):
            boundaries.append((index, None, None))
        elif thematic_paper:
            topic = _topic_heading(line, next_number)
            if topic is not None:
                next_number += 1
                boundaries.append((index, paper_profile.ROLE_THEMATIC, topic))

    parts: dict[str, list[str]] = {}
    roles: dict[str, str] = {}
    covered = 0
    left_out = False
    for position, (start, role, key) in enumerate(boundaries):
        if role is None or key is None:
            continue
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
        chunk = [
            line for line in lines[start + 1 : end]
            if not _NUMBER_ONLY.match(line.strip())
        ]
        text = re.sub(r"\s+", " ", " ".join(chunk)).strip()
        if len(text) <= MIN_SECTION_CONTENT_LENGTH:
            continue
        if role == paper_profile.ROLE_THEMATIC and key not in parts:
            if sum(1 for r in roles.values() if r == paper_profile.ROLE_THEMATIC) >= MAX_THEMATIC_SECTIONS:
                left_out = True  # more topics than we keep
                continue
        parts.setdefault(key, []).append(text)
        roles[key] = role
        covered += len(text)

    extracted = {
        name: " ".join(chunks)[:MAX_SECTION_CHARS].strip()
        for name, chunks in parts.items()
    }
    if not extracted:
        body = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if body:
            extracted["body"] = body[:FALLBACK_BODY_CHARS]
    if left_out:
        covered = 0  # part of the paper has no section, so the whole body is analysed instead
    return extracted, roles, covered


def _extract_sections(lines: list[str]) -> dict[str, str]:
    """Extract core manuscript sections using heading lines only."""
    return _extract_sections_detailed(lines)[0]


def _extract_references(ref_lines: list[str], one_per_line: bool = False) -> list[str]:
    """Extract bibliography entries from the lines after the References heading.

    With `one_per_line` (Word files) every non-empty line is one reference, up to the next
    known heading such as Appendix or Acknowledgements.
    """
    if one_per_line:
        chunks: list[str] = []
        for line in ref_lines:
            text = line.strip()
            if not text or _NUMBER_ONLY.match(text):
                continue
            if _heading_title(text) is not None:
                break
            chunks.append(text)
        chunks = _strip_list_numbers(chunks)
    else:
        kept = [line for line in ref_lines if not _NUMBER_ONLY.match(line.strip())]
        reference_section = "\n".join(kept).strip()
        if not reference_section:
            return []
        chunks = _split_reference_candidates(reference_section)
    cleaned: list[str] = []
    for chunk in chunks:
        chunk = _trim_after_reference(chunk)
        normalized = re.sub(r"\s+", " ", chunk).strip()
        normalized = re.sub(REFERENCE_PREFIX_PATTERN, "", normalized).strip()
        # a line break inside a hyphenated name or a page range leaves a space: "Ouellet- Plamondon", "827– 831"
        normalized = re.sub(r"(?<=[A-Za-z])- (?=[A-Z])", "-", normalized)
        normalized = re.sub(r"(?<=\d)([\u2013\u2014-]) (?=\d)", r"\1", normalized)
        # "13 (a) ...; (b) ...; (c) ..." is several papers under one number: each is a reference
        for piece in _split_subreferences(normalized):
            piece = trim_after_citation(piece)
            if len(piece) < MIN_REFERENCE_LENGTH:
                continue
            if _looks_like_heading(piece):
                continue
            if not _has_citation_signal(piece):
                continue  # a footnote or comment inside the list, not a citation
            cleaned.append(piece)
            if len(cleaned) >= MAX_REFERENCES:
                return cleaned
    return cleaned


_LIST_NUMBER = re.compile(r"^(\d{1,3})\s+(?=\S)")


def _strip_list_numbers(chunks: list[str]) -> list[str]:
    """Remove "1 ", "2 " ... typed in front of Word references (a number, a tab, then the citation).

    Done only when every entry starts with the next number in order, so a reference that happens to
    begin with a number is never touched.
    """
    numbers = [_LIST_NUMBER.match(chunk) for chunk in chunks]
    if len(chunks) < 2 or any(m is None for m in numbers):
        return chunks
    first = int(numbers[0].group(1))  # type: ignore[union-attr]
    if any(int(m.group(1)) != first + i for i, m in enumerate(numbers)):  # type: ignore[union-attr]
        return chunks
    return [chunk[m.end():] for chunk, m in zip(chunks, numbers)]  # type: ignore[union-attr]


_CITATION_SIGNAL = re.compile(r"\b(?:19|20)\d{2}\b|10\.\d{4,9}/|arxiv", re.IGNORECASE)
_SUBREFERENCE_MARK = re.compile(r"\(([a-z])\)\s+(?=[A-Z\[])")


def _has_citation_signal(text: str) -> bool:
    """A citation has a year, a DOI or an arXiv id; a footnote in the list usually has none."""
    return bool(_CITATION_SIGNAL.search(text))


def _split_subreferences(text: str) -> list[str]:
    """Split "(a) ...; (b) ...; (c) ..." into separate citations, in order.

    Markers must run a, b, c... and follow ";", "." or ":" so "(c)" inside a chemical name or a
    sentence is left alone. Text before "(a)" ("See for examples:") is dropped when it is only a lead-in.
    """
    marks: list[re.Match[str]] = []
    expected = "a"
    for match in _SUBREFERENCE_MARK.finditer(text):
        before = text[: match.start()].rstrip()
        if match.group(1) != expected or (before and before[-1] not in ";.:"):
            continue
        marks.append(match)
        expected = chr(ord(expected) + 1)
    if not marks:
        return [text]

    pieces: list[str] = []
    lead = text[: marks[0].start()].strip(" ;:")
    if lead and (len(lead) > 60 or _has_citation_signal(lead)):
        pieces.append(lead)
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        pieces.append(text[match.end():end].strip(" ;"))
    return [piece for piece in pieces if piece]


def _trim_after_reference(chunk: str) -> str:
    """Drop appendix text glued to the last reference (e.g. figure labels)."""
    lines = chunk.split("\n")
    previous = ""
    for index, line in enumerate(lines):
        current = line.strip()
        if not current:
            continue
        if previous and _REF_END_YEAR.search(previous) and _GENERIC_TITLE.match(current):
            return "\n".join(lines[:index])
        previous = current
    return chunk


# A line that starts a numbered entry: "[7] ", "7. ", "7) " or just "7 " (RSC, ACS and Nature layouts),
# followed by an author, or by a sub-reference marker such as "(a)".
_NUMBERED_START = re.compile(r"(?m)^[ \t]*\[?(\d{1,3})[\.\)\]]?[ \t]+(?=(?:\([a-z]\)\s*)?[\[A-Z])")
MAX_NUMBER_JUMP = 2  # tolerate an entry the PDF extraction lost
MIN_NUMBERED_ENTRIES = 3


def _split_numbered_entries(section: str) -> list[str] | None:
    """Split a numbered reference list using the numbers themselves.

    A line only counts as a new entry when its number is the next one in the sequence (1, 2, 3, ...),
    so a wrapped line that happens to start with a digit ("45 Angew.") is not mistaken for one.
    Returns None when the text is not a numbered list.
    """
    starts: list[re.Match[str]] = []
    expected = 1
    for match in _NUMBERED_START.finditer(section):
        number = int(match.group(1))
        if expected <= number <= expected + MAX_NUMBER_JUMP:
            starts.append(match)
            expected = number + 1
    if len(starts) < MIN_NUMBERED_ENTRIES:
        return None
    entries = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        entries.append(section[match.end():end].strip())
    return entries


def _running_key(line: str) -> str | None:
    """Page numbers change from page to page; with digits blanked, a running header looks the same."""
    stripped = line.strip()
    if len(stripped) < 10:
        return None
    return re.sub(r"\d+", "#", stripped).lower()


def _drop_running_lines(page_texts: list[str]) -> list[str]:
    """Remove running headers and footers: lines (digits ignored) repeated on three or more pages.

    Journals print "View Article Online", "This journal is (c) ..." and "Polym. Chem., 2017, 8, 6199 | 6200"
    on every page. Left in, they get glued onto whichever reference sits at a page break.
    """
    if len(page_texts) < 4:
        return page_texts
    pages_with: dict[str, set[int]] = defaultdict(set)
    for number, text in enumerate(page_texts):
        for line in text.split("\n"):
            key = _running_key(line)
            if key:
                pages_with[key].add(number)
    repeated = {key for key, pages in pages_with.items() if len(pages) >= 3}
    if not repeated:
        return page_texts
    return [
        "\n".join(line for line in text.split("\n") if _running_key(line) not in repeated)
        for text in page_texts
    ]


# "Surname, I." or "Surname, I. J." at the start of a line: the first author of an author-year entry (APA, Harvard)
_AUTHOR_START = re.compile(r"^[A-Z][\w'\u2019\-\u00c0-\u017f]+(?:\s[\w'\u2019\-]+){0,2},\s+(?:[A-Z][\.\-]\s?){1,3}")
_YEAR_IN_ENTRY = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
MAX_YEARS_PER_ENTRY = 2.0  # a split with more years per entry than this has merged several references


def _entry_is_complete(text: str) -> bool:
    """An author-year entry has its year and ends like a citation (period, page number, bracket)."""
    stripped = text.rstrip()
    return bool(_YEAR_IN_ENTRY.search(stripped)) and stripped[-1:] in ".)]0123456789"


def _split_author_year(section: str) -> list[str] | None:
    """Split an unnumbered author-year list ("Akopyan, F., J. Sawada, ... (2015). Title. Journal 34, 1-9.").

    A line starts a new entry only when it opens like a first author AND the entry before it is already
    complete, so a wrapped author list ("... P. Datta,\nNam, B. Taba, ...") is not cut in the middle.
    Returns None when this layout is not found.
    """
    entries: list[str] = []
    current: list[str] = []
    for raw_line in section.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if current and _AUTHOR_START.match(line) and _entry_is_complete(" ".join(current)):
            entries.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append(" ".join(current))
    return entries if len(entries) >= MIN_NUMBERED_ENTRIES else None


def _years_per_entry(entries: list[str]) -> float:
    return sum(len(_YEAR_IN_ENTRY.findall(entry)) for entry in entries) / max(1, len(entries))


def _split_reference_candidates(reference_section: str) -> list[str]:
    """Split references using known citation formatting patterns."""
    numbered = _split_numbered_entries(reference_section)
    if numbered is not None:
        return numbered
    for pattern in (REF_SPLIT_PATTERN_A, REF_SPLIT_PATTERN_B):
        chunks = [chunk.strip() for chunk in re.split(pattern, reference_section) if chunk.strip()]
        if len(chunks) > 1:
            return chunks

    # No numbers: entries are separated by blank lines, or (very often in PDFs) not separated at all.
    by_blank_lines = [c.strip() for c in re.split(REF_SPLIT_PATTERN_C, reference_section) if c.strip()]
    if len(by_blank_lines) > 1 and _years_per_entry(by_blank_lines) <= MAX_YEARS_PER_ENTRY:
        return by_blank_lines
    by_author = _split_author_year(reference_section)
    if by_author is not None and _years_per_entry(by_author) < _years_per_entry(by_blank_lines or [reference_section]):
        return by_author
    if len(by_blank_lines) > 1:
        return by_blank_lines
    fallback = reference_section.strip()
    return [fallback] if fallback else []


def _looks_like_heading(value: str) -> bool:
    """Heuristic check for short uppercase section-heading-like text."""
    return len(value) < 60 and value.upper() == value
