"""Document parsing service: extracts structured academic paper content from PDF or Word (.docx)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from contextvars import ContextVar
from typing import NamedTuple

import fitz
from docx import Document as open_docx

from models import FileMark, ParsedDocument
from services import paper_profile
from services.citation_refs import trim_after_citation
from services.citations import trim_doi

logger = logging.getLogger(__name__)

MAX_ABSTRACT_CHARS = 1800
# Sections are not cut: the prompt builder decides how much text the model gets.
MAX_SECTION_CHARS = 400_000
MAX_REFERENCES = 300
MIN_REFERENCE_LENGTH = 20
MAX_FILE_SIZE_BYTES = 10_485_760
FALLBACK_ABSTRACT_CHARS = 1200
MIN_SECTION_CONTENT_LENGTH = 50
FALLBACK_BODY_CHARS = 400_000
MIN_WORDS_FOR_REFERENCE_WARNING = 2000
REFERENCE_RECALL_WARNING = 0.9  # warn when fewer than this share of a numbered list could be read

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
        # Nature / Science / Cell layouts, where Methods often follows the main reference list
        "online methods", "star methods", "star★methods", "method details", "methods summary",
        "materials and experimental methods",
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
# Headings of parts of the paper that have no standard role but are still read by the model.
KEPT_OTHER_TITLES = frozenset({
    "background", "related work", "literature review", "limitations", "future work",
})
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
    # licence, rights and journal notes printed after the paper
    "licence and permissions", "license and permissions", "licence", "license", "permissions",
    "rights and permissions", "reprints and permissions", "open access", "copyright",
    "additional information", "ethics declarations", "code availability", "peer review information",
    "publisher's note", "extended data", "source data", "competing financial interests",
    "disclosure", "disclosures", "declaration of interests", "declarations of interest", "financial disclosure",
    "conflict of interest statement", "conflicts of interest statement", "competing interest", "funding information",
    "funding sources", "financial support", "declarations",
)
REFERENCE_TITLES = (
    "references", "bibliography", "literature cited", "notes and references", "references and notes",
    "cited references", "works cited",
)
MAX_THEMATIC_SECTIONS = 12
BODY_FALLBACK_MIN_SECTION_CHARS = 200

_TITLE_TO_SECTION = {t: name for name, titles in SECTION_TITLES.items() for t in titles}
_KNOWN_TITLES = set(_TITLE_TO_SECTION) | set(OTHER_TITLES) | set(REFERENCE_TITLES)
# Optional heading prefix: "2.", "2.1", "II)", "(3)" or a letter with punctuation ("D. References", "A) Methods")
_HEADING_NUMBER = (
    r"(?:(?:(?:\d{1,2}(?:\.\d{1,2}){0,2}|[IVX]{1,4})\s+|(?:\d{1,2}(?:\.\d{1,2}){0,2}|[IVX]{1,4})[\.\)]\s*"
    r"|\(\d{1,2}\)\s*|[A-Z][\.\)]\s+))?"
)
_NUMBER_ONLY = re.compile(r"^\d{1,2}$")
_GENERIC_TITLE = re.compile(r"^[A-Z][A-Za-z\-, ]{2,45}$")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
REF_SPLIT_PATTERN_A = r"\n(?=\[\d{1,3}\])"
REF_SPLIT_PATTERN_B = r"\n(?=\d{1,3}[\.\)]\s+[A-Z])"
REF_SPLIT_PATTERN_C = r"\n\n+"
REFERENCE_PREFIX_PATTERN = r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[\.\)])\s*"
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


# ---------------------------------------------------------------- headings from the layout of the file

MIN_LAYOUT_HEADINGS = 3  # a heading style repeats; a title or a label appears once
MAX_LAYOUT_HEADINGS = 40  # more than this is not a list of section headings
_CAPTION_START = re.compile(r"(?i)^(?:fig(?:ure)?s?|table|scheme|chart|box|equation|plate|panel|note|supplementary)\b")
_BOLD_FONT = re.compile(r"(?i)bold|black|heavy|demi|semibold|extrabold")
_HEADING_LIKE_SHARE = 0.85  # share of a line's letters that must be set like a heading
_BOLD_FLAG = 16
_SIZE_STEP = 0.8  # points larger than the body text that make a line a heading by size alone


def _layout_heading_shape_ok(text: str) -> bool:
    """A heading is short, is not a sentence or a caption, and is not an address, a number or a reference."""
    if not (3 <= len(text) <= MAX_LAYOUT_HEADING_CHARS):
        return False
    words = text.split()
    letters = sum(c.isalpha() for c in text)
    if len(words) > MAX_LAYOUT_HEADING_WORDS or letters < 3 or letters < 0.6 * len(text.replace(" ", "")):
        return False
    if text.endswith((",", "-")) or _CAPTION_START.match(text) or _EMAIL.search(text) or "http" in text.lower():
        return False
    return text[:1].isupper() or text[:1].isdigit()


def _group_layout_headings(
    lines: list[tuple[int, int, int, tuple[float, bool], str]]
) -> tuple[tuple[str, ...], ...]:
    """Headings set apart by their typeface, as tuples of the lines each is printed on.

    `lines` are the lines that look like headings: (page, block, line in block, style, text). The headings of a paper
    share one typeface and repeat (Introduction, Methods, ...), so the highest style that occurs at least
    MIN_LAYOUT_HEADINGS times is taken; a title, or a label that appears once, never is.
    """
    # lines of one heading that wrapped: same page, same block, same style
    groups: list[tuple[tuple[float, bool], list[str]]] = []
    last: tuple[int, int, int, tuple[float, bool]] | None = None
    for page, block, position, style, text in lines:
        # the next line of the same block, in the same style, continues a heading that did not end
        continues = (
            groups and last is not None and last[:2] == (page, block) and last[2] + 1 == position and last[3] == style
            and not groups[-1][1][-1].endswith((".", "?", ":", "!")) and _heading_title(groups[-1][1][-1]) is None
        )
        if continues:
            groups[-1][1].append(text)
        else:
            groups.append((style, [text]))
        last = (page, block, position, style)
    by_style: dict[tuple[float, bool], list[list[str]]] = defaultdict(list)
    for style, parts in groups:
        by_style[style].append(parts)
    repeating = [
        style for style, found in by_style.items() if MIN_LAYOUT_HEADINGS <= len(found) <= MAX_LAYOUT_HEADINGS
    ]
    if not repeating:
        return ()
    top = max(repeating, key=lambda s: (s[0], s[1], len(by_style[s])))
    return tuple(tuple(parts) for parts in by_style[top])


def _layout_headings_pdf(document: "fitz.Document", first_page: int) -> tuple[tuple[str, ...], ...]:
    """Section headings of a PDF, found from its typography (larger or bold type than the body text)."""
    weight: Counter[tuple[float, bool]] = Counter()
    raw: list[tuple[int, int, int, list[tuple[int, float, bool]], str]] = []
    for page_number in range(first_page, len(document)):
        for block_number, block in enumerate(document[page_number].get_text("dict").get("blocks", [])):
            if block.get("type") != 0:
                continue
            for line_number, line in enumerate(block.get("lines", [])):
                spans = [s for s in line.get("spans", []) if str(s.get("text", "")).strip()]
                if not spans:
                    continue
                measured = [
                    (
                        len(s["text"].strip()),
                        round(float(s.get("size", 0)) * 2) / 2,
                        bool(int(s.get("flags", 0)) & _BOLD_FLAG) or bool(_BOLD_FONT.search(str(s.get("font", "")))),
                    )
                    for s in spans
                ]
                text = re.sub(r"\s+", " ", "".join(s["text"] for s in spans)).strip()
                for letters, size, bold in measured:
                    weight[(size, bold)] += letters
                raw.append((page_number, block_number, line_number, measured, text))
    if not raw or not weight:
        return ()
    body_size, body_bold = weight.most_common(1)[0][0]

    running_pages: dict[str, set[int]] = defaultdict(set)
    for page_number, _, _, _, text in raw:
        running_pages[re.sub(r"\d+", "#", _layout_key(text))].add(page_number)

    candidates: list[tuple[int, int, int, tuple[float, bool], str]] = []
    for page_number, block_number, line_number, measured, text in raw:
        if len(running_pages[re.sub(r"\d+", "#", _layout_key(text))]) >= 3:
            continue  # a running header or footer
        total = sum(letters for letters, _, _ in measured)
        marked = sum(
            letters for letters, size, bold in measured
            if size >= body_size + _SIZE_STEP or (bold and not body_bold and size >= body_size - 1.0)
        )
        if not total or marked / total < _HEADING_LIKE_SHARE:
            continue
        cleaned = _clean_heading_text(text)
        if not _layout_heading_shape_ok(cleaned) or _NUMBER_ONLY.match(cleaned):
            continue
        dominant = max(measured, key=lambda m: m[0])
        candidates.append((page_number, block_number, line_number, (dominant[1], dominant[2]), text))
    return _group_layout_headings(candidates)


def _merge_layout_headings(
    lines: list[str], headings: tuple[tuple[str, ...], ...]
) -> tuple[list[str], frozenset[str]]:
    """The lines with each wrapped layout heading joined onto one line, and the keys of all layout headings."""
    keys: set[str] = set()
    wrapped: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for parts in headings:
        keys.add(_layout_key(" ".join(parts)))
        if len(parts) > 1:
            wrapped[_layout_key(parts[0])].append(tuple(_layout_key(p) for p in parts))
    if not wrapped:
        return lines, frozenset(keys)
    merged: list[str] = []
    index = 0
    while index < len(lines):
        match = None
        for candidate in sorted(wrapped.get(_layout_key(lines[index]), ()), key=len, reverse=True):
            tail = lines[index : index + len(candidate)]
            if len(tail) == len(candidate) and all(_layout_key(t) == k for t, k in zip(tail, candidate)):
                match = candidate
                break
        if match is None:
            merged.append(lines[index])
            index += 1
        else:
            merged.append(" ".join(t.strip() for t in lines[index : index + len(match)]))
            index += len(match)
    return merged, frozenset(keys)


# ---------------------------------------------------------------- what the file says about its own status

# Words a publisher prints on a retracted or flagged article. Only the first page, or a watermark repeated on at
# least half of the pages, counts: a paper ABOUT retractions, or a reference to a "Retraction Note", does not.
_FILE_MARKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("retracted", re.compile(r"\bRETRACTED ARTICLE\b")),
    ("retracted", re.compile(r"(?m)^[ \t]*RETRACTED[ \t]*:")),
    ("retracted", re.compile(r"(?i)\bthis article has been retracted\b")),
    ("retracted", re.compile(r"(?i)\bnotice of retraction\b")),
    ("expression_of_concern", re.compile(r"\bEXPRESSION OF CONCERN\b")),
    ("expression_of_concern", re.compile(r"(?im)^[ \t]*expression of concern[ \t]*:")),
    ("withdrawn", re.compile(r"(?m)^[ \t]*WITHDRAWN[ \t]*:")),
)
MARK_SHARE_OF_PAGES = 0.5
# The paper's own DOI is printed after "doi" or in a doi.org link, in the header or footer of its pages.
_OWN_DOI = re.compile(r"(?i)(?:\bdoi\s*:?\s*|doi\.org/)(10\.\d{4,9}/[^\s,\]\"']+)")
_ANY_DOI = re.compile(r"10\.\d{4,9}/[^\s,\]\"'<>]+")  # metadata is XML: a tag ends the DOI
MAX_DOI_CANDIDATES = 3
MAX_TITLE_CHARS = 300
_FILENAME_TITLE = re.compile(r"(?i)\.(?:docx?|pdf|tex)\b|microsoft word|untitled|^[\w\-]+$")


def _file_marks(page_texts: list[str]) -> list[FileMark]:
    """Status words printed in the file: on its first page, or as a watermark on at least half of its pages."""
    found: dict[tuple[str, str], FileMark] = {}
    for kind, pattern in _FILE_MARKS:
        pages = [number for number, text in enumerate(page_texts) if pattern.search(text)]
        if not pages:
            continue
        if pages[0] == 0 or len(pages) >= max(2, MARK_SHARE_OF_PAGES * len(page_texts)):
            words = re.sub(r"\s+", " ", pattern.search(page_texts[pages[0]]).group(0)).strip(" :")  # type: ignore[union-attr]
            key = (kind, words.lower())
            if key not in found:
                found[key] = FileMark(kind=kind, text=words, pages=len(pages))
    return list(found.values())


def _doi_candidates(page_texts: list[str], metadata_texts: list[str]) -> list[str]:
    """The paper's own DOI, most likely first: from the file's metadata, then a DOI printed on several pages
    (a running header or footer), then one on the first page. A DOI cited once in the references is not one."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        doi = trim_doi(raw)
        if doi and doi.lower() not in seen:
            seen.add(doi.lower())
            ordered.append(doi)

    for text in metadata_texts:
        for match in _ANY_DOI.finditer(text or ""):
            add(match.group(0))
    pages_with: Counter[str] = Counter()
    spelled: dict[str, str] = {}
    on_first_page: set[str] = set()
    for number, text in enumerate(page_texts):
        here = {trim_doi(m.group(1)) for m in _OWN_DOI.finditer(text)}
        for doi in here:
            if not doi:
                continue
            pages_with[doi.lower()] += 1
            spelled.setdefault(doi.lower(), doi)
            if number == 0:
                on_first_page.add(doi.lower())
    for key, count in sorted(pages_with.items(), key=lambda item: (-item[1], item[0])):
        if count >= 2 or key in on_first_page:
            add(spelled[key])
    return ordered[:MAX_DOI_CANDIDATES]


def _usable_title(text: str | None) -> str:
    title = re.sub(r"\s+", " ", text or "").strip()
    if len(title.split()) < 3 or _FILENAME_TITLE.search(title):
        return ""
    return title[:MAX_TITLE_CHARS]


def _title_by_size(page: "fitz.Page") -> str:
    """The title of a PDF: the lines in the largest type on the first page that read like words."""
    lines: list[tuple[float, int, str]] = []
    for number, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if str(s.get("text", "")).strip()]
            if not spans:
                continue
            size = round(max(float(s.get("size", 0)) for s in spans), 1)
            lines.append((size, number, re.sub(r"\s+", " ", "".join(s["text"] for s in spans)).strip()))
    wordy = [item for item in lines if sum(c.isalpha() for c in item[2]) >= 10]
    if not wordy:
        return ""
    top = max(size for size, _, _ in wordy)
    block = next(number for size, number, _ in wordy if size == top)
    return _usable_title(" ".join(text for size, number, text in wordy if size == top and number == block))


_ABSTRACT_LABEL = re.compile(r"(?i)^\s*abstract[\s\.:\-—]*")
MIN_BLOCK_ABSTRACT_WORDS = 25


def _block_lines(block: dict) -> list[str]:
    texts = ["".join(s.get("text", "") for s in line.get("spans", [])).strip() for line in block.get("lines", [])]
    return [t for t in texts if t]


def _abstract_by_block(page: "fitz.Page") -> str | None:
    """The abstract as the PDF lays it out: the block that starts with the "Abstract" label, to its end."""
    blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
    for number, block in enumerate(blocks):
        texts = _block_lines(block)
        for position, text in enumerate(texts):
            if not _ABSTRACT_LABEL.match(text):
                continue
            rest = [_ABSTRACT_LABEL.sub("", text, count=1)] + texts[position + 1 :]
            if not rest[0].strip():
                rest = rest[1:]
                if not rest and number + 1 < len(blocks):  # the label is a block of its own
                    rest = _block_lines(blocks[number + 1])
            kept: list[str] = []
            for line in rest:
                if re.match(r"(?i)^\s*(?:keywords?|index terms)\b", line) or re.match(r"^\s*[∗†‡*]", line):
                    break
                kept.append(line)
            joined = _normalize_pdf_text(re.sub(r"\s+", " ", " ".join(t for t in kept if t))).strip()
            joined = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "-", joined)
            if len(kept) >= 2 and len(joined.split()) >= MIN_BLOCK_ABSTRACT_WORDS:
                return joined
            return None
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
    layout_headings: tuple[tuple[str, ...], ...] = ()
    block_abstract: str | None = None
    marks: list[FileMark] = []
    dois: list[str] = []
    title = ""
    front_text = ""
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        page_texts = [page.get_text() for page in document]
        skip = _leading_cover_pages(page_texts)
        try:
            # read before running headers are removed: a watermark IS a running line
            metadata = [str(v) for v in (document.metadata or {}).values() if v]
            metadata.append(document.get_xml_metadata() or "")
            marks = _file_marks(page_texts[skip:])
            dois = _doi_candidates(page_texts[skip:], metadata)
            title = _usable_title((document.metadata or {}).get("title")) or _title_by_size(document[skip])
            front_text = _normalize_pdf_text(page_texts[skip]) if skip < len(page_texts) else ""
        except Exception:  # identity and status are checked without them
            logger.debug("Reading the file's status marks failed", exc_info=True)
        try:
            style_abstract = _abstract_by_style(document[skip])
        except Exception:  # an odd layout must never stop the upload
            logger.debug("Style-based abstract detection failed", exc_info=True)
        try:
            block_abstract = _abstract_by_block(document[skip])
        except Exception:  # an odd layout must never stop the upload
            logger.debug("Block-based abstract detection failed", exc_info=True)
        try:
            layout_headings = _layout_headings_pdf(document, skip)
        except Exception:  # the known headings still work without the typography
            logger.debug("Layout heading detection failed", exc_info=True)
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
        layout_headings=layout_headings,
        block_abstract=block_abstract,
        integrity_marks=marks,
        doi_candidates=dois,
        title=title,
        front_text=front_text,
    )


def parse_docx(file_bytes: bytes, article_type: str | None = None) -> ParsedDocument:
    """Parse a Word (.docx) academic paper and extract structured content.

    Reads the document body in order (paragraphs, tables, content controls) and ignores
    headers, footers, footnotes, comments, tracked deletions and text boxes. Each paragraph
    becomes one line, so a heading is a line by itself, as the section detector expects.

    Raises:
        ValueError: If the file cannot be opened, is too large when unpacked, or has no text.
    """
    paragraphs, marks, core = _docx_paragraphs_marked(file_bytes)
    full_text = "\n".join(paragraphs).strip()
    if not full_text:
        raise ValueError("No readable text found in this Word file")
    logger.debug("Extracted %d paragraphs from docx", len(paragraphs))
    layout_headings: tuple[tuple[str, ...], ...] = ()
    try:
        layout_headings = _layout_headings_docx(paragraphs, marks)
    except Exception:  # the known headings still work without the styles
        logger.debug("Layout heading detection failed", exc_info=True)
    head = "\n".join(paragraphs[:DOCX_FRONT_PARAGRAPHS])
    rest = "\n".join(paragraphs[DOCX_FRONT_PARAGRAPHS:])
    # In Word every reference is its own paragraph, and automatic list numbers are not text.
    return _build_document(
        full_text,
        one_reference_per_line=True,
        article_type_hint=article_type,
        layout_headings=layout_headings,
        integrity_marks=_file_marks([head, rest]),
        doi_candidates=_doi_candidates([head], list(core.values())),
        title=_usable_title(core.get("title")) or (_usable_title(paragraphs[0]) if paragraphs else ""),
        front_text=head,
    )


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


PARAGRAPH_BREAK = chr(10) * 2
_SENTENCE_END = re.compile(r"[.!?][)\]\"']?\d{0,3}$")
_PARAGRAPH_SHORT_RATIO = 0.7  # the last line of a paragraph is clearly shorter than a typical full line
BACK_MATTER_TITLES = frozenset({
    "acknowledgements", "acknowledgments", "acknowledgement", "appendix", "supplementary material",
    "supporting information", "supplementary information", "supplementary data",
    "electronic supplementary information", "electronic supplementary information (esi)",
    "conflicts of interest", "conflict of interest", "competing interests", "declaration of competing interest",
    "author contributions", "data availability", "data availability statement", "funding",
    "credit authorship contribution statement", "author information", "associated content",
    "licence and permissions", "license and permissions", "rights and permissions", "reprints and permissions",
    "ethics declarations", "code availability", "peer review information", "publisher's note",
})


def _paragraph_text(lines: list[str], per_line: bool = False) -> str:
    """Lines as readable text: paragraphs separated by a blank line, words by single spaces.

    In a Word file every line already is a paragraph. In a PDF a paragraph ends where a line finishes a
    sentence and is clearly shorter than a full line (justified text), or next to a heading.
    """
    kept = [line.strip() for line in lines if line.strip() and not _NUMBER_ONLY.match(line.strip())]
    if not kept:
        return ""
    if per_line:
        paragraphs = kept
    else:
        lengths = sorted(len(line) for line in kept if len(line) >= 30)
        typical = lengths[len(lengths) // 2] if lengths else 0
        paragraphs = []
        current = [kept[0]]
        for previous, line in zip(kept, kept[1:]):
            next_to_heading = _heading_title(line) is not None or _heading_title(previous) is not None
            ends_short = bool(
                typical and _SENTENCE_END.search(previous) and len(previous) < _PARAGRAPH_SHORT_RATIO * typical
            )
            if next_to_heading or (ends_short and line[:1].isupper()):
                paragraphs.append(" ".join(current))
                current = [line]
            else:
                current.append(line)
        paragraphs.append(" ".join(current))
    cleaned = [re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "-", re.sub(r"\s+", " ", para)).strip() for para in paragraphs]
    return PARAGRAPH_BREAK.join(para for para in cleaned if para)


def _cut_back_matter(lines: list[str]) -> list[str]:
    """Drop acknowledgements, conflicts of interest, supporting information... at the end of the body.

    Only a heading in the second half counts, so a stray early line is never mistaken for the end.
    """
    for index, line in enumerate(lines):
        if index >= len(lines) // 2 and _heading_title(line) in BACK_MATTER_TITLES:
            return lines[:index]
    return lines


# Journal furniture on the first page: whole lines that start like this are not part of the paper's text.
_FURNITURE_LINE = re.compile(
    r"^\s*(?:Cite this:|Received\s+\d|Accepted\s+\d|DOI:\s*10\.|rsc\.li/|View Article Online|View Journal"
    r"|Published on \d|This journal is ©)",
    re.IGNORECASE,
)


def _body_text(lines: list[str], per_line: bool = False) -> str:
    """The body as readable text (paragraphs kept), without journal furniture or back matter."""
    kept = [line for line in _cut_back_matter(lines) if not _FURNITURE_LINE.match(line)]
    return _paragraph_text(kept, per_line)


class _Reading(NamedTuple):
    """One way of reading a file's structure: which lines, with which layout headings, and what came out."""

    lines: list[str]
    layout: frozenset[str]
    ref_blocks: list[list[str]]
    abstract: str
    sections: dict[str, str]
    roles: dict[str, str]
    covered: int
    body: str
    structure: str
    coverage: float
    titles: dict[str, str]


_STRUCTURE_RANK = {"detected": 2, "partial": 1, "none": 0}


def _front_lines(lines: list[str], reflow: bool) -> list[str]:
    """The body lines with only the known headings, for reading the label on the first page."""
    body_lines, _ = _split_reference_blocks(lines)
    return _reflow_lines(body_lines) if reflow else body_lines


def _read_structure(
    lines: list[str],
    layout: frozenset[str],
    reflow: bool,
    style_abstract: str | None,
    article_type: str | None,
    numbered_topics: bool = False,
    block_abstract: str | None = None,
) -> _Reading:
    """Split a file's lines into references, abstract and sections, with `layout` as its marked headings."""
    token = _LAYOUT_HEADINGS.set(layout)
    try:
        body_lines, ref_blocks = _split_reference_blocks(lines)
        if reflow:
            body_lines = _reflow_lines(body_lines)
        abstract = _extract_abstract(body_lines, style_abstract, block_abstract, paragraphs=not reflow)
        # the paragraphs after an abstract that was read from the file make up an introduction without a heading
        read_from_file = abstract != "Abstract not found." and (
            any(_ABSTRACT_LABEL.match(line) and line.strip() for line in body_lines[:200])
            or bool(style_abstract and abstract.startswith(style_abstract[:60]))
        )
        titles: dict[str, str] = {}
        sections, roles, covered = _extract_sections_detailed(
            body_lines, article_type, per_line=not reflow, numbered_topics=numbered_topics,
            abstract=abstract if read_from_file else "", titles_out=titles,
        )
        body = _body_text(body_lines, per_line=not reflow)
        structure, coverage = paper_profile.assess_structure(roles, covered, len(body), article_type)
    finally:
        _LAYOUT_HEADINGS.reset(token)
    titles = {key: title for key, title in titles.items() if key in sections}
    return _Reading(lines, layout, ref_blocks, abstract, sections, roles, covered, body, structure, coverage, titles)


def _structure_signature(sections: dict[str, str], roles: dict[str, str]) -> str:
    """A short fingerprint of the section names and roles, in order: equal for equal structures."""
    payload = json.dumps([[name, roles.get(name, "")] for name in sections], ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_document(
    full_text: str,
    one_reference_per_line: bool,
    article_type_hint: str | None = None,
    reflow: bool = False,
    style_abstract: str | None = None,
    label_lines: list[str] | None = None,
    layout_headings: tuple[tuple[str, ...], ...] = (),
    block_abstract: str | None = None,
    integrity_marks: list[FileMark] | None = None,
    doi_candidates: list[str] | None = None,
    title: str = "",
    front_text: str = "",
) -> ParsedDocument:
    """Shared text-to-structure step for every input format.

    `layout_headings` are the headings the file's own layout marks (bold or larger type, Word heading styles),
    each as the lines it is printed on. The file is read first with the known headings alone; only when that
    does not find the paper's structure is it read again with the layout headings, and the better reading is
    kept. Every step is a pure function of the file, so the same file always gives the same sections.
    """
    lines = full_text.split("\n")

    user_type = paper_profile.coerce_article_type(article_type_hint)
    label_type = paper_profile.detect_article_type(label_lines if label_lines else _front_lines(lines, reflow)[:60])
    article_type = user_type or label_type
    type_source = "user" if user_type else ("label" if label_type else "none")

    reading = _read_structure(
        lines, frozenset(), reflow, style_abstract, article_type, block_abstract=block_abstract
    )
    if reading.structure != "detected" and layout_headings:
        merged, keys = _merge_layout_headings(lines, layout_headings)
        with_layout = _read_structure(
            merged, keys, reflow, style_abstract, article_type, block_abstract=block_abstract
        )
        if (_STRUCTURE_RANK[with_layout.structure], with_layout.coverage) > (
            _STRUCTURE_RANK[reading.structure], reading.coverage
        ):
            reading = with_layout
    if reading.structure != "detected" and article_type is None:
        # Numbered headings ("1. Introduction", "2. Ring-opening polymerization", ...) name the sections of
        # many papers whose type is not printed on them (often reviews in an author's own layout). Taken from the file, they stay the same between runs.
        numbered = _read_structure(
            reading.lines, reading.layout, reflow, style_abstract, article_type,
            numbered_topics=True, block_abstract=block_abstract,
        )
        if numbered.structure == "detected":
            reading = numbered
    abstract, sections, roles, body = reading.abstract, reading.sections, reading.roles, reading.body
    structure, coverage, ref_blocks = reading.structure, reading.coverage, reading.ref_blocks
    references: list[str] = []
    numbered_total = 0
    for position, block in enumerate(ref_blocks):
        found, numbered = _extract_references_counted(
            block, one_reference_per_line, first_number=1 if position == 0 else None
        )
        references.extend(found)
        numbered_total += numbered
    references = references[:MAX_REFERENCES]
    word_count = len(full_text.split())
    warnings = _parse_warnings(ref_blocks, references, numbered_total, word_count)

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
        section_titles=dict(reading.titles),
        body=body,
        parse_warnings=warnings,
        structure_signature=_structure_signature(sections, roles),
        integrity_marks=list(integrity_marks or []),
        doi_candidates=list(doi_candidates or []),
        title=title,
        front_text=front_text or "\n".join(lines[:80]),
    )


class _DocxMark(NamedTuple):
    """How a Word paragraph is set: its heading level (1 = highest) from its style, and whether it is all bold."""

    level: int | None
    bold: bool
    size: float


_HEADING_STYLE_NAME = re.compile(r"(?i)^(?:heading|berschrift|titre|t[ií]tulo|rubrik)\s*(\d)$")


def _docx_heading_levels(document) -> dict[str, int]:
    """Style id -> heading level, for the styles that are headings by name ("Heading 2") or outline level."""
    levels: dict[str, int] = {}
    for style in document.styles.element.iterchildren(_W + "style"):
        style_id = style.get(_W + "styleId")
        name_element = style.find(_W + "name")
        name = (name_element.get(_W + "val") if name_element is not None else "") or ""
        named = _HEADING_STYLE_NAME.match(name.strip())
        outline = style.find(f"{_W}pPr/{_W}outlineLvl")
        if style_id and named:
            levels[style_id] = int(named.group(1))
        elif style_id and outline is not None and name.strip().lower() != "title":
            try:
                levels[style_id] = int(outline.get(_W + "val", "")) + 1
            except ValueError:
                continue
    return levels


def _docx_mark(paragraph, levels: dict[str, int]) -> _DocxMark:
    properties = paragraph.find(_W + "pPr")
    level: int | None = None
    if properties is not None:
        style = properties.find(_W + "pStyle")
        if style is not None:
            level = levels.get(style.get(_W + "val", ""))
        outline = properties.find(_W + "outlineLvl")
        if level is None and outline is not None:
            try:
                level = int(outline.get(_W + "val", "")) + 1
            except ValueError:
                level = None
    runs = [r for r in paragraph.iter(_W + "r") if "".join(t.text or "" for t in r.iter(_W + "t")).strip()]
    bold = bool(runs)
    size = 0.0
    for run in runs:
        run_properties = run.find(_W + "rPr")
        flag = run_properties.find(_W + "b") if run_properties is not None else None
        if flag is None or flag.get(_W + "val", "true").lower() in ("0", "false", "off"):
            bold = False
        if not size and run_properties is not None:
            run_size = run_properties.find(_W + "sz")
            if run_size is not None:
                try:
                    size = int(run_size.get(_W + "val", "0")) / 2
                except ValueError:
                    size = 0.0
    return _DocxMark(level, bold, size)


def _layout_headings_docx(
    paragraphs: list[str], marks: list[_DocxMark | None]
) -> tuple[tuple[str, ...], ...]:
    """Section headings of a Word file: its Heading styles, or else the repeating bold short paragraphs.

    The headings of a paper share one level and repeat, so the highest level that occurs at least
    MIN_LAYOUT_HEADINGS times is taken; a title (Heading 1 used once) is skipped by that.
    """
    styled: dict[int, list[str]] = defaultdict(list)
    bold: dict[float, list[str]] = defaultdict(list)
    for text, mark in zip(paragraphs, marks):
        if mark is None:
            continue
        cleaned = _clean_heading_text(text)
        if not _layout_heading_shape_ok(cleaned) or _NUMBER_ONLY.match(cleaned):
            continue
        if mark.level is not None:
            styled[mark.level].append(text)
        elif mark.bold:
            bold[mark.size].append(text)
    for level in sorted(styled):
        if MIN_LAYOUT_HEADINGS <= len(styled[level]) <= MAX_LAYOUT_HEADINGS:
            return tuple((t,) for t in styled[level])
    for size in sorted(bold, reverse=True):
        if MIN_LAYOUT_HEADINGS <= len(bold[size]) <= MAX_LAYOUT_HEADINGS:
            return tuple((t,) for t in bold[size])
    return ()


DOCX_FRONT_PARAGRAPHS = 40  # the "first page" of a Word file, for its status marks and its DOI


def _docx_paragraphs(file_bytes: bytes) -> list[str]:
    """Body text of a .docx as a list of non-empty paragraph strings, in reading order."""
    return _docx_paragraphs_marked(file_bytes)[0]


def _docx_core(document) -> dict[str, str]:
    """The file's own properties (title, subject, keywords, identifier) that can name the paper or its DOI."""
    try:
        props = document.core_properties
        values = {
            "title": props.title, "subject": props.subject, "keywords": props.keywords,
            "identifier": props.identifier, "comments": props.comments,
        }
    except Exception:  # a file without core properties
        return {}
    return {key: str(value) for key, value in values.items() if value}


def _docx_paragraphs_marked(file_bytes: bytes) -> tuple[list[str], list[_DocxMark | None], dict[str, str]]:
    """The paragraphs of a .docx, for each how it is set (None for a table row), and the file's properties."""
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
    marks: list[_DocxMark | None] = []
    try:
        levels = _docx_heading_levels(document)
    except Exception:  # a file without usable styles still has its text
        levels = {}
    _collect_docx_blocks(document.element.body, lines, marks, levels)
    return lines, marks, _docx_core(document)


def _collect_docx_blocks(
    parent, lines: list[str], marks: list[_DocxMark | None] | None = None, levels: dict[str, int] | None = None
) -> None:
    """Append the text of paragraphs, tables and content controls under `parent`, in order.

    When `marks` is given, one entry per appended line says how that paragraph is set.
    """
    for child in parent.iterchildren():
        tag = child.tag
        if tag == _W + "p":
            text = _docx_paragraph_text(child)
            if text:
                lines.append(text)
                if marks is not None:
                    marks.append(_docx_mark(child, levels or {}))
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
                    if marks is not None:
                        marks.append(None)
        elif tag == _W + "sdt":  # content control (cover pages, tables of contents)
            content = child.find(_W + "sdtContent")
            if content is not None:
                _collect_docx_blocks(content, lines, marks, levels)


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


# ---------------------------------------------------------------- headings
#
# A line is a heading when (1) its title is a known one, (2) the file's own layout says so (bold or larger
# type in a PDF, a Heading style in Word), or (3) it is numbered or in capitals and names a part of a paper
# ("3. EXPERIMENTAL RESULTS AND ANALYSIS"). Everything here is a pure function of the file's content, so the
# same file always gives the same sections.

# Marks a journal prints in front of a heading ("■ REFERENCES") are not part of its title.
# Whatever symbol a journal puts before its headings (■ ▪ • ·, or a private-use character from a symbol font)
# comes out of a PDF as a different character every time, so every leading character that is not a letter, a
# digit or an opening bracket is dropped.
_LEADING_ORNAMENTS = re.compile(r"^[^\w(\[]+")
MAX_KNOWN_HEADING_CHARS = 60
MAX_LAYOUT_HEADING_CHARS = 110
MAX_LAYOUT_HEADING_WORDS = 16
MAX_KEYWORD_HEADING_WORDS = 8
_FUNCTION_WORDS = frozenset({"of", "the", "and", "in", "for", "with", "to", "a", "an", "on", "by", "or", "at", "as"})

# Words that say which part of a paper a heading is. Results come first so "Experimental Results" is
# Results, not Methods. Used only for papers that are not reviews or perspectives (their headings are topics).
_ROLE_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    (paper_profile.ROLE_RESULTS, frozenset({"result", "results", "finding", "findings", "evaluation", "experiments"})),
    (paper_profile.ROLE_METHODS, frozenset({
        "method", "methods", "methodology", "materials", "experimental", "procedure", "procedures",
        "protocol", "protocols", "approach", "setup", "implementation",
    })),
    (paper_profile.ROLE_DISCUSSION, frozenset({
        "discussion", "conclusion", "conclusions", "outlook", "summary", "perspective", "perspectives",
        "implications", "concluding",
    })),
    (paper_profile.ROLE_INTRODUCTION, frozenset({"introduction", "background", "preliminaries"})),
)


def _clean_heading_text(line: str) -> str:
    """The line without leading marks, ligatures, "&" and trailing punctuation: "■ MATERIALS & METHODS:" -> "MATERIALS and METHODS"."""
    text = _LEADING_ORNAMENTS.sub("", line.strip().translate(_LIGATURE_TABLE))
    text = text.replace("&", " and ")
    return re.sub(r"\s+", " ", text).strip().rstrip(".:;").strip()


def _layout_key(text: str) -> str:
    return _clean_heading_text(text).casefold()


# Heading lines that the layout of the file marks (a set of _layout_key values), for the file being read.
_LAYOUT_HEADINGS: ContextVar[frozenset[str]] = ContextVar("layout_headings", default=frozenset())


def _keyword_role(title: str) -> str | None:
    words = set(re.findall(r"[a-z]+", title.lower()))
    for role, vocabulary in _ROLE_WORDS:
        if words & vocabulary:
            return role
    return None


def _heading_parts(line: str) -> tuple[str, str] | None:
    """(lowercase title, title as it is shown) when the line is a heading, else None."""
    text = _clean_heading_text(line)
    if not text or len(text) > MAX_LAYOUT_HEADING_CHARS:
        return None
    match = re.fullmatch(_HEADING_NUMBER + r"(.+)", text)
    if not match:
        return None
    raw_title = match.group(1).strip()
    # A heading starts with a capital. A lowercase word on a line by itself ("results", in the middle
    # of a sentence a justified PDF broke apart) is not a heading.
    if not raw_title[:1].isupper():
        return None
    title = raw_title.lower()
    shown = raw_title.capitalize() if _is_caps_heading_text(raw_title) else raw_title
    words = title.split()
    if len(text) <= MAX_KNOWN_HEADING_CHARS and title in _KNOWN_TITLES:
        return title, shown
    if len(words) > MAX_LAYOUT_HEADING_WORDS:
        return None
    if _layout_key(line) in _LAYOUT_HEADINGS.get():
        return title, shown
    numbered = match.start(1) > 0
    if (
        (numbered or _is_caps_heading_text(raw_title))
        and len(text) <= MAX_KNOWN_HEADING_CHARS
        and len(words) <= MAX_KEYWORD_HEADING_WORDS
        and words[-1] not in _FUNCTION_WORDS
        and _keyword_role(title) is not None
    ):
        return title, shown
    return None


def _heading_title(line: str) -> str | None:
    """Return the lowercase heading title if the line is a heading, else None."""
    parts = _heading_parts(line)
    return parts[0] if parts else None


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


MIN_REFERENCE_BLOCK_LINES = 3


def _split_reference_blocks(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """(body lines, reference blocks). Every References heading starts a block.

    A block runs until the next References heading, or until a heading of the paper itself (Methods,
    Acknowledgements, Supporting Information...) that follows at least a few lines of references. So a
    Nature or Cell paper, where Methods and a second reference list come after the main references, keeps
    its Methods in the body and both reference lists in the references.
    """
    starts = [i for i, line in enumerate(lines) if _heading_title(line) in REFERENCE_TITLES]
    if not starts:
        return lines, []
    body: list[str] = list(lines[: starts[0]])
    blocks: list[list[str]] = []
    for number, start in enumerate(starts):
        limit = starts[number + 1] if number + 1 < len(starts) else len(lines)
        end = limit
        for index in range(start + 1 + MIN_REFERENCE_BLOCK_LINES, limit):
            title = _heading_title(lines[index])
            if title is not None and title not in REFERENCE_TITLES:
                end = index
                break
        blocks.append(lines[start + 1 : end])
        body.extend(lines[end:limit])
    return body, blocks


def _parse_warnings(blocks: list[list[str]], references: list[str], numbered_total: int, word_count: int) -> list[str]:
    """What a reader of the report should know about how well the file could be read."""
    warnings: list[str] = []
    if not blocks and word_count >= MIN_WORDS_FOR_REFERENCE_WARNING:
        warnings.append("No reference list heading was found in the file, so its references were not checked.")
    elif numbered_total and len(references) < REFERENCE_RECALL_WARNING * numbered_total:
        warnings.append(
            f"Only {len(references)} of the {numbered_total} numbered references could be read from the file; "
            "the others were not checked."
        )
    return warnings


MIN_ABSTRACT_WORDS_BEFORE_BODY = 120  # an abstract this long is complete; a long paragraph after it is the body
MIN_BODY_PARAGRAPH_WORDS = 60


def _extract_abstract(
    lines: list[str],
    style_abstract: str | None = None,
    block_abstract: str | None = None,
    paragraphs: bool = False,
) -> str:
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

    if start is not None and block_abstract:
        # The PDF keeps the abstract in a block of its own. It ends where the block ends, even when the
        # next paragraph is set in the same typeface (no heading follows an abstract in many journals).
        first_words = inline or (lines[start].strip() if start < len(lines) else "")
        if first_words and _alnum(block_abstract).startswith(_alnum(first_words)[:25]):
            return block_abstract[:MAX_ABSTRACT_CHARS].strip()

    if start is not None:
        collected = [inline] if inline else []
        for index in range(start, len(lines)):
            if _heading_title(lines[index]) or _is_generic_heading(lines, index):
                break
            if re.match(r"(?i)^\s*(?:keywords?|index terms)\b", lines[index]):
                break
            if re.match(r"^\s*[∗†‡*]", lines[index]):  # author footnotes
                break
            if (
                paragraphs
                and sum(len(c.split()) for c in collected) >= MIN_ABSTRACT_WORDS_BEFORE_BODY
                and len(lines[index].split()) >= MIN_BODY_PARAGRAPH_WORDS
            ):
                break  # a Word file has one paragraph per line: a long one after a full abstract is the introduction
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
# IEEE and many conference layouts number their headings with Roman numerals: "IV. IDENTIFIED GAPS", "IX.REFERENCES"
_ROMAN_LEADING = re.compile(r"^\s*([IVX]{1,4})[\.\)]\s*([A-Z]\S.*)$")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10}
MAX_WRAPPED_HEADING_WORDS = 4


def _roman_value(numeral: str) -> int:
    total = 0
    for char, following in zip(numeral, numeral[1:] + " "):
        value = _ROMAN_VALUES[char]
        total += -value if following != " " and _ROMAN_VALUES.get(following, 0) > value else value
    return total


def _is_caps_heading_text(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _wrapped_heading(lines: list[str], index: int, title: str) -> tuple[str, int]:
    """(title, index of its last line). An all-capitals heading that wraps ("... IN EXISTING" / "RESEARCH") is one heading."""
    following = lines[index + 1].strip() if index + 1 < len(lines) else ""
    short_caps = (
        _is_caps_heading_text(title) and _is_caps_heading_text(following)
        and len(following.split()) <= MAX_WRAPPED_HEADING_WORDS and not following.endswith(".")
        and not _ROMAN_LEADING.match(following) and _heading_title(following) is None
    )
    long_title = (
        len(lines[index].strip()) > 60 and following and len(following) <= 40 and following[:1].isupper()
        and not following.endswith(".") and _heading_title(following) is None
    )
    if short_caps or long_title:
        return f"{title} {following}", index + 1
    return title, index


def _roman_topic(line: str, wanted_number: int) -> str | None:
    """Title of a Roman-numbered top-level heading ("V. PROPOSED SYSTEM") that is the next one in sequence."""
    match = _ROMAN_LEADING.match(line)
    if not match or _roman_value(match.group(1)) != wanted_number:
        return None
    title = match.group(2).strip().rstrip(":")
    if not (3 <= len(title) <= 90) or title.endswith("."):
        return None
    return title


def _is_roman_caps_heading(line: str) -> bool:
    match = _ROMAN_LEADING.match(line)
    return bool(match) and _is_caps_heading_text(match.group(2)) and len(match.group(2)) <= 90


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


MIN_OPENING_CHARS = 600  # shorter than this, the text before the first heading is front matter, not an introduction
_OPENING_SKIP = re.compile(r"(?i)^\s*(?:keywords?|index terms)\b|@|doi\s*:|©|\breceived\b|\baccepted\b")


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _opening_text(lines: list[str], abstract: str, per_line: bool) -> str:
    """The introduction that has no heading: the prose between the end of the abstract and the first heading.

    The end of the abstract is its last line found in the abstract text, so nothing is taken when the
    abstract was not read from the file itself.
    """
    abstract_key = _alnum(abstract)
    last_abstract_line = -1
    for index, line in enumerate(lines):
        key = _alnum(line)
        if len(key) >= 15 and key in abstract_key:
            last_abstract_line = index
    if last_abstract_line < 0:
        return ""
    kept = [
        line for line in lines[last_abstract_line + 1 :]
        if len(line.split()) >= 6 and not _OPENING_SKIP.search(line) and _heading_title(line) is None
        and not _FURNITURE_LINE.match(line) and not _META_LINE.search(line)
    ]
    text = _paragraph_text(kept, per_line)
    return text if len(text) >= MIN_OPENING_CHARS else ""


TABLE_HEADER_CELLS = 3  # short lines after a heading word before it counts as a table's column header
MAX_TABLE_CELL_WORDS = 4
TABLE_NUMERIC_SHARE = 0.35  # share of numbers and table signs in the lines after it


def _is_table_header(
    lines: list[str], index: int, role: str, boundaries: list[tuple[int, str | None, str | None]]
) -> bool:
    """A standard heading word that is really a table's first column header ("Method", "Results").

    It repeats a section the paper already has, and the lines after it are table cells (short, no prose),
    where a real heading is followed by prose or by a numbered sub-heading and then prose.
    """
    if not any(r == role for _, r, _ in boundaries):
        return False
    following = [line.strip() for line in lines[index + 1 : index + 12] if line.strip()][:TABLE_HEADER_CELLS]
    if len(following) < TABLE_HEADER_CELLS:
        return False
    cells = all(
        len(cell.split()) <= MAX_TABLE_CELL_WORDS and not cell.rstrip().endswith(".") and _heading_title(cell) is None
        for cell in following
    )
    # rows read as one line: mostly numbers and table signs ("4.52 4.30 0.948 N/A", "0.84 ± 1.45")
    tokens = " ".join(following).split()
    numeric = sum(1 for t in tokens if re.search(r"\d|^(?:N/A|NA|n\.d\.|[±=%<>–—-])$", t))
    return cells or (len(tokens) >= 6 and numeric / len(tokens) >= TABLE_NUMERIC_SHARE)


def _extract_sections_detailed(
    lines: list[str],
    article_type: str | None = None,
    per_line: bool = False,
    numbered_topics: bool = False,
    abstract: str = "",
    titles_out: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str], int]:
    """Sections under real headings: (sections, role of each, characters they cover).

    Standard headings map to introduction / methods / results / discussion. For perspectives and
    reviews every numbered top-level heading between them is a "thematic" section named by its title.
    `titles_out`, when given, receives each section's heading as the paper prints it ("Methodology",
    "Conclusion"), so a report can name the section as the author did while the key stays stable.
    """
    thematic_paper = numbered_topics or article_type in paper_profile.THEMATIC_TYPES
    boundaries: list[tuple[int, str | None, str | None]] = []  # (line, role, key)
    shown_at: dict[int, str] = {}  # boundary line -> heading as printed
    next_number = 1
    for index, line in enumerate(lines):
        parts_of_heading = _heading_parts(line)
        if parts_of_heading is not None:
            title, shown = parts_of_heading
            number = _LEADING_NUMBER.match(line)
            roman = _ROMAN_LEADING.match(line)
            if number and int(number.group(1)) == next_number:
                next_number += 1
            elif roman and _roman_value(roman.group(1)) == next_number:
                next_number += 1
            if title in _TITLE_TO_SECTION and _is_table_header(lines, index, _TITLE_TO_SECTION[title], boundaries):
                continue  # "Method" over a results table's columns, not a second Methods section
            if title in _TITLE_TO_SECTION or index not in shown_at:
                shown_at[index] = shown
            if title in _TITLE_TO_SECTION:
                role = _TITLE_TO_SECTION[title]
                boundaries.append((index, role, role))
            elif title in KEPT_OTHER_TITLES:
                boundaries.append((index, paper_profile.ROLE_OTHER, title))
            elif title in _KNOWN_TITLES:  # Acknowledgements, Supporting Information...: ends a section, is none
                boundaries.append((index, None, None))
            else:
                # a heading the file's layout (or its numbering) shows. In a paper with the usual parts it is
                # one of them when it says so ("Experimental Results and Analysis"); otherwise it is a section
                # named by its own title, so its text reaches the model instead of being glued to the one before.
                role = None if thematic_paper else _keyword_role(title)
                if role is not None:
                    boundaries.append((index, role, role))
                else:
                    boundaries.append((index, paper_profile.ROLE_THEMATIC, shown))
        elif _is_generic_heading(lines, index):
            following = _heading_title(lines[index + 1]) if index + 1 < len(lines) else None
            if following in _TITLE_TO_SECTION and _is_table_header(
                lines, index + 1, _TITLE_TO_SECTION[following], boundaries
            ):
                continue  # a page number above a table whose first column is headed "Method"
            boundaries.append((index, None, None))
        elif thematic_paper:
            topic = _topic_heading(line, next_number) or _roman_topic(line, next_number)
            if topic is not None:
                next_number += 1
                topic, last = _wrapped_heading(lines, index, topic)  # a long heading wrapped onto a second line
                if topic.isupper():
                    topic = topic.capitalize()
                boundaries.append((last, paper_profile.ROLE_THEMATIC, topic))
        elif _is_roman_caps_heading(line):
            # "IV. IDENTIFIED GAPS IN EXISTING RESEARCH": a heading of the paper itself. It is a section named by
            # its own title, so its text reaches the model instead of being glued to the section before it.
            topic = _roman_topic(line, next_number)
            if topic is not None:
                next_number += 1
                topic, last = _wrapped_heading(lines, index, topic)
                boundaries.append((last, paper_profile.ROLE_OTHER, topic.capitalize() if topic.isupper() else topic))
            else:
                _, last = _wrapped_heading(lines, index, _ROMAN_LEADING.match(line).group(2))  # type: ignore[union-attr]
                boundaries.append((last, None, None))

    parts: dict[str, list[str]] = {}
    roles: dict[str, str] = {}
    covered = 0
    left_out = False
    first_real = next((start for start, role, _ in boundaries if role is not None), None)
    if (
        abstract
        and first_real is not None
        and not any(role == paper_profile.ROLE_INTRODUCTION for _, role, _ in boundaries)
    ):
        # Many papers start straight after the abstract, with no "Introduction" heading above the first paragraphs.
        opening = _opening_text(lines[:first_real], abstract, per_line)
        if opening:
            parts["introduction"] = [opening]
            roles["introduction"] = paper_profile.ROLE_INTRODUCTION
            covered += len(opening)
    for position, (start, role, key) in enumerate(boundaries):
        if role is None or key is None:
            continue
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
        chunk = [
            line for line in lines[start + 1 : end]
            if not _NUMBER_ONLY.match(line.strip())
        ]
        text = _paragraph_text(chunk, per_line)
        if len(text) <= MIN_SECTION_CONTENT_LENGTH:
            continue
        if role == paper_profile.ROLE_THEMATIC and key not in parts:
            if sum(1 for r in roles.values() if r == paper_profile.ROLE_THEMATIC) >= MAX_THEMATIC_SECTIONS:
                left_out = True  # more topics than we keep
                continue
        parts.setdefault(key, []).append(text)
        roles[key] = role
        covered += len(text)
        if titles_out is not None and start in shown_at:
            printed = titles_out.get(key, "")
            heading = shown_at[start]
            if not printed:
                titles_out[key] = heading
            elif heading.lower() in _TITLE_TO_SECTION and heading.lower() not in printed.lower().split(" / "):
                # two standard headings under one key ("Discussion", then "Conclusion"); a sub-heading the
                # layout or a keyword made a section ("MI-centric Training Protocol") is part of it, not a name
                titles_out[key] = f"{printed} / {heading}"

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
    """Extract bibliography entries from the lines after the References heading."""
    return _extract_references_counted(ref_lines, one_per_line)[0]


def _extract_references_counted(
    ref_lines: list[str], one_per_line: bool = False, first_number: int | None = 1
) -> tuple[list[str], int]:
    """(references, number of entries a numbered list says it has, or 0).

    With `one_per_line` (Word files) every non-empty line is one reference, up to the next
    known heading such as Appendix or Acknowledgements. `first_number` is the number the list starts
    at; None takes it from the first numbered line (a second list that continues the numbering).
    """
    numbered_total = 0
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
        reference_section = re.sub(r"(?m)^([ \t]*\[\d{1,3}\])[ \t]*\n[ \t]*", r"\1 ", "\n".join(kept)).strip()
        if not reference_section:
            return [], 0
        starts = _numbered_starts(reference_section, first_number)
        if len(starts) >= MIN_NUMBERED_ENTRIES:
            numbered_total = int(starts[-1].group(1)) - int(starts[0].group(1)) + 1
        chunks = _split_reference_candidates(reference_section, first_number)
    cleaned: list[str] = []
    for chunk in chunks:
        chunk = _trim_after_reference(chunk)
        normalized = re.sub(r"\s+", " ", chunk.translate(_INVISIBLE_CHARS)).strip()
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
                return cleaned, numbered_total
    return cleaned, numbered_total


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
# A title may itself start with a number ("3 125 Years IACS: ..."), so a number then a capitalised word counts too.
_NUMBERED_START = re.compile(
    r"(?m)^[ \t]*[\[\(]?(\d{1,3})[\.\)\]]?[ \t]+(?=(?:\([a-z]\)\s*)?[\[A-Z]|\d{1,4}[ \t]+[A-Z][a-z])"
)
MAX_NUMBER_JUMP = 2  # tolerate an entry the PDF extraction lost
MIN_NUMBERED_ENTRIES = 3


def _numbered_starts(section: str, first: int | None = 1) -> list[re.Match[str]]:
    """The lines that start entries 1, 2, 3, ... (or first, first+1, ... when the list continues another)."""
    starts: list[re.Match[str]] = []
    expected = first
    for match in _NUMBERED_START.finditer(section):
        number = int(match.group(1))
        if expected is None:
            expected = number
        if expected <= number <= expected + MAX_NUMBER_JUMP:
            starts.append(match)
            expected = number + 1
    return starts


def _split_numbered_entries(section: str, first: int | None = 1) -> list[str] | None:
    """Split a numbered reference list using the numbers themselves.

    A line only counts as a new entry when its number is the next one in the sequence (1, 2, 3, ...),
    so a wrapped line that happens to start with a digit ("45 Angew.") is not mistaken for one.
    Returns None when the text is not a numbered list.
    """
    starts = _numbered_starts(section, first)
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
    if len(stripped) < 10 or len(re.findall(r"[A-Za-z]", stripped)) < 3:
        return None  # a page range on its own line ("1770–1782.") repeats on every page of references
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


def _split_reference_candidates(reference_section: str, first_number: int | None = 1) -> list[str]:
    """Split references using known citation formatting patterns."""
    numbered = _split_numbered_entries(reference_section, first_number)
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
