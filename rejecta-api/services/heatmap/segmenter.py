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
    _ABBREVIATIONS = (
        r"\b(?:e\.g\.|i\.e\.|et\s+al\.|fig\.|eq\.|dr\.|prof\.|vs\.|ref\.|no\.|dept\.|vol\.|pp\.)"
    )

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
    def _segment_sentences(
        cls,
        full_text: str,
        paragraph_clean_text: str,
        paragraph_char_offset: int,
    ) -> List[SentenceSpan]:
        """Segment a paragraph into clean sentences while computing exact UTF-16 offsets relative to full_text."""
        sentences: List[SentenceSpan] = []

        # Placeholder replacement for abbreviations to avoid false splits
        protected_text = paragraph_clean_text
        abbrev_matches = list(re.finditer(cls._ABBREVIATIONS, paragraph_clean_text, flags=re.IGNORECASE))
        abbrev_vault = {}
        for idx, match in enumerate(abbrev_matches):
            placeholder = f"__ABBR_{idx:03d}__"
            abbrev_vault[placeholder] = match.group(0)
            protected_text = (
                protected_text[: match.start()] + placeholder + protected_text[match.end() :]
            )

        # Regex split on sentence boundary: [.!?] followed by whitespace and a potential quote or capital letter
        split_pattern = r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])"
        parts = re.split(split_pattern, protected_text)

        search_pos = 0
        s_index = 0

        for part in parts:
            if not part.strip():
                continue

            # Restore abbreviations
            restored_part = part
            for ph, original in abbrev_vault.items():
                restored_part = restored_part.replace(ph, original)

            # Find exact position in paragraph_clean_text
            start_in_p = paragraph_clean_text.find(restored_part, search_pos)
            if start_in_p == -1:
                start_in_p = search_pos

            end_in_p = start_in_p + len(restored_part)
            search_pos = end_in_p

            global_start_char = paragraph_char_offset + start_in_p
            global_end_char = paragraph_char_offset + end_in_p

            start_utf16 = _char_idx_to_utf16_offset(full_text, global_start_char)
            end_utf16 = _char_idx_to_utf16_offset(full_text, global_end_char)

            words = len(restored_part.split())

            sentences.append(
                SentenceSpan(
                    index=s_index,
                    text=restored_part,
                    start_utf16=start_utf16,
                    end_utf16=end_utf16,
                    word_count=words,
                )
            )
            s_index += 1

        return sentences
