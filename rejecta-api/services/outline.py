"""The paper's verified outline, and the checks that keep the report's words inside it.

The outline is read from the file by code: for each section its printed number ("2", "B"), heading, first page,
and whether it is in the main text or an appendix (after the reference list, or headed "Appendix"); the figures
and tables with their captions; and what the file says about where it was presented or published ("The ICML
2023 Workshop on ..."). The model is given this outline, and everything it writes is checked against it:
- a section the paper does not have ("the Limitations section") takes its sentence out of the report;
- an appendix section named as if it were main text gets "(Appendix B)" after its name;
- a figure or table number the paper does not have takes its sentence out.
What the main text lacks (a Methods or Conclusion section that is only in an appendix, or nowhere) is stated by
code as an observation, so the model never has to guess, and never critiques a section that is not there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.analysis_guard import alnum
from services import paper_profile as pp

REFERENCE_TITLES = frozenset({
    "references", "bibliography", "literature cited", "notes and references", "references and notes",
    "cited references", "works cited",
})
_LABEL = re.compile(r"^\s*(?:(\d{1,2}(?:\.\d{1,2}){0,2})[\.\)]?\s+|([A-H]|[IVX]{1,4})[\.\)]\s+)")
_APPENDIX_WORD = re.compile(r"(?i)\bappendi|supplementa")
_CAPTION = re.compile(r"^\s*(Fig(?:ure)?s?\.?|Table|Scheme|Chart)\s*(S?\d{1,2})[A-Za-z]?\s*[\.:|]\s*(\S.{0,200})", re.IGNORECASE)
_VENUE = re.compile(
    r"(?i)(?:\b(?:19|20)\d{2}\b[^.\n]{0,60}\b(?:workshop|conference|symposium|meeting)\b"
    r"|\b(?:workshop|conference|symposium) on\b|\bproceedings of\b|\bpresented at\b|\baccepted (?:at|to|for)\b"
    r"|\bpublished (?:in|at|by)\b|\bunder review\b|\bpreprint\b|\barxiv:\s*\d{4}\.\d{4,5})"
)
_ARXIV_STAMP = re.compile(r"arXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?(?:\s*\[[^\]]{2,20}\])?(?:\s+\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})?")
MAX_VENUE_STATEMENTS = 3
MAX_FIGURES = 40


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _label_and_title(line: str) -> tuple[str, str]:
    text = _clean(line)
    match = _LABEL.match(text)
    if not match:
        return "", text
    return (match.group(1) or match.group(2) or ""), text[match.end():].strip()


def _title_variants(title: str) -> list[str]:
    return [alnum(part) for part in re.split(r"\s+/\s+", title) if alnum(part)]


def _find_heading(lines: list[str], wanted: list[str], start: int) -> int | None:
    for order in (range(start, len(lines)), range(0, start)):
        for index in order:
            _, rest = _label_and_title(lines[index])
            key = alnum(rest)
            if key and len(key) <= 140 and any(key == w or (len(w) >= 12 and key.startswith(w)) for w in wanted):
                return index
    return None


def _reference_line(lines: list[str]) -> int | None:
    found = None
    for index, line in enumerate(lines):
        _, rest = _label_and_title(line)
        if rest.lower().rstrip(".:") in REFERENCE_TITLES:
            found = index
    return found


def _pages(page_texts: list[str]) -> list[str]:
    return [alnum(text) for text in page_texts]


def _page_of(fragment: str, pages: list[str], first_page: int, from_page: int = 0) -> int | None:
    key = alnum(fragment)[:80]
    if len(key) < 6:
        return None
    for order in (range(from_page, len(pages)), range(0, from_page)):
        for p in order:
            if key in pages[p]:
                return p + first_page
    return None


def attach(
    parsed: Any, lines: list[str], page_texts: list[str] | None = None, first_page: int = 1, front_text: str = ""
) -> None:
    """Fill the parsed document's outline fields (labels, parts, pages, figures, venue statements) in place."""
    labels: dict[str, str] = {}
    parts: dict[str, str] = {}
    pages: dict[str, int] = {}
    page_alnum = _pages(page_texts or [])
    ref_line = _reference_line(lines)
    cursor = 0
    page_cursor = 0
    for key in parsed.sections:
        title = parsed.section_titles.get(key) or key
        wanted = _title_variants(title) or _title_variants(key)
        index = _find_heading(lines, wanted, cursor) if wanted else None
        if index is None:
            parts[key] = "appendix" if _APPENDIX_WORD.search(title) else "main"
            if key == "introduction" and page_alnum:
                pages[key] = first_page
            continue
        cursor = index + 1
        label, _ = _label_and_title(lines[index])
        labels[key] = label
        after_references = ref_line is not None and index > ref_line
        parts[key] = "appendix" if after_references or _APPENDIX_WORD.search(title) else "main"
        if page_alnum:
            page = _page_of(lines[index], page_alnum, first_page, page_cursor)
            if page is not None:
                pages[key] = page
                page_cursor = page - first_page
    parsed.section_labels = labels
    parsed.section_parts = parts
    parsed.section_pages = pages
    parsed.subsections = subsections(lines, labels, page_alnum, first_page)
    parsed.page_count = len(page_texts or [])
    parsed.first_page = first_page
    parsed.page_texts = list(page_texts or [])
    parsed.figures = figures(page_texts or [], first_page) if page_texts else figures([parsed.full_text], 0)
    parsed.venue_statements = venue_statements(front_text or (page_texts[0] if page_texts else parsed.full_text[:6000]))


_SUBHEADING = re.compile(r"^\s*((\d{1,2}|[A-H])\.(\d{1,2}))\.?\s+((?:[A-Z]|\d[A-Za-z])[^\n]{2,100})$")
MAX_SUBSECTIONS = 60


def subsections(lines: list[str], labels: dict[str, str], page_alnum: list[str], first_page: int) -> list[dict[str, Any]]:
    """Numbered subsections ("2.1 Observations", "C.3 2D and 3D Graph-based ..."), each under a section whose own
    number the outline has, and only in sequence (x.1, then x.2 ...), so "1.5 M NaOH was added" is never one."""
    parent_of = {label: key for key, label in labels.items() if label}
    expected: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    page_cursor = 0
    for line in lines:
        match = _SUBHEADING.match(_clean(line))
        if not match:
            continue
        label, parent_label, number, title = match.group(1), match.group(2), int(match.group(3)), match.group(4).strip()
        title = title.rstrip(".:").strip()
        if parent_label not in parent_of or number != expected.get(parent_label, 1):
            continue
        if not re.search(r"[a-z]{3,}", title) and not title.isupper():
            continue
        if sum(ch.isdigit() for ch in title) > 4 or title.endswith(","):
            continue
        expected[parent_label] = number + 1
        entry: dict[str, Any] = {"label": label, "title": title, "parent": parent_of[parent_label]}
        if page_alnum:
            page = _page_of(line, page_alnum, first_page, page_cursor)
            if page is not None:
                entry["page"] = page
                page_cursor = page - first_page
        out.append(entry)
        if len(out) >= MAX_SUBSECTIONS:
            break
    return out


def figures(page_texts: list[str], first_page: int) -> list[dict[str, Any]]:
    """Figures, tables and schemes with the start of their caption and their page (first caption wins)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p, text in enumerate(page_texts):
        for line in text.split("\n"):
            match = _CAPTION.match(line)
            if not match:
                continue
            kind = match.group(1).lower()
            kind = "Figure" if kind.startswith("fig") else kind.capitalize()
            label = f"{kind} {match.group(2).upper()}"
            if label in seen:
                continue
            seen.add(label)
            entry: dict[str, Any] = {"label": label, "caption": _clean(match.group(3))[:160]}
            if first_page:
                entry["page"] = p + first_page
            out.append(entry)
            if len(out) >= MAX_FIGURES:
                return out
    return out


def venue_statements(front: str) -> list[str]:
    """What the first page says about where the work was presented or published, word for word."""
    text = re.sub(r"\s+", " ", front or "")
    out: list[str] = []
    for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text):
        sentence = sentence.strip()
        arxiv = _ARXIV_STAMP.search(sentence)
        if arxiv:  # the arXiv stamp sits inside running text on the first page: keep only the stamp
            sentence = arxiv.group(0)
        else:
            found = _VENUE.search(sentence)
            if found and found.start() > 0:
                # page furniture before the statement ("Title Abstract The ICML 2023 Workshop ..."): start at the
                # statement, or at a "The" just before it
                head = sentence[: found.start()]
                the = head.rfind("The ", max(0, len(head) - 40))
                sentence = sentence[the if the >= 0 else found.start():]
        if 15 <= len(sentence) <= 260 and _VENUE.search(sentence) and "@" not in sentence:
            if sentence not in out:
                out.append(sentence)
        if len(out) >= MAX_VENUE_STATEMENTS:
            break
    return out


# --------------------------------------------------------------------------- the outline in use


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    label: str
    part: str  # main | appendix
    page: int | None
    role: str

    @property
    def display(self) -> str:
        """"Appendix B: Experimental Setups (p. 9)" or "Section 3: Why ... (p. 2)"."""
        where = f" (p. {self.page})" if self.page else ""
        if self.part == "appendix":
            prefix = f"Appendix {self.label}: " if self.label and self.label.isalpha() else "Appendix: "
        else:
            prefix = f"Section {self.label}: " if self.label else ""
        return f"{prefix}{self.title}{where}"


def entries(ctx: Any) -> list[Entry]:
    out: list[Entry] = []
    for key in ctx.section_texts:
        out.append(Entry(
            key=key, title=ctx.section_titles.get(key) or key.capitalize(), label=ctx.section_labels.get(key, ""),
            part=ctx.section_parts.get(key, "main"), page=ctx.section_pages.get(key), role=ctx.section_roles.get(key, "other"),
        ))
    return out


_MAIN_EXPECTED = (
    (pp.ROLE_METHODS, "Methods / Experimental"),
    (pp.ROLE_DISCUSSION, "Discussion / Conclusion"),
)


def structure_notes(ctx: Any, article_type: str | None = None) -> list[str]:
    """What the main text lacks, said by code: a standard part that is only in an appendix, or nowhere.
    Only for research articles and communications (a review or perspective is not expected to have them); a paper
    whose type is not printed counts as research when it has a Results section."""
    kind = article_type or ctx.article_type
    items = entries(ctx)
    research = kind in (pp.RESEARCH_ARTICLE, pp.COMMUNICATION) or (
        kind in (None, "", pp.UNKNOWN) and any(e.role == pp.ROLE_RESULTS for e in items)
    )
    if not research or ctx.structure_mode != "detected":
        return []
    notes: list[str] = []
    if any(e.part == "appendix" for e in items):
        appendix = ", ".join(e.display for e in items if e.part == "appendix")
        notes.append(f"Main text and appendix are told apart: {appendix} are appendix material.")
    for role, name in _MAIN_EXPECTED:
        in_main = [e for e in items if e.role == role and e.part == "main"]
        in_appendix = [e for e in items if e.role == role and e.part == "appendix"]
        if in_main:
            continue
        if role == pp.ROLE_METHODS and kind == pp.COMMUNICATION:
            continue  # a communication often has its methods only in the Supporting Information
        if in_appendix:
            notes.append(f"The main text has no {name} section; it is in {in_appendix[0].display}.")
        else:
            notes.append(f"The paper has no {name} section.")
    return notes


def outline_block(ctx: Any) -> str:
    """The outline as the model sees it, with the rules for naming sections."""
    items = entries(ctx)
    if not items:
        return ""
    lines = ["VERIFIED OUTLINE OF THE PAPER (read from the file by code; the only sections that exist):"]
    subs = getattr(ctx, "subsections", None) or []
    for e in items:
        lines.append(f'- "{e.key}": {e.display} — {"APPENDIX" if e.part == "appendix" else "main text"}, role {e.role}')
        for sub in (s for s in subs if s.get("parent") == e.key):
            page = f" (p. {sub['page']})" if sub.get("page") else ""
            lines.append(f"    - {sub['label']} {sub['title']}{page}")
    figs = getattr(ctx, "figures", None) or []
    if figs:
        lines.append("Figures and tables in the file: " + ", ".join(f["label"] for f in figs) + ".")
    lines.append(
        "Tables appear in the text as one line, \"[Table N (p. X) as a table. Columns: a | b | c. Rows: r1 | v | v; "
        "r2 | v | v]\": read each value by its row and column. Figures are not shown: only their captions are; "
        "do not fault what a figure may show, and do not describe a figure beyond its caption."
    )
    for note in structure_notes(ctx):
        lines.append(f"Observation (by code): {note}")
    lines.append(
        "Rules: name a section or subsection only by a heading or number in this outline, and add \"(Appendix X)\" when it is appendix "
        "material. Do not assume a section exists (Discussion, Conclusion, Limitations, Future Work, Related Work) "
        "unless it is in the outline; when the main text lacks a standard part, state that as an observation, do "
        "not critique a section that is not there. Name only figures and tables listed above."
    )
    venue = getattr(ctx, "venue_statements", None) or []
    if venue:
        lines.append("PUBLICATION CONTEXT the file states (word for word): " + " | ".join(f"“{v}”" for v in venue))
        lines.append(
            "Take this into account: the work may already have been presented or published in that form. Say so "
            "where it matters for the target journal (prior presentation, overlap), without inventing the venue's policy."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- checking what the model wrote

_SECTION_MENTION = re.compile(
    r"(?:[\"'‘“]([^\"'’”]{3,80})[\"'’”]\s+(?:section|appendix)\b"
    r"|\bthe\s+([A-Z][\w\-]*(?:\s+(?:and|&|of|[A-Z][\w\-]*)){0,5})\s+section\b"
    r"|\b(?:Section|Sec\.|Subsection)\s+(\d{1,2}(?:\.\d{1,2})?)\b"
    r"|\bAppendix\s+([A-H](?:\.\d{1,2})?)\b)"
)
_FIGURE_MENTION = re.compile(r"\b(Fig(?:ure)?s?\.?|Tables?|Schemes?)\s*(S?\d{1,2})\b", re.IGNORECASE)
_GENERIC_NAMES = frozenset(alnum(w) for w in (
    "this", "that", "each", "every", "the", "first", "second", "third", "same", "next", "previous", "following",
    "main", "whole", "entire", "current", "corresponding", "relevant", "respective", "last", "final",
))
# names that stand for a role; they are fine when the paper has a section of that role
_ROLE_NAMES = {
    pp.ROLE_INTRODUCTION: ("introduction", "background"),
    pp.ROLE_METHODS: ("methods", "method", "methodology", "materials and methods", "experimental", "experimental section",
                      "experimental setup", "experimental setups", "experiments"),
    pp.ROLE_RESULTS: ("results", "results and discussion", "findings", "experiments"),
    pp.ROLE_DISCUSSION: ("discussion", "conclusion", "conclusions", "discussion and conclusion", "discussion and conclusions",
                         "summary", "outlook", "summary and outlook"),
}


@dataclass
class Mention:
    text: str
    entry: Entry | None
    known: bool


def _match_name(name: str, items: list[Entry]) -> tuple[Entry | None, bool]:
    key = alnum(name)
    if not key or key in _GENERIC_NAMES:
        return None, True
    for e in items:
        titles = _title_variants(e.title) + [alnum(e.key)]
        if any(key == t or (len(key) >= 6 and (key in t or t in key)) for t in titles):
            return e, True
    for role, names in _ROLE_NAMES.items():
        if key in {alnum(n) for n in names}:
            match = next((e for e in items if e.role == role), None)
            return match, match is not None
    return None, False


def _numbered(label: str, items: list[Entry], subs: list[dict[str, Any]], appendix: bool) -> tuple[Entry | None, bool]:
    """The entry a number names ("2", "2.1", "C", "C.3"): it exists only if that exact number is in the outline."""
    if "." in label:
        sub = next((s for s in subs if s.get("label") == label), None)
        if sub is None:
            return None, False
        parent = next((e for e in items if e.key == sub.get("parent")), None)
        return parent, parent is not None and (parent.part == "appendix") == appendix or not appendix and parent is not None
    entry = next((e for e in items if e.label == label and (e.part == "appendix") == appendix), None)
    return entry, entry is not None


def mentions(text: str, items: list[Entry], subs: list[dict[str, Any]] | None = None) -> list[Mention]:
    """The sections a piece of the model's text names, and whether each exists in the outline (subsections too)."""
    subs = subs or []
    found: list[Mention] = []
    for m in _SECTION_MENTION.finditer(text or ""):
        quoted, capital, number, letter = m.groups()
        if number:
            entry, known = _numbered(number, items, subs, appendix=False)
            found.append(Mention(m.group(0), entry, known))
        elif letter:
            entry, known = _numbered(letter, items, subs, appendix=True)
            found.append(Mention(m.group(0), entry, known))
        else:
            name = quoted or capital or ""
            entry, known = _match_name(name, items)
            if not known:  # a subsection's own title ("the 'Observations' section")
                sub = next((s for s in subs if alnum(s.get("title", "")) == alnum(name)), None)
                if sub is not None:
                    entry, known = next((e for e in items if e.key == sub.get("parent")), None), True
            found.append(Mention(m.group(0), entry, known))
    return found


def unknown_figures(text: str, figure_labels: set[str]) -> list[str]:
    if not figure_labels:
        return []
    out = []
    for kind, number in _FIGURE_MENTION.findall(text or ""):
        k = kind.lower()
        label = f"{'Figure' if k.startswith('fig') else ('Table' if k.startswith('tab') else 'Scheme')} {number.upper()}"
        if label not in figure_labels and label not in out:
            out.append(label)
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")


def check_prose(text: str, ctx: Any) -> tuple[str, list[str]]:
    """The text with each sentence that names a section or figure the paper does not have removed, and the
    first mention of an appendix section marked as such. Returns (text, what was removed)."""
    if not text:
        return text, []
    items = entries(ctx)
    if not items:
        return text, []
    labels = {f["label"] for f in (getattr(ctx, "figures", None) or [])}
    subs = getattr(ctx, "subsections", None) or []
    kept: list[str] = []
    removed: list[str] = []
    marked: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        found = mentions(sentence, items, subs)
        bad = [m.text for m in found if not m.known]
        bad += unknown_figures(sentence, labels)
        if bad:
            removed.append(f"{bad[0]} — {sentence[:160]}")
            continue
        for m in found:
            e = m.entry
            if e is None or e.part != "appendix" or e.key in marked or "appendix" in sentence.lower():
                continue
            tag = f" (Appendix {e.label})" if e.label and e.label.isalpha() else " (appendix)"
            sentence = sentence.replace(m.text, m.text + tag, 1)
            marked.add(e.key)
        kept.append(sentence)
    return " ".join(kept).strip(), removed
