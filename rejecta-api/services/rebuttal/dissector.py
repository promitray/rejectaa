"""services/rebuttal/dissector.py
Two-pass comment itemizer:
Pass 1: Deterministic regex splitting for reviewer headers, numbered items, bullet points.
Pass 2: Fallback handling for unstructured prose blobs without clear separators.
Assigns reviewer attribution (Reviewer 1, Reviewer 2, Editor) and sequential comment IDs (R1C1, R1C2...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class RawCommentItem:
    comment_id: str
    reviewer_id: str
    sequence_num: int
    raw_text: str


class CommentDissector:
    """Dissects unstructured or structured peer review reports into discrete, attributed comment items."""

    # Headers identifying a new reviewer block
    _REVIEWER_HEADER_RE = re.compile(
        r"(?:^|\n)\s*(?:[-=*#]{2,}\s*)?"
        r"(?:\[?\b(?:Reviewer|Referee)\s*#?\s*([0-9A-Za-z]+)\b|"
        r"\b(?:Editor|Associate Editor|Guest Editor)\b|"
        r"\bR([0-9]+)\s*[:\-])",
        re.IGNORECASE,
    )

    # Numbered / bulleted comment item prefixes
    _ITEM_SPLIT_RE = re.compile(
        r"(?:^|\n)\s*(?:"
        r"(?:\d+[\.\)]\s+)"
        r"|(?:\[?(?:Comment|Concern|Point|Critique|Issue)\s*#?\s*\d+\s*\]?\s*[:\-\.]?\s*)"
        r"|(?:[•\-\*]\s+)"
        r"|(?:\([a-zA-Z0-9]+\)\s+)"
        r")",
        re.MULTILINE,
    )

    @classmethod
    def dissect(cls, report_text: str) -> List[RawCommentItem]:
        """Dissect review report text into attributed raw comment items."""
        cleaned = report_text.strip()
        if not cleaned:
            return []

        # Sanitize: Strip any trailing author instructions/notes that got pasted into reviewer text
        author_note_patterns = [
            r"(?:\n|^)\s*(?:IMPORTANT\s+)?AUTHOR\s+NOTE\s*:.*$",
            r"(?:\n|^)\s*NOTE\s+FOR\s+THE\s+AUTHORS?\s*:.*$",
            r"(?:\n|^)\s*INTERNAL\s+NOTE\s*:.*$",
        ]
        for pat in author_note_patterns:
            cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()

        # Step 1: Split by reviewer sections if explicit headers exist
        reviewer_sections = cls._split_reviewer_sections(cleaned)

        all_items: List[RawCommentItem] = []
        for reviewer_id, section_text in reviewer_sections:
            items = cls._split_section_into_comments(reviewer_id, section_text)
            all_items.extend(items)

        # Fallback: if no items were extracted, treat the whole text as single comment
        if not all_items:
            all_items.append(
                RawCommentItem(
                    comment_id="R1C1",
                    reviewer_id="Reviewer 1",
                    sequence_num=1,
                    raw_text=cleaned,
                )
            )

        # Enforce unique comment IDs globally across each reviewer
        rev_seq_map: dict[str, int] = {}
        for item in all_items:
            prefix = "E" if item.reviewer_id.lower() == "editor" else "".join(filter(str.isdigit, item.reviewer_id))
            prefix = f"R{prefix}" if prefix and prefix.isdigit() else (prefix if prefix else "R1")
            rev_seq_map[prefix] = rev_seq_map.get(prefix, 0) + 1
            item.comment_id = f"{prefix}C{rev_seq_map[prefix]}"
            item.sequence_num = rev_seq_map[prefix]

        return all_items

    @classmethod
    def _split_reviewer_sections(cls, text: str) -> List[tuple[str, str]]:
        """Identify reviewer block headers and slice text accordingly."""
        matches = list(cls._REVIEWER_HEADER_RE.finditer(text))
        if not matches:
            return [("Reviewer 1", text)]

        sections: List[tuple[str, str]] = []

        # If there's text before the first reviewer header (e.g. editor comments)
        first_start = matches[0].start()
        if first_start > 50:
            prefix_text = text[:first_start].strip()
            if prefix_text:
                sections.append(("Editor", prefix_text))

        for idx, match in enumerate(matches):
            header_str = match.group(0).strip()
            rev_num = match.group(1) or match.group(2)

            if "editor" in header_str.lower():
                reviewer_id = "Editor"
            elif rev_num:
                reviewer_id = f"Reviewer {rev_num.strip()}"
            else:
                reviewer_id = f"Reviewer {idx + 1}"

            start_pos = match.end()
            end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[start_pos:end_pos].strip()

            if body:
                sections.append((reviewer_id, body))

        return sections if sections else [("Reviewer 1", text)]

    @classmethod
    def _split_section_into_comments(cls, reviewer_id: str, section_text: str) -> List[RawCommentItem]:
        """Split a single reviewer's text block into individual critique points."""
        cleaned = section_text.strip()
        if not cleaned:
            return []

        # Find all numbered/bullet item boundaries
        split_indices = [m.start() for m in cls._ITEM_SPLIT_RE.finditer(cleaned)]

        # Extract clean short prefix for ID: 'Reviewer 1' -> 'R1', 'Reviewer 2' -> 'R2', 'Editor' -> 'E'
        if "editor" in reviewer_id.lower():
            id_prefix = "E"
        else:
            digits = "".join(filter(str.isdigit, reviewer_id))
            id_prefix = f"R{digits}" if digits else "R1"
        items: List[RawCommentItem] = []

        if len(split_indices) >= 2:
            # Multi-item numbered or bulleted list found
            indices = split_indices + [len(cleaned)]
            # If there's substantial preface text before first item, keep as introductory point
            if indices[0] > 60:
                preface = cleaned[: indices[0]].strip()
                if len(preface.split()) >= 8:
                    items.append(
                        RawCommentItem(
                            comment_id=f"{id_prefix}C1",
                            reviewer_id=reviewer_id,
                            sequence_num=1,
                            raw_text=preface,
                        )
                    )

            for i in range(len(indices) - 1):
                start = indices[i]
                end = indices[i + 1]
                chunk = cleaned[start:end].strip()
                # Clean leading numbering/bullets
                chunk_clean = cls._ITEM_SPLIT_RE.sub("", chunk, count=1).strip()
                if chunk_clean and len(chunk_clean.split()) >= 4:
                    items.append(
                        RawCommentItem(
                            comment_id=f"{id_prefix}C{len(items) + 1}",
                            reviewer_id=reviewer_id,
                            sequence_num=len(items) + 1,
                            raw_text=chunk_clean,
                        )
                    )
        else:
            # No clear bullet/numbered format: split by double newlines (paragraphs)
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", cleaned) if p.strip()]
            valid_paras = [p for p in paragraphs if len(p.split()) >= 6]

            if len(valid_paras) > 1:
                for idx, para in enumerate(valid_paras, start=1):
                    items.append(
                        RawCommentItem(
                            comment_id=f"{id_prefix}C{idx}",
                            reviewer_id=reviewer_id,
                            sequence_num=idx,
                            raw_text=para,
                        )
                    )
            else:
                # Single paragraph/comment
                items.append(
                    RawCommentItem(
                        comment_id=f"{id_prefix}C1",
                        reviewer_id=reviewer_id,
                        sequence_num=1,
                        raw_text=cleaned,
                    )
                )

        return items
