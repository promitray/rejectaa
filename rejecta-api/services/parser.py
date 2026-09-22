"""PDF parsing service for extracting structured academic paper content."""

from __future__ import annotations

import logging
import re

import fitz

from models import ParsedDocument

logger = logging.getLogger(__name__)

MAX_ABSTRACT_CHARS = 1500
MAX_SECTION_CHARS = 3000
MAX_REFERENCES = 60
MIN_REFERENCE_LENGTH = 20
MAX_FILE_SIZE_BYTES = 10_485_760
FALLBACK_ABSTRACT_CHARS = 600
MIN_SECTION_CONTENT_LENGTH = 50
ABSTRACT_PATTERN_1 = (
    r"(?i)abstract[\s\n]{0,10}([\s\S]{150,1500}?)(?=\n\s*(?:introduction|keywords|1[\.\s]|background))"
)
ABSTRACT_PATTERN_2 = r"(?i)\babstract\b[:\s\n]{1,10}([\s\S]{100,1200}?)(?=\n\s*\n\s*[A-Z])"
SECTION_PATTERNS: dict[str, str] = {
    "introduction": r"(?i)\b(?:1[\.\s]+)?introduction\b",
    "methods": r"(?i)\b(?:2[\.\s]+)?(?:method(?:s|ology)?|materials?\s+and\s+methods?)\b",
    "results": r"(?i)\b(?:3[\.\s]+)?(?:results?|findings?|experiments?)\b",
    "discussion": r"(?i)\b(?:4[\.\s]+)?(?:discussion|analysis)\b",
}
STOP_PATTERNS: list[str] = [
    r"(?i)\b(?:\d[\.\s]+)?(?:method|result|discussion|conclusion|reference|bibliography|acknowledge|appendix|supplementary|figure|table)\b"
]
REFERENCE_HEADING_PATTERN = r"(?i)\n\s*(?:references?|bibliography)\s*\n"
REF_SPLIT_PATTERN_A = r"\n(?=\[\d{1,3}\])"
REF_SPLIT_PATTERN_B = r"\n(?=\d{1,3}[\.\)]\s+[A-Z])"
REF_SPLIT_PATTERN_C = r"\n\n+"
REFERENCE_PREFIX_PATTERN = r"^\s*(?:\[\d{1,3}\]|\d{1,3}[\.\)])\s*"


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

    abstract = _extract_abstract(full_text)
    sections = _extract_sections(full_text)
    references = _extract_references(full_text)
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


def _extract_abstract(text: str) -> str:
    """Extract abstract text using regex patterns and fallback."""
    for pattern in (ABSTRACT_PATTERN_1, ABSTRACT_PATTERN_2):
        match = re.search(pattern, text)
        if match:
            abstract = match.group(1).strip()
            if abstract:
                return abstract[:MAX_ABSTRACT_CHARS].strip()

    fallback = text[:FALLBACK_ABSTRACT_CHARS].strip()
    return fallback if fallback else "Abstract not found."


def _extract_sections(text: str) -> dict[str, str]:
    """Extract core manuscript sections from the full text."""
    section_ranges: list[tuple[int, str, int]] = []
    for section_name, pattern in SECTION_PATTERNS.items():
        match = re.search(pattern, text)
        if match:
            section_ranges.append((match.start(), section_name, match.end()))

    section_ranges.sort(key=lambda item: item[0])
    extracted: dict[str, str] = {}

    for index, (start_pos, section_name, heading_end) in enumerate(section_ranges):
        section_end = len(text)
        if index + 1 < len(section_ranges):
            section_end = section_ranges[index + 1][0]

        stop_matches: list[int] = []
        for stop_pattern in STOP_PATTERNS:
            stop_match = re.search(stop_pattern, text[heading_end:section_end])
            if stop_match:
                stop_matches.append(heading_end + stop_match.start())
        if stop_matches:
            section_end = min(section_end, min(stop_matches))

        content = text[heading_end:section_end].strip()
        content = re.sub(r"^\s*[:\-\.\n\r\t ]+", "", content).strip()
        content = content[:MAX_SECTION_CHARS].strip()
        if len(content) > MIN_SECTION_CONTENT_LENGTH:
            extracted[section_name] = content

    return extracted


def _extract_references(text: str) -> list[str]:
    """Extract bibliography entries from trailing references section."""
    reference_section = _find_reference_section(text)
    if not reference_section:
        return []

    chunks = _split_reference_candidates(reference_section)
    cleaned: list[str] = []
    for chunk in chunks:
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


def _find_reference_section(text: str) -> str:
    """Return trailing reference section text after heading, if found."""
    matches = list(re.finditer(REFERENCE_HEADING_PATTERN, text))
    if not matches:
        return ""
    return text[matches[-1].end() :]


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
