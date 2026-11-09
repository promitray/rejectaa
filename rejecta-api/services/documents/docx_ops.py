"""Paragraph-level reading and editing of a working Word copy, keeping the formatting that carries meaning.

Text that must survive a rewrite unchanged (superscript citation numbers, subscripts, reference fields,
hyperlinks, inline equations, footnote marks, short bold or italic symbols such as compound labels) is
replaced by a token before the text goes to the model. Each token remembers the exact Word XML it stands
for, so when the rewritten text comes back the token is turned into that same XML again.
"""

from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field

from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from lxml import etree

from services.parser import _docx_paragraph_text, _heading_title

W_P = qn("w:p")
W_R = qn("w:r")
W_T = qn("w:t")
W_RPR = qn("w:rPr")
W_PPR = qn("w:pPr")
W_PSTYLE = qn("w:pStyle")
_MATH_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

FORMAT_TOKEN_FIRST = 5000  # the calibrator's own tokens count up from 0000, so these never collide
TOKEN_PATTERN = re.compile(r"__REJ_INV_\d{4}__")
MAX_CAPTION_CHARS = 600
_CAPTION = re.compile(r"^(?:Fig(?:ure)?s?\.?|Scheme|Table|Chart|Graph)\s*S?\d+[A-Za-z]?\b", re.IGNORECASE)
SHORT_STYLED_CHARS = 4  # a bold or italic run this short is a symbol or label (P1, Mn, Đ), not prose

# Children of a run that are only text. Anything else (a drawing, a footnote mark, a field character) makes it special.
_PLAIN_RUN_CHILDREN = frozenset(
    _W_NS + name for name in ("rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "lastRenderedPageBreak")
)
# Paragraph children kept whole as one token.
_WHOLE_ELEMENTS = frozenset({_W_NS + "hyperlink", _W_NS + "fldSimple", _W_NS + "sdt", _MATH_NS + "oMath", _W_NS + "smartTag"})
# Paragraph children that hold no visible text of their own.
_SKIPPED = frozenset(
    _W_NS + name
    for name in (
        "pPr", "del", "moveFrom", "bookmarkStart", "bookmarkEnd", "proofErr", "commentRangeStart",
        "commentRangeEnd", "permStart", "permEnd",
    )
)


def open_docx(data: bytes):
    return Document(io.BytesIO(data))


def save_docx(document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def body_paragraphs(document) -> list:
    """Top-level paragraphs of the body, in order (paragraphs inside tables are not addressed)."""
    return list(document.element.body.iterchildren(W_P))


def paragraph_text(p_el) -> str:
    return _docx_paragraph_text(p_el)


def paragraph_kind(p_el, text: str | None = None) -> str:
    """'text' (prose that may be rewritten), 'heading', 'caption', 'figure' or 'empty'."""
    text = paragraph_text(p_el) if text is None else text
    has_drawing = bool(p_el.findall(".//" + _W_NS + "drawing")) or bool(p_el.findall(".//" + _W_NS + "pict"))
    if not text:
        return "figure" if has_drawing else "empty"
    style = p_el.find(W_PPR + "/" + W_PSTYLE) if p_el.find(W_PPR) is not None else None
    style_name = (style.get(qn("w:val")) or "").lower() if style is not None else ""
    if style_name.startswith("heading") or style_name == "title" or _heading_title(text) is not None:
        return "heading"
    if style_name == "caption" or (_CAPTION.match(text) and len(text) <= MAX_CAPTION_CHARS):
        return "caption"
    if has_drawing and len(text) < 40:
        return "figure"
    return "text"


def _run_text(run) -> str:
    parts: list[str] = []
    for child in run.iterchildren():
        if child.tag == W_T:
            parts.append(child.text or "")
        elif child.tag in (_W_NS + "tab", _W_NS + "br", _W_NS + "cr"):
            parts.append(" ")
        elif child.tag == _W_NS + "noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def _element_text(element) -> str:
    texts = [t.text or "" for t in element.iter(W_T, _MATH_NS + "t")]
    return "".join(texts)


def _run_is_special(run) -> bool:
    rpr = run.find(W_RPR)
    if any(child.tag not in _PLAIN_RUN_CHILDREN for child in run.iterchildren()):
        return True
    if rpr is None:
        return False
    if rpr.find(_W_NS + "vertAlign") is not None:
        return True
    styled = rpr.find(_W_NS + "b") is not None or rpr.find(_W_NS + "i") is not None
    return styled and 0 < len(_run_text(run).strip()) <= SHORT_STYLED_CHARS


@dataclass
class TokenizedSelection:
    """Selected paragraphs as one text with tokens, ready for the calibrator."""

    masked_text: str
    tokens: dict[str, list[str]] = field(default_factory=dict)  # token -> serialized Word XML elements
    token_text: dict[str, str] = field(default_factory=dict)  # token -> the visible text it stands for


def tokenize(paragraphs: list) -> TokenizedSelection:
    """Turn paragraphs into text, putting every formatted element that must not change behind a token."""
    selection = TokenizedSelection(masked_text="")
    counter = [FORMAT_TOKEN_FIRST]
    texts: list[str] = []

    def new_token(elements: list) -> str:
        token = f"__REJ_INV_{counter[0]:04d}__"
        counter[0] += 1
        selection.tokens[token] = [etree.tostring(el, encoding="unicode") for el in elements]
        selection.token_text[token] = "".join(_element_text(el) for el in elements)
        return token

    for p_el in paragraphs:
        parts: list[str] = []
        field_group: list | None = None  # runs of a complex field (citation manager), from begin to end
        depth = 0
        for child in _visible_children(p_el):
            if field_group is not None:
                field_group.append(child)
                for char in child.iter(_W_NS + "fldChar"):
                    kind = char.get(qn("w:fldCharType"))
                    depth += 1 if kind == "begin" else -1 if kind == "end" else 0
                if depth == 0:
                    parts.append(new_token(field_group))
                    field_group = None
                continue
            begins = [c for c in child.iter(_W_NS + "fldChar") if c.get(qn("w:fldCharType")) == "begin"]
            if child.tag == W_R and begins:
                field_group = [child]
                depth = 0
                for char in child.iter(_W_NS + "fldChar"):
                    kind = char.get(qn("w:fldCharType"))
                    depth += 1 if kind == "begin" else -1 if kind == "end" else 0
                if depth == 0:
                    parts.append(new_token(field_group))
                    field_group = None
                continue
            if child.tag in _WHOLE_ELEMENTS or (child.tag == W_R and _run_is_special(child)):
                parts.append(new_token([child]))
            elif child.tag == W_R:
                parts.append(_run_text(child))
        if field_group:  # a field that never ended: keep what there is
            parts.append(new_token(field_group))
        texts.append(re.sub(r"[ \t]+", " ", "".join(parts)).strip())
    selection.masked_text = "\n\n".join(texts)
    return selection


def _visible_children(p_el):
    """Runs and whole elements of a paragraph in order; runs inside a tracked insertion count as normal runs."""
    for child in p_el.iterchildren():
        if child.tag in _SKIPPED:
            continue
        if child.tag in (_W_NS + "ins", _W_NS + "moveTo"):
            yield from child.iterchildren()
        else:
            yield child


def unmask_for_display(masked: str, tokens: dict[str, str]) -> str:
    """Plain text of a masked string, with every token replaced by the text it stands for."""
    return TOKEN_PATTERN.sub(lambda m: tokens.get(m.group(0), m.group(0)), masked)


def _plain_run_properties(template_p):
    """rPr of the paragraph's longest plain run, so new text looks like the old body text."""
    best = None
    best_len = -1
    for run in template_p.iter(W_R):
        if _run_is_special(run):
            continue
        length = len(_run_text(run))
        if length > best_len:
            best, best_len = run, length
    rpr = best.find(W_RPR) if best is not None else None
    return copy.deepcopy(rpr) if rpr is not None else None


def _text_run(text: str, rpr):
    run = OxmlElement("w:r")
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    return run


def build_paragraphs(template_p, masked_text: str, tokens: dict[str, list[str]], plain_tokens: dict[str, str]) -> list:
    """New Word paragraphs for rewritten text. Blank lines separate paragraphs.

    `tokens` are formatted elements (put back as the original XML), `plain_tokens` are the calibrator's own
    tokens (numbers, units, citations in plain text), put back as text.
    """
    ppr = template_p.find(W_PPR)
    rpr = _plain_run_properties(template_p)
    new_paragraphs = []
    for block in re.split(r"\n\s*\n", masked_text.strip()):
        block = re.sub(r"\s*\n\s*", " ", block).strip()
        if not block:
            continue
        p_el = OxmlElement("w:p")
        if ppr is not None:
            p_el.append(copy.deepcopy(ppr))
        for piece in re.split(r"(__REJ_INV_\d{4}__)", block):
            if not piece:
                continue
            if piece in tokens:
                for xml in tokens[piece]:
                    p_el.append(parse_xml(xml))
            else:
                text = TOKEN_PATTERN.sub(lambda m: plain_tokens.get(m.group(0), ""), piece)
                if text:
                    p_el.append(_text_run(text, rpr))
        new_paragraphs.append(p_el)
    return new_paragraphs


def replace_paragraphs(document, selected: list[int], new_paragraphs: list) -> int:
    """Put `new_paragraphs` where the first selected paragraph is and remove all selected ones.

    Paragraphs between the selected ones (captions, figures) stay. Returns how many paragraphs the body
    gained (negative when it shrank).
    """
    paragraphs = body_paragraphs(document)
    first = paragraphs[selected[0]]
    for p_el in new_paragraphs:
        first.addprevious(p_el)
    for index in selected:
        old = paragraphs[index]
        old.getparent().remove(old)
    return len(new_paragraphs) - len(selected)
