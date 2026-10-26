"""PDF parsing service for extracting structured academic paper content."""

from __future__ import annotations

import logging
import re

import fitz

from models import ParsedDocument

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
        "methods", "method", "methodology", "materials and methods",
        "model architecture", "approach", "experimental setup",
        "training", "experimental design", "study design",
    ),
    "results": (
        "results", "findings", "experiments", "evaluation",
        "experimental results", "results and discussion",
    ),
    "discussion": (
        "discussion", "conclusion", "conclusions", "discussion and conclusion",
        "conclusions and future work", "concluding remarks",
    ),
}
# Headings that end a section without being one we keep.
OTHER_TITLES = (
    "abstract", "background", "related work", "literature review",
    "why self-attention", "acknowledgements", "acknowledgments",
    "acknowledgement", "appendix", "supplementary material", "keywords",
    "index terms", "limitations", "future work",
)
REFERENCE_TITLES = ("references", "bibliography", "literature cited")

_TITLE_TO_SECTION = {t: name for name, titles in SECTION_TITLES.items() for t in titles}
_KNOWN_TITLES = set(_TITLE_TO_SECTION) | set(OTHER_TITLES) | set(REFERENCE_TITLES)
_HEADING_NUMBER = r"(?:(?:\d{1,2}|[IVX]{1,4})[\.\)]?\s+)?"
_NUMBER_ONLY = re.compile(r"^\d{1,2}$")
_GENERIC_TITLE = re.compile(r"^[A-Z][A-Za-z\-, ]{2,45}$")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
REF_SPLIT_PATTERN_A = r"\n(?=\[\d{1,3}\])"
REF_SPLIT_PATTERN_B = r"\n(?=\d{1,3}[\.\)]\s+[A-Z])"
REF_SPLIT_PATTERN_C = r"\n\n+"
REFERENCE_PREFIX_PATTERN = r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*"
_REF_END_YEAR = re.compile(r"\b(?:19|20)\d{2}[a-z]?\.$")


def parse_pdf(file_bytes: bytes) -> ParsedDocument:
    """Parse a PDF academic paper and extract structured content.

    Args:
        file_bytes: Raw bytes of the PDF file.

    Returns:
        ParsedDocument with extracted text, abstract, sections,
        references, and word count.

    Raises:
        ValueError: If the PDF cannot be opened or parsed.
    """
    document: fitz.Document | None = None
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        page_texts = [page.get_text() for page in document]
        full_text = "\n".join(page_texts).strip()
        logger.debug("Extracted text from %d pages", len(page_texts))
    except (ValueError, RuntimeError, TypeError, fitz.FileDataError) as exc:
        raise ValueError(
            "Could not open PDF — file may be corrupted or encrypted"
        ) from exc
    finally:
        if document is not None:
            document.close()

    lines = full_text.split("\n")
    ref_start = _find_reference_line(lines)
    body_lines = lines[:ref_start] if ref_start is not None else lines
    ref_lines = lines[ref_start + 1 :] if ref_start is not None else []

    abstract = _extract_abstract(body_lines)
    sections = _extract_sections(body_lines)
    references = _extract_references(ref_lines)
    word_count = len(full_text.split())

    logger.debug("Detected sections: %s", sorted(sections.keys()))
    logger.debug("Extracted %d references", len(references))

    return ParsedDocument(
        full_text=full_text,
        abstract=abstract,
        sections=sections,
        references=references,
        word_count=word_count,
    )


def _heading_title(line: str) -> str | None:
    """Return the lowercase heading title if the line is a heading, else None."""
    stripped = line.strip().rstrip(".:").strip()
    if not stripped or len(stripped) > 60:
        return None
    match = re.fullmatch(_HEADING_NUMBER + r"(.+)", stripped)
    if not match:
        return None
    title = match.group(1).strip().lower()
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


def _extract_abstract(lines: list[str]) -> str:
    """Take the text between the Abstract heading and the next heading."""
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

    head = re.sub(r"\s+", " ", _EMAIL.sub("", " ".join(lines[:80]))).strip()
    fallback = head[:FALLBACK_ABSTRACT_CHARS].strip()
    return fallback if fallback else "Abstract not found."


def _extract_sections(lines: list[str]) -> dict[str, str]:
    """Extract core manuscript sections using heading lines only."""
    boundaries: list[tuple[int, str | None]] = []
    for index, line in enumerate(lines):
        title = _heading_title(line)
        if title is not None:
            boundaries.append((index, _TITLE_TO_SECTION.get(title)))
        elif _is_generic_heading(lines, index):
            boundaries.append((index, None))

    parts: dict[str, list[str]] = {}
    for position, (start, section) in enumerate(boundaries):
        if section is None:
            continue
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else len(lines)
        chunk = [
            line for line in lines[start + 1 : end]
            if not _NUMBER_ONLY.match(line.strip())
        ]
        text = re.sub(r"\s+", " ", " ".join(chunk)).strip()
        if len(text) > MIN_SECTION_CONTENT_LENGTH:
            parts.setdefault(section, []).append(text)

    extracted = {
        name: " ".join(chunks)[:MAX_SECTION_CHARS].strip()
        for name, chunks in parts.items()
    }
    if not extracted:
        body = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if body:
            extracted["body"] = body[:FALLBACK_BODY_CHARS]
    return extracted


def _extract_references(ref_lines: list[str]) -> list[str]:
    """Extract bibliography entries from the lines after the References heading."""
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
        if len(normalized) < MIN_REFERENCE_LENGTH:
            continue
        if _looks_like_heading(normalized):
            continue
        cleaned.append(normalized)
        if len(cleaned) >= MAX_REFERENCES:
            break
    return cleaned


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


def _split_reference_candidates(reference_section: str) -> list[str]:
    """Split references using known citation formatting patterns."""
    patterns = (REF_SPLIT_PATTERN_A, REF_SPLIT_PATTERN_B, REF_SPLIT_PATTERN_C)
    for pattern in patterns:
        chunks = [chunk.strip() for chunk in re.split(pattern, reference_section) if chunk.strip()]
        if len(chunks) > 1:
            return chunks
    fallback = reference_section.strip()
    return [fallback] if fallback else []


def _looks_like_heading(value: str) -> bool:
    """Heuristic check for short uppercase section-heading-like text."""
    return len(value) < 60 and value.upper() == value
