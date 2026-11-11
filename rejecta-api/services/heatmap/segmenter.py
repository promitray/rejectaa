"""services/heatmap/segmenter.py
Deterministic paragraph and sentence segmentation engine with exact UTF-16 code unit offset tracking.
Ensures zero-drift slicing in browser JavaScript (text.slice(start_utf16, end_utf16)).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class SentenceSpan:
    index: int
    text: str
    start_utf16: int
    end_utf16: int
    word_count: int


@dataclass(frozen=True)
class ParagraphSpan:
    index: int
    text: str
    start_utf16: int
    end_utf16: int
    sentences: List[SentenceSpan]
    word_count: int


def _utf16_len(s: str) -> int:
    """Return the length of the string in UTF-16 code units (as JavaScript measures it).

    Characters outside the Basic Multilingual Plane (BMP, code points > 0xFFFF)
    require a surrogate pair (2 code units) in UTF-16.
    """
    return len(s.encode("utf-16-le")) // 2


def _char_idx_to_utf16_offset(full_text: str, char_idx: int) -> int:
    """Convert a Python character index into a UTF-16 code unit offset."""
    return _utf16_len(full_text[:char_idx])


class DocumentSegmenter:
    """Segments academic text into paragraphs and sentences with guaranteed UTF-16 code-unit offsets.

    Protects abbreviations, citations, equations, and decimal points from premature sentence splitting.
    """

    # Sentence boundary regex recognizing end of sentence followed by space and capital letter or bracket
    # Handles common abbreviations: e.g., i.e., et al., Fig., Eq., Dr., Prof., vs., Ref.
    _ABBREVIATIONS = re.compile(
        r"\b(?:e\.g|i\.e|et\s+al|figs?|eqs?|dr|prof|vs|refs?|no|dept|vol|pp|ca|approx|cf|resp|sect|ch|st|mr|ms)\.$",
        re.IGNORECASE,
    )
    # End of a sentence: . ! ? optionally followed by a citation number printed after it ("delivery.3 In"),
    # then space and the start of the next sentence.
    _SENTENCE_END = re.compile(r"[.!?](\d{1,3}[a-z]?(?:[,\u2013\-]\d{1,3}[a-z]?)*)?(?=\s+[A-Z0-9\"'(\[])")

    @classmethod
    def segment(cls, text: str) -> List[ParagraphSpan]:
        """Split text into structured ParagraphSpan and SentenceSpan objects with UTF-16 offsets."""
        paragraphs: List[ParagraphSpan] = []
        raw_paragraphs = re.split(r"(\r?\n\s*\r?\n)", text)

        current_char_offset = 0
        p_index = 0

        for chunk in raw_paragraphs:
            if not chunk:
                continue

            chunk_char_len = len(chunk)
            chunk_start_char = current_char_offset
            chunk_end_char = current_char_offset + chunk_char_len

            # If the chunk is just blank spacing/newlines, advance cursor without creating an empty paragraph
            if not chunk.strip():
                current_char_offset = chunk_end_char
                continue

            # Strip leading/trailing whitespaces within the paragraph while maintaining accurate offsets
            leading_ws_match = re.match(r"^\s*", chunk)
            leading_ws_len = len(leading_ws_match.group(0)) if leading_ws_match else 0
            trailing_ws_match = re.search(r"\s*$", chunk)
            trailing_ws_len = len(trailing_ws_match.group(0)) if trailing_ws_match else 0

            p_clean_start_char = chunk_start_char + leading_ws_len
            p_clean_end_char = chunk_end_char - trailing_ws_len
            p_clean_text = chunk.strip()

            p_start_utf16 = _char_idx_to_utf16_offset(text, p_clean_start_char)
            p_end_utf16 = _char_idx_to_utf16_offset(text, p_clean_end_char)

            # Segment the paragraph into sentences
            sentences = cls._segment_sentences(
                full_text=text,
                paragraph_clean_text=p_clean_text,
                paragraph_char_offset=p_clean_start_char,
            )

            p_words = len(p_clean_text.split())

            paragraphs.append(
                ParagraphSpan(
                    index=p_index,
                    text=p_clean_text,
                    start_utf16=p_start_utf16,
                    end_utf16=p_end_utf16,
                    sentences=sentences,
                    word_count=p_words,
                )
            )

            p_index += 1
            current_char_offset = chunk_end_char

        return paragraphs

    @classmethod
    def _split_sentences(cls, text: str) -> List[str]:
        """Sentences of a paragraph.

        A full stop after an abbreviation (Fig., e.g., et al.) does not end a sentence. A citation number glued
        after the full stop stays with its sentence; it counts as one only when a word or a bracket comes before
        the full stop, so a decimal such as "pH 7.4" is never taken for a citation.
        """
        parts: List[str] = []
        start = 0
        for match in cls._SENTENCE_END.finditer(text):
            stop = match.start()
            before = text[stop - 1] if stop > 0 else ""
            if match.group(1) and not (before.isalpha() or before in ")]"):
                continue
            if text[stop] == "." and cls._ABBREVIATIONS.search(text[max(0, stop - 12) : stop + 1]):
                continue
            parts.append(text[start : match.end()])
            start = match.end()
        parts.append(text[start:])
        return [part for part in parts if part.strip()]

    @classmethod
    def _segment_sentences(
        cls,
        full_text: str,
        paragraph_clean_text: str,
        paragraph_char_offset: int,
    ) -> List[SentenceSpan]:
        """Segment a paragraph into clean sentences while computing exact UTF-16 offsets relative to full_text."""
        sentences: List[SentenceSpan] = []
        search_pos = 0
        for s_index, part in enumerate(cls._split_sentences(paragraph_clean_text)):
            text = part.strip()
            start_in_p = paragraph_clean_text.find(text, search_pos)
            if start_in_p == -1:
                start_in_p = search_pos
            end_in_p = start_in_p + len(text)
            search_pos = end_in_p
            sentences.append(
                SentenceSpan(
                    index=s_index,
                    text=text,
                    start_utf16=_char_idx_to_utf16_offset(full_text, paragraph_char_offset + start_in_p),
                    end_utf16=_char_idx_to_utf16_offset(full_text, paragraph_char_offset + end_in_p),
                    word_count=len(text.split()),
                )
            )
        return sentences
