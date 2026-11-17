"""The paper as numbered paragraphs and sentences, so the model cites ids instead of copying text.

Every paragraph of the abstract and of each section gets an id in reading order ("P1", "P2", ...) and every
sentence an id under it ("P12.3"). The model is shown the text with these ids and answers with ids; code then
takes the words from the index. A quotation in the report is therefore always the paper's own text: it cannot
be misquoted, shortened into another meaning, or invented.

The ids depend only on the text, so the same file always gets the same ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ABSTRACT = "abstract"
MAX_PARAGRAPH_CHARS = 1400  # a longer "paragraph" (merged PDF lines) is split, at sentence ends, into pieces
MIN_SENTENCE_CHARS = 25  # shorter pieces ("(i)", "2.") are joined to the sentence before them
_ID = re.compile(r"^P(\d+)(?:\.(\d+))?$")

# A full stop after one of these is not the end of a sentence.
_ABBREVIATIONS = frozenset(
    "fig figs eq eqs ref refs tab sec secs al e.g i.e vs cf approx ca resp no nos dr mr ms prof inc ltd co "
    "st vol vols pp ed eds etc min max avg std sd mol wt aq conc temp exp suppl".split()
)
_BOUNDARY = re.compile(r"[.!?][\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


@dataclass(frozen=True)
class Sentence:
    id: str
    paragraph: str
    section: str
    text: str


@dataclass(frozen=True)
class Paragraph:
    id: str
    section: str
    position: int  # reading order, from 1
    sentence_ids: tuple[str, ...]
    text: str


@dataclass
class PaperIndex:
    paragraphs: dict[str, Paragraph] = field(default_factory=dict)
    sentences: dict[str, Sentence] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)  # section name -> paragraph ids, in order

    # ---- reading
    def section_of(self, item_id: str) -> str | None:
        para = self.paragraphs.get(item_id) or self.paragraphs.get(item_id.split(".")[0])
        return para.section if para else None

    def text_of(self, ids: list[str] | tuple[str, ...]) -> str:
        """The paper's words for sentence or paragraph ids, in reading order, joined with "…" across gaps."""
        ordered = sorted({i for i in ids if self.has(i)}, key=sort_key)
        parts: list[str] = []
        previous: tuple[int, int] | None = None
        for item in ordered:
            key = sort_key(item)
            text = self.sentences[item].text if item in self.sentences else self.paragraphs[item].text
            if parts and previous is not None and not _adjacent(previous, key, self):
                parts.append("…")
            parts.append(text)
            previous = key if item in self.sentences else (key[0], len(self.paragraphs[item].sentence_ids))
        return " ".join(parts)

    def has(self, item_id: str) -> bool:
        return item_id in self.sentences or item_id in self.paragraphs

    def numbered(self, section: str, max_chars: int | None = None) -> str:
        """A section as the model sees it: one line per sentence, each with its id."""
        lines: list[str] = []
        size = 0
        for pid in self.sections.get(section, []):
            for sid in self.paragraphs[pid].sentence_ids:
                line = f"[{sid}] {self.sentences[sid].text}"
                if max_chars is not None and size + len(line) > max_chars:
                    lines.append("[… rest of the section not shown …]")
                    return "\n".join(lines)
                lines.append(line)
                size += len(line) + 1
            lines.append("")
        return "\n".join(lines).strip()

    def numbered_paragraphs(self, paragraph_ids: list[str]) -> str:
        """Retrieved passages for the model: each sentence with its id, the paragraph's section named once."""
        blocks: list[str] = []
        for pid in sorted(set(paragraph_ids), key=sort_key):
            para = self.paragraphs.get(pid)
            if para is None:
                continue
            lines = [f"({para.section})"] + [f"[{sid}] {self.sentences[sid].text}" for sid in para.sentence_ids]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def chunks(self) -> list[dict[str, str | int]]:
        """The paragraphs as the units that are embedded and searched."""
        return [{"id": p.id, "position": p.position, "section": p.section, "text": p.text} for p in self.paragraphs.values()]


def sort_key(item_id: str) -> tuple[int, int]:
    match = _ID.match(item_id)
    if not match:
        return (10**9, 0)
    return int(match.group(1)), int(match.group(2) or 0)


def _adjacent(previous: tuple[int, int], current: tuple[int, int], index: PaperIndex) -> bool:
    if previous[0] == current[0]:
        return current[1] == previous[1] + 1
    para = index.paragraphs.get(f"P{previous[0]}")
    return current[0] == previous[0] + 1 and current[1] <= 1 and para is not None and previous[1] >= len(para.sentence_ids)


def split_sentences(text: str) -> list[str]:
    """Sentences of one paragraph. A full stop after an abbreviation ("Fig.", "et al.", "e.g.") or an initial
    ("J. Smith") does not end a sentence; very short pieces are joined to the sentence before them."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    pieces: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        before = text[start : match.start() + 1]
        last_word = re.findall(r"([A-Za-z][A-Za-z.]*)\.$", before)
        if last_word:
            word = last_word[0].lower().rstrip(".")
            if word in _ABBREVIATIONS or len(word) == 1:
                continue
        pieces.append(text[start : match.end()].strip())
        start = match.end()
    pieces.append(text[start:].strip())
    out: list[str] = []
    for piece in pieces:
        if out and len(piece) < MIN_SENTENCE_CHARS:
            out[-1] = f"{out[-1]} {piece}"
        elif piece:
            out.append(piece)
    if len(out) > 1 and len(out[0]) < MIN_SENTENCE_CHARS:
        out[1] = f"{out[0]} {out[1]}"
        out.pop(0)
    return out


def _paragraph_texts(text: str) -> list[str]:
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "")]
    out: list[str] = []
    for para in paragraphs:
        if not para:
            continue
        if len(para) <= MAX_PARAGRAPH_CHARS:
            out.append(para)
            continue
        current = ""
        for sentence in split_sentences(para):  # a long block is cut at sentence ends into readable pieces
            if current and len(current) + len(sentence) + 1 > MAX_PARAGRAPH_CHARS:
                out.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            out.append(current)
    return out


def build_index(abstract: str, sections: dict[str, str]) -> PaperIndex:
    """The index of a paper: the abstract first, then each section in the order given (the paper's order)."""
    index = PaperIndex()
    position = 0
    ordered = ([(ABSTRACT, abstract)] if abstract and abstract.strip() else []) + [
        (name, text) for name, text in sections.items() if name != ABSTRACT
    ]
    for section, text in ordered:
        ids: list[str] = []
        for para_text in _paragraph_texts(text):
            sentences = split_sentences(para_text)
            if not sentences:
                continue
            position += 1
            pid = f"P{position}"
            sids = tuple(f"{pid}.{n}" for n in range(1, len(sentences) + 1))
            for sid, sentence in zip(sids, sentences):
                index.sentences[sid] = Sentence(sid, pid, section, sentence)
            index.paragraphs[pid] = Paragraph(pid, section, position, sids, " ".join(sentences))
            ids.append(pid)
        if ids:
            index.sections[section] = ids
    return index


_CITED_ID = re.compile(r"\bP\d+(?:\.\d+)?\b")


def ids_in(value: object) -> list[str]:
    """Ids from a model's answer: a list of ids, or ids written inside a string ("P12.3, P12.4")."""
    if isinstance(value, str):
        return _CITED_ID.findall(value)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(ids_in(item))
        return out
    return []
