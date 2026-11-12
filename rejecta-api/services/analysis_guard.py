"""Checks on what the model returns, done in code so the report cannot contain what the paper does not.

The model proposes; this module verifies and computes:
  * a derived section must start with words that really are in the paper (its "anchor");
  * a detected section must be one the parser found under a real heading;
  * a quotation in the report must be in the paper, otherwise it is removed;
  * numbers and years the paper does not contain are flagged;
  * the overall score is the weighted mean of fixed criteria, plus a citation criterion computed
    from the citation check, never a single number invented by the model;
  * what the uploaded file cannot show (Supporting Information) is reported as "not assessed".
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from models import ConsistencyReport, Disclosures, PublicationStatus, SectionCheck
from services import paper_profile as pp
from services import section_checks
from services.textfold import GREEK_TABLE, fold

logger = logging.getLogger(__name__)

MAX_SECTIONS = 8
MIN_ANCHOR_CHARS = 20  # letters and digits, spaces and punctuation ignored (about four words)
ANCHOR_PREFIX_CHARS = 24
MAX_GROUNDING_NOTES = 8
MISSING_METHODS_PENALTY = 8  # research article with no Experimental/Methods section in the file
SI_PENALTY = 5  # most a missing Supporting Information can cost; it is the upload's limit, not a flaw
GHOST_PENALTY = 15  # citation integrity lost per reference that cannot exist
MAX_GHOST_PENALTY = 60
UNVERIFIED_QUOTE = "[unverified quote removed]"

# Mood bands, the same ones the prompt gives the model.
_MOOD_BANDS = ((20, "furious"), (40, "skeptical"), (55, "unimpressed"), (70, "neutral"), (80, "interested"), (100, "delighted"))


def mood_for_score(score: int) -> str:
    for upper, mood in _MOOD_BANDS:
        if score <= upper:
            return mood
    return "delighted"


def alnum(text: str) -> str:
    """Lowercase letters and digits only. Makes quotes comparable across ligatures, hyphen breaks and spacing,
    and across the spellings of a Greek letter ("ϕ", "φ", "\\phi", "phi" are all "phi")."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", text).lower().translate(GREEK_TABLE))


def _index_text(text: str) -> tuple[str, str, list[int]]:
    """(normalised text, its letters and digits, and for each of those letters its position in the normalised text)."""
    norm = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    index: list[int] = []
    for position, char in enumerate(norm):
        for lower in char.lower():
            for letter in fold(lower):  # a Greek letter gives its name, every letter at the Greek letter's place
                if "a" <= letter <= "z" or "0" <= letter <= "9":
                    chars.append(letter)
                    index.append(position)
    return norm, "".join(chars), index


@dataclass
class AnalysisContext:
    """What the pipeline knows about the paper, shared with the prompt builder and the checks."""

    source_text: str = ""  # the whole paper, for verifying quotes and anchors
    article_type: str = pp.UNKNOWN
    type_source: str = "none"  # label | user | none
    structure_mode: str = "detected"  # detected | derived
    section_texts: dict[str, str] = field(default_factory=dict)  # headed sections (detected mode)
    section_roles: dict[str, str] = field(default_factory=dict)
    section_titles: dict[str, str] = field(default_factory=dict)  # key -> heading as printed
    abstract: str = ""
    body: str = ""  # the whole body, sent to the model in derived mode
    refers_to_si: bool = False
    citations_total: int = 0
    citations_verified: int = 0
    citations_ghost: int = 0
    citations_unchecked: int = 0
    extra_text: str = ""  # citation summary and journal data: numbers there are legitimate
    numbers_text: str = ""  # text whose numbers count as "in the paper" (the body, not the reference list)
    parse_warnings: list[str] = field(default_factory=list)  # what the parser could not read
    # A revision: the issues raised on the earlier version, as [{"id", "severity", "problem"}].
    previous_issues: list[dict[str, Any]] = field(default_factory=list)
    # A revision of a paper without headings: the sections the earlier version was divided into, in order.
    previous_sections: list[str] = field(default_factory=list)
    # The publication record (services.integrity): published, retracted, under concern... None: not checked.
    publication: PublicationStatus | None = None
    # The paper's own values compared by code (services.consistency), and its disclosure statements.
    consistency: ConsistencyReport | None = None
    disclosures: Disclosures | None = None
    corpus: str = field(init=False, default="")
    norm_text: str = field(init=False, default="")  # the paper, Unicode-normalised
    corpus_index: list[int] = field(init=False, default_factory=list)  # corpus position -> position in norm_text
    body_norm: str = field(init=False, default="")  # the readable body (paragraphs), Unicode-normalised
    body_corpus: str = field(init=False, default="")  # letters and digits of the body
    body_index: list[int] = field(init=False, default_factory=list)  # body_corpus position -> position in body_norm
    numbers: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.norm_text, self.corpus, self.corpus_index = _index_text(self.source_text)
        self.body_norm, self.body_corpus, self.body_index = _index_text(self.body)
        self.numbers = set(re.findall(r"\d+(?:\.\d+)?", f"{self.numbers_text or self.source_text} {self.extra_text}"))


_CURRENT: ContextVar[AnalysisContext | None] = ContextVar("rejecta_analysis_context", default=None)


def current_context() -> AnalysisContext | None:
    return _CURRENT.get()


@contextmanager
def paper_context(ctx: AnalysisContext | None) -> Iterator[None]:
    """Make `ctx` visible to the prompt builder and the output checks for the duration of one analysis."""
    token = _CURRENT.set(ctx)
    try:
        yield
    finally:
        _CURRENT.reset(token)


# --------------------------------------------------------------------------- text checks

_QUOTED = re.compile(r"[\"“]([^\"“”]{20,500})[\"”]")
_ELLIPSIS = re.compile(r"\.{3}|…")


def _scrub_quotes(text: str, ctx: AnalysisContext, counter: list[int]) -> str:
    """Replace a quotation of five or more words that is not in the paper."""

    def repl(match: re.Match[str]) -> str:
        quote = match.group(1)
        if len(quote.split()) < 5:
            return match.group(0)  # a short term in quotes is emphasis, not a quotation
        pieces = [p for p in _ELLIPSIS.split(quote) if len(alnum(p)) >= 12]
        if not pieces or all(alnum(p) in ctx.corpus for p in pieces):
            return match.group(0)
        counter[0] += 1
        return UNVERIFIED_QUOTE

    return _QUOTED.sub(repl, text)


_UNIT_NUMBER = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s?(?:%|°C|nm|µm|μm|mg|mL|mmol|mM|eV|kDa|kg|fold|samples|patients|compounds)\b"
    r"|(?<![\w.])(\d+(?:\.\d+)?)\s?(?:%|°C)"
)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")


def _ungrounded_numbers(text: str, ctx: AnalysisContext) -> list[str]:
    found: list[str] = []
    for match in _UNIT_NUMBER.finditer(text):
        value = match.group(1) or match.group(2)
        if value not in ctx.numbers and match.group(0).strip() not in found:
            found.append(match.group(0).strip())
    for match in _YEAR.finditer(text):
        if match.group(1) not in ctx.numbers and match.group(1) not in found:
            found.append(match.group(1))
    return found


# --------------------------------------------------------------------------- sections

def _to_score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _section_entries(raw: Any) -> list[dict[str, Any]]:
    """Sections as a list of dicts with a name, whether the model sent a list or an object."""
    entries: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("section")
                if name:
                    entries.append({**item, "name": str(name)})
    elif isinstance(raw, dict):
        for name, item in raw.items():
            if isinstance(item, dict):
                entries.append({**item, "name": str(name)})
    return entries


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _locate(text: str, ctx: AnalysisContext) -> int:
    """Where `text` starts in the paper (position in the letters-and-digits form), or -1 if it is not there."""
    wanted = alnum(text)
    position = ctx.corpus.find(wanted) if len(wanted) >= MIN_ANCHOR_CHARS else -1
    if position < 0 and len(wanted) >= ANCHOR_PREFIX_CHARS:
        position = ctx.corpus.find(wanted[:ANCHOR_PREFIX_CHARS])
    return position


IMPACT_AREAS = ("novelty", "scientific_validity", "publication_potential")
MAX_REASONS = 6
MIN_REASON_PART_CHARS = 20
_FIGURE_REF = re.compile(r"\b(?:Fig(?:ure)?s?\.?|Scheme|Table)\s*S?\d+[A-Za-z]?", re.IGNORECASE)
_FIGURE_WINDOW = 300  # characters around the evidence in which a figure, scheme or table counts as "next to" it
# Words that appear in advice for any paper. A solution made of these alone is generic.
_GENERIC_WORDS = frozenset(
    "paper papers manuscript authors author results result discussion section sections should would could which "
    "their there these those about further additional include provide ensure improve clearly explicit explicitly "
    "address consider clarify strengthen enhance better between implications potential connect broader detail "
    "details summarize article study reviewers reviewer journal specific describe discuss present regarding "
    "approach readers reader overall general importance significance impact".split()
)


def _impact_area(value: Any) -> str:
    key = re.sub(r"[\s\-/]+", "_", str(value or "").strip().lower())
    key = {"validity": "scientific_validity", "scientific": "scientific_validity", "publication": "publication_potential"}.get(key, key)
    return key if key in IMPACT_AREAS else ""


def _stems(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z][a-z\-]{4,}", text.lower()) if w not in _GENERIC_WORDS}


def _where(position: int, evidence: str, starts: list[tuple[int, str]], ctx: AnalysisContext) -> str:
    """Section and nearby figure of an evidence passage, found by code from where it sits in the paper."""
    section = ""
    for start, name in starts:
        if start <= position:
            section = name
    wanted = len(alnum(evidence))
    first = ctx.corpus_index[position]
    last = ctx.corpus_index[min(len(ctx.corpus_index) - 1, position + wanted - 1)]
    lo = max(0, first - _FIGURE_WINDOW)
    window = ctx.norm_text[lo : last + _FIGURE_WINDOW]
    middle = (first + last) / 2 - lo
    nearest = min(
        _FIGURE_REF.finditer(window),
        key=lambda m: abs((m.start() + m.end()) / 2 - middle),
        default=None,
    )
    figure = re.sub(r"\s+", " ", nearest.group(0)).strip() if nearest else ""
    return ", ".join(part for part in (f"section: {section}" if section else "", f"next to {figure}" if figure else "") if part)


MAX_UNVERIFIED_CONCERNS = 5


def _clean_reasons(
    reasons: Any,
    ctx: AnalysisContext,
    notes: list[str],
    starts: list[tuple[int, str]],
    unverified: list[dict[str, Any]] | None = None,
    criteria_keys: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Keep only rejection reasons with all four parts: problem, evidence, impact, solution.

    The evidence must be words that are really in the manuscript, and its location (section, nearby figure)
    is worked out by code, not taken from the model. A solution that shares nothing specific with the problem
    and the evidence is advice that would fit any paper, so that reason is removed too.

    A removed reason that still has a problem is not thrown away: it goes to `unverified`, which the report shows
    apart as "not tied to your text" and which never counts as a rejection reason.
    """

    def criterion_of(reason: dict[str, Any]) -> str:
        key = re.sub(r"\s+", "_", str(reason.get("criterion") or "").strip().lower())
        return key if key in criteria_keys else ""

    def set_aside(problem: str, impact: str, solution: str, evidence: str, severity: Any, why: str, item: dict[str, Any]) -> None:
        if unverified is not None and len(problem) >= MIN_REASON_PART_CHARS and len(unverified) < MAX_UNVERIFIED_CONCERNS:
            unverified.append(
                {
                    "reason": problem, "problem": problem, "severity": "minor" if severity == "minor" else "major",
                    "evidence": evidence[:300], "location": "", "impact_area": "", "impact": impact,
                    "fix": solution, "solution": solution, "origin": "model", "criterion": criterion_of(item),
                    "unverified_reason": why,
                }
            )

    kept: list[dict[str, Any]] = []
    no_evidence = incomplete = generic = 0

    def text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    for reason in reasons if isinstance(reasons, list) else []:
        if not isinstance(reason, dict):
            incomplete += 1
            continue
        problem = text(reason.get("problem") or reason.get("reason"))
        impact = text(reason.get("impact"))
        solution = text(reason.get("solution") or reason.get("fix"))
        evidence = text(reason.get("evidence")).strip("\"“”")
        position = _locate(evidence, ctx)
        if position < 0:
            no_evidence += 1
            set_aside(problem, impact, solution, "", reason.get("severity"),
                      "The quoted evidence is not in the manuscript, so this concern could not be tied to the text.", reason)
            continue
        if min(len(problem), len(impact), len(solution)) < MIN_REASON_PART_CHARS:
            incomplete += 1
            continue
        if not (_stems(solution) & _stems(f"{problem} {evidence}")):
            generic += 1
            set_aside(problem, impact, solution, evidence, reason.get("severity"),
                      "The suggested fix does not address the quoted passage.", reason)
            continue
        kept.append(
            {
                "reason": problem,
                "problem": problem,
                "severity": reason.get("severity"),
                "evidence": evidence[:300],
                "location": _where(position, evidence, starts, ctx),
                "impact_area": _impact_area(reason.get("impact_area")),
                "impact": impact,
                "fix": solution,
                "solution": solution,
                "origin": "model",
                "criterion": criterion_of(reason),
            }
        )
    if no_evidence:
        notes.append(
            f"{no_evidence} rejection reason(s) did not point to any text in the manuscript; they are listed apart as "
            "unverified concerns and do not count as rejection reasons."
        )
    if incomplete:
        notes.append(f"{incomplete} rejection reason(s) were removed because they lacked a problem, an impact or a solution.")
    if generic:
        notes.append(
            f"{generic} rejection reason(s) had a solution that was not specific to the cited passage; they are listed "
            "apart as unverified concerns."
        )
    return kept[:MAX_REASONS]


def _section_starts(ctx: AnalysisContext, sections: dict[str, dict[str, Any]]) -> list[tuple[int, str]]:
    """Where each section begins in the paper, in reading order."""
    if ctx.structure_mode == "derived":
        found = [(_locate(str(f.get("anchor", "")), ctx), name) for name, f in sections.items()]
    else:
        found = [(_locate(text[:80], ctx), name) for name, text in ctx.section_texts.items()]
    return sorted((pos, name) for pos, name in found if pos >= 0)


MAX_CONTENT_CHARS = 12000  # most text of one section sent back to the app
TOOL_TEXT_CHARS = 6000  # most text one Calibrate run takes (about 900 words), so the rewrite fits the model's output
MAX_CAPTION_CHARS = 600  # a paragraph that starts like a caption and is longer than this is body text that got merged with it
_CAPTION = re.compile(r"^(?:Fig(?:ure)?s?\.?|Scheme|Table|Chart|Graph)\s*S?\d+[A-Za-z]?\b", re.IGNORECASE)


def _tool_text(content: str) -> tuple[str | None, list[str]]:
    """The section text prepared for Calibrate and Heatmap: captions left out, cut at a paragraph boundary.

    Returns (text or None when almost nothing is left, notes for the user).
    """
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]
    kept = [p for p in paragraphs if not (_CAPTION.match(p) and len(p) <= MAX_CAPTION_CHARS)]
    notes: list[str] = []
    if len(paragraphs) != len(kept):
        notes.append(f"{len(paragraphs) - len(kept)} figure, scheme or table caption(s) left out.")
    chosen: list[str] = []
    used = 0
    cut = False
    for paragraph in kept:
        extra = len(paragraph) + (2 if chosen else 0)
        if used + extra > TOOL_TEXT_CHARS:
            cut = True
            if not chosen:  # the first paragraph alone is too long: cut it at a word boundary
                chosen.append(paragraph[:TOOL_TEXT_CHARS].rsplit(" ", 1)[0] + " …")
            break
        chosen.append(paragraph)
        used += extra
    text = "\n\n".join(chosen)
    if cut:
        notes.append(f"Long section: only the first {len(text.split())} words are sent. Run again for the rest.")
    return (text if len(text) >= 20 else None), notes


def _evidence_window(content: str, evidence: str) -> tuple[str | None, list[str]]:
    """The paragraph holding `evidence` and its neighbours, for Calibrate: captions left out, at most
    TOOL_TEXT_CHARS, the evidence paragraph first, then the ones after it, then the ones before it."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", content) if p.strip()]
    prose = [p for p in paragraphs if not (_CAPTION.match(p) and len(p) <= MAX_CAPTION_CHARS)]
    wanted = alnum(evidence)[:ANCHOR_PREFIX_CHARS * 2]
    hit = next((i for i, p in enumerate(prose) if wanted and wanted in alnum(p)), None)
    if hit is None:
        return None, []
    chosen = {hit}
    used = len(prose[hit])
    after, before = hit + 1, hit - 1
    while after < len(prose) or before >= 0:
        for index in (after, before):
            if 0 <= index < len(prose) and index not in chosen and used + len(prose[index]) + 2 <= TOOL_TEXT_CHARS:
                chosen.add(index)
                used += len(prose[index]) + 2
        after, before = after + 1, before - 1
        if used >= TOOL_TEXT_CHARS:
            break
    text = "\n\n".join(prose[i] for i in sorted(chosen))
    if len(text) > TOOL_TEXT_CHARS:  # the evidence paragraph alone is too long
        text = text[:TOOL_TEXT_CHARS].rsplit(" ", 1)[0] + " …"
    notes = []
    if len(chosen) < len(prose):
        notes.append(f"Only the {len(chosen)} paragraph(s) around the passage this reason is about are sent.")
    return (text if len(text) >= 20 else None), notes


def _attach_reason_text(ctx: AnalysisContext, sections: dict[str, dict[str, Any]], reasons: list[dict[str, Any]]) -> None:
    """For each rejection reason: the section its evidence sits in and the paragraphs around the evidence."""
    total = len(ctx.body_corpus)
    spans = _section_spans(ctx, sections) if total else {}
    for reason in reasons:
        evidence = reason.get("evidence", "")
        position = _find_in_body(evidence, ctx) if total else -1
        name = next((n for n, (start, end) in spans.items() if start <= position < end), "") if position >= 0 else ""
        content = ""
        if name:
            if ctx.structure_mode == "derived":
                start, end = spans[name]
                stop = ctx.body_index[end] if end < total else len(ctx.body_norm)
                content = ctx.body_norm[ctx.body_index[start] : stop]
            else:
                content = ctx.section_texts.get(name, "")
        elif position >= 0:
            content = ctx.body_norm
        elif alnum(evidence) and alnum(evidence)[:ANCHOR_PREFIX_CHARS] in alnum(ctx.abstract):
            content = ctx.abstract
        tool_text, notes = _evidence_window(content, evidence) if content else (None, [])
        reason["section"] = name
        reason["tool_text"] = tool_text
        reason["tool_notes"] = notes


def _clean_field(value: Any) -> str | None:
    """A short plain name for the paper's field, or None. It ends up in the tools' prompts, so it is kept simple."""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .")
    return text if 2 <= len(text) <= 60 and re.fullmatch(r"[A-Za-z][A-Za-z0-9 &,/()'\-]*", text) else None


def _find_in_body(text: str, ctx: AnalysisContext, start: int = 0) -> int:
    """Where `text` starts in the body's letters-and-digits text, searching from `start`; -1 if it is not there."""
    wanted = alnum(text)
    if len(wanted) < MIN_ANCHOR_CHARS:
        return -1
    position = ctx.body_corpus.find(wanted, start)
    if position < 0 and len(wanted) >= ANCHOR_PREFIX_CHARS:
        position = ctx.body_corpus.find(wanted[:ANCHOR_PREFIX_CHARS], start)
    return position


def _section_spans(ctx: AnalysisContext, sections: dict[str, dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Where each section starts and ends in the body (positions in the body's letters and digits).

    A derived section starts at its verified anchor and runs until the next section starts. The first
    search begins after the abstract, so a sentence that is both in the abstract and in the text is
    taken from the text. A detected section is the text the parser found under its heading.
    """
    spans: dict[str, tuple[int, int]] = {}
    total = len(ctx.body_corpus)
    if ctx.structure_mode == "derived":
        floor = 0
        tail = alnum(ctx.abstract)[-40:]
        if len(tail) >= MIN_ANCHOR_CHARS:
            found = ctx.body_corpus.find(tail)
            floor = found + len(tail) if found >= 0 else 0
        starts: list[tuple[int, str]] = []
        for name, fields in sections.items():
            anchor = str(fields.get("anchor", ""))
            position = _find_in_body(anchor, ctx, floor)
            if position < 0:
                position = _find_in_body(anchor, ctx, 0)
            if position >= 0:
                starts.append((position, name))
        starts.sort()
        unique: list[tuple[int, str]] = []
        for position, name in starts:
            if not unique or unique[-1][0] != position:  # two sections cannot start at the same place
                unique.append((position, name))
        for i, (position, name) in enumerate(unique):
            spans[name] = (position, unique[i + 1][0] if i + 1 < len(unique) else total)
    else:
        for name in sections:
            text = ctx.section_texts.get(name, "")
            position = _find_in_body(text[:100], ctx)
            if position >= 0:
                spans[name] = (position, min(total, position + len(alnum(text))))
    return spans


def section_full_texts(ctx: AnalysisContext, sections: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each section's whole text, in the order of `sections`: under its heading (detected), or from its anchor to
    the next section's (derived). A section whose text cannot be found is left out."""
    if ctx.structure_mode != "derived":
        return {name: ctx.section_texts[name].strip() for name in sections if ctx.section_texts.get(name, "").strip()}
    total = len(ctx.body_corpus)
    spans = _section_spans(ctx, sections) if total else {}
    out: dict[str, str] = {}
    for name in sections:
        if name in spans:
            start, end = spans[name]
            stop = ctx.body_index[end] if end < total else len(ctx.body_norm)
            content = ctx.body_norm[ctx.body_index[start] : stop].strip()
            if content:
                out[name] = content
    return out


def _attach_content(ctx: AnalysisContext, sections: dict[str, dict[str, Any]]) -> None:
    """Add the text each section covers, its size and where it sits in the paper. Nothing is added if it cannot be found."""
    total = len(ctx.body_corpus)
    if not total:
        return
    for name, (start, end) in _section_spans(ctx, sections).items():
        if ctx.structure_mode == "derived":
            stop = ctx.body_index[end] if end < total else len(ctx.body_norm)
            content = ctx.body_norm[ctx.body_index[start] : stop].strip()
        else:
            content = ctx.section_texts[name].strip()
        if not content:
            continue
        words = len(content.split())
        tool_text, tool_notes = _tool_text(content)
        truncated = len(content) > MAX_CONTENT_CHARS
        if truncated:
            content = content[:MAX_CONTENT_CHARS].rsplit(" ", 1)[0] + " …"
        sections[name].update(
            content=content,
            content_truncated=truncated,
            word_count=words,
            start_pct=round(100 * start / total),
            end_pct=min(100, round(100 * end / total)),
            tool_text=tool_text,
            tool_notes=tool_notes,
        )


def compute_checks(
    ctx: AnalysisContext, texts: dict[str, str], roles: dict[str, str], titles: dict[str, str], article_type: str
) -> dict[str, list[SectionCheck]]:
    """Each section's checklist, decided by code (services.section_checks). Raises nothing: {} on failure."""
    if not texts:
        return {}
    paper = section_checks.PaperText(
        full_text=ctx.source_text, abstract=ctx.abstract, sections=texts,
        roles={name: roles.get(name) or "other" for name in texts}, article_type=article_type,
        titles={name: titles.get(name, "") for name in texts},
    )
    try:
        return section_checks.check_sections(paper)
    except Exception:  # noqa: BLE001 - the checklist is extra; the analysis stands without it
        logger.warning("Section checks failed; continuing without them", exc_info=True)
        return {}


def _attach_checks(
    ctx: AnalysisContext, sections: dict[str, dict[str, Any]], texts: dict[str, str], article_type: str, notes: list[str]
) -> None:
    ordered = {name: texts[name] for name in sections if texts.get(name)}
    if not ordered:
        return
    found = compute_checks(
        ctx, ordered, {n: str(sections[n].get("role") or "other") for n in ordered},
        {n: str(sections[n].get("title") or "") for n in ordered}, article_type,
    )
    if not found:
        notes.append("The section checklists could not be computed for this file.")
    for name, items in found.items():
        sections[name]["checks"] = [item.model_dump() for item in items]


MAX_SECTION_NAME_CHARS = 120  # a paper's own numbered headings can be long


def _clean_sections(
    raw: Any, ctx: AnalysisContext | None, notes: list[str], article_type: str | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Valid sections keyed by name, and the roles they have.

    Names never come from the model's imagination: a detected section keeps its heading from the file, and
    a section of a paper without headings is filed under a fixed name (Introduction, Experimental, Results
    and discussion, Conclusion...), so the same paper always gets the same sections.
    """
    kept: list[tuple[int, str, dict[str, Any], str]] = []  # (position in paper, name, fields, role)
    detected_keys = {_key(k): k for k in (ctx.section_texts if ctx else {})}
    # detected sections are shown in the order of the paper, never in the order the model listed them
    detected_order = {k: i for i, k in enumerate(ctx.section_texts if ctx else {})}

    for entry in _section_entries(raw):
        name = re.sub(r"\s+", " ", entry["name"]).strip(" .:-")
        score = _to_score(entry.get("score"))
        if not (2 <= len(name) <= MAX_SECTION_NAME_CHARS) or score is None:
            continue
        role = str(entry.get("role", "other")).lower()
        role = role if role in pp.ALL_ROLES else "other"
        fields = {k: entry[k] for k in ("issue", "fix", "strength", "weakness") if isinstance(entry.get(k), str)}
        fields["score"] = score
        position = 0

        if ctx is not None and ctx.structure_mode == "derived":
            anchor = str(entry.get("anchor", "")).strip()
            position = _locate(anchor, ctx)
            if position < 0:
                notes.append(f'Section "{name}" was removed: its starting words were not found in the paper.')
                continue
            fields["source"] = "derived"
            fields["anchor"] = anchor[:200]
            key_name, role = pp.canonical_section_name(name, role, article_type)
        elif ctx is not None:
            match = detected_keys.get(_key(name))
            if match is None:
                notes.append(f'Section "{name}" was removed: it is not a section of this paper.')
                continue
            fields["source"] = "detected"
            key_name = match
            role = ctx.section_roles.get(match, role)
            position = detected_order[match]
        else:
            key_name = name
        fields["role"] = role
        printed = ctx.section_titles.get(key_name, "") if ctx is not None else ""
        if printed and printed.lower() != key_name.lower():
            fields["title"] = printed
        kept.append((position, key_name, fields, role))

    if ctx is not None:
        kept.sort(key=lambda item: item[0])  # reading order (stable): a name used twice keeps its first place
    unique: list[tuple[int, str, dict[str, Any], str]] = []
    seen: set[str] = set()
    for item in kept:
        if _key(item[1]) not in seen:
            seen.add(_key(item[1]))
            unique.append(item)
    unique = unique[:MAX_SECTIONS]
    return {name: fields for _, name, fields, _ in unique}, [role for *_, role in unique]


# --------------------------------------------------------------------------- criteria and score

def _criteria_from_model(raw: Any, profile: pp.Profile) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return found
    for key, weight in profile.criteria:
        item = raw.get(key)
        reason = ""
        if isinstance(item, dict):
            score = _to_score(item.get("score"))
            reason = str(item.get("reason", "")).strip()
        else:
            score = _to_score(item)
        if score is not None:
            found[key] = {"score": score, "reason": reason[:400], "weight": float(weight), "computed": False}
    return found


def _citation_criterion(ctx: AnalysisContext) -> dict[str, Any] | None:
    """Reference integrity from the citation check. Skipped when nothing could be checked.

    Only ghost references (a DOI or arXiv id that does not exist) cost points. A reference the databases
    have no record of is not evidence of anything: books, reports, theses and older or regional work are
    often missing from them, so counting it would mark down papers for citing them.

    The score says how valid the references are; the coverage says how many of them that is known for. A
    reference with no record found is not counted against the paper, but it is not counted as valid either: the
    criterion weighs only as much as the share of references whose status is settled (verified or ghost), so a
    paper whose references could mostly not be checked does not get a full-weight 100.
    """
    total = ctx.citations_total
    settled = ctx.citations_verified + ctx.citations_ghost
    if total <= 0 or settled <= 0 or total - ctx.citations_unchecked <= 0:
        return None
    coverage = min(1.0, settled / total)
    score = 100 - min(MAX_GHOST_PENALTY, GHOST_PENALTY * ctx.citations_ghost)
    not_found = max(0, total - settled - ctx.citations_unchecked)
    parts = [f"{ctx.citations_verified} of {total} references verified"]
    parts.append(f"{ctx.citations_ghost} could not exist (ghost)" if ctx.citations_ghost else "none that could not exist")
    if not_found:
        parts.append(f"{not_found} with no matching record (not counted either way)")
    if ctx.citations_unchecked:
        parts.append(f"{ctx.citations_unchecked} not checked")
    reason = "; ".join(parts) + f". Coverage {round(coverage * 100)}%: this criterion counts for that share of its weight."
    return {
        "score": max(0, min(100, score)),
        "reason": reason,
        "weight": pp.CITATION_WEIGHT * coverage,
        "computed": True,
        "coverage": round(coverage, 3),
    }


def _text_fields(payload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    """Every free-text field of the report as (container, key), so it can be checked in place."""
    for key in ("verdict", "oracle_letter", "novelty_assessment", "journal_fit"):
        if isinstance(payload.get(key), str):
            yield payload, key
    for container in (
        list(payload.get("sections", {}).values())
        + list(payload.get("rejection_reasons", []))
        + list(payload.get("unverified_concerns", []))
    ):
        if isinstance(container, dict):
            for key in ("issue", "fix", "strength", "weakness", "reason"):
                if isinstance(container.get(key), str):
                    yield container, key
    for container in payload.get("criteria", {}).values():
        if isinstance(container, dict) and isinstance(container.get("reason"), str):
            yield container, "reason"


ISSUE_STATUSES = ("resolved", "partly_resolved", "open")
MAX_ISSUE_NOTE_CHARS = 400


def _previous_issue_statuses(raw: Any, ctx: AnalysisContext) -> list[dict[str, Any]]:
    """The model's verdict on each earlier issue: only issues that exist, only the three statuses, once each."""
    known = {item["id"] for item in ctx.previous_issues}
    out: dict[int, dict[str, Any]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        status = str(item.get("status", "")).strip().lower().replace(" ", "_")
        if number in known and status in ISSUE_STATUSES and number not in out:
            note = re.sub(r"\s+", " ", str(item.get("note") or "")).strip()[:MAX_ISSUE_NOTE_CHARS]
            out[number] = {"id": number, "status": status, "note": note}
    return [out[n] for n in sorted(out)]


MAX_CONSISTENCY_REASONS = 5
CONSISTENCY_RIGOR_PENALTY = 5  # points off rigor per major inconsistency
MAX_CONSISTENCY_RIGOR_PENALTY = 15

_CONSISTENCY_TEXT: dict[str, tuple[str, str]] = {
    "value_mismatch": (
        "Readers cannot tell which value was used, so the experiment cannot be reproduced and the result may not mean what it says.",
        "State the value that was actually used and correct the other passage.",
    ),
    "category_mismatch": (
        "The summary and the data say different things, so the main finding is misreported in one of them.",
        "Make the Abstract and the Results report what the data show, in the same terms.",
    ),
    "percent_claim": (
        "The rationale given for the doses does not hold as written, which puts the dosing and its safety reading in doubt.",
        "Recompute the stated percentage, or correct the doses or the reference value they are compared with.",
    ),
    "unit_mismatch": (
        "A value in the wrong unit cannot be reproduced and may hide a different quantity.",
        "Give the value in the unit the quantity is measured in, and check the number with it.",
    ),
    "implausible_value": (
        "A value this far from the usual range changes the results that depend on it, or is a typing error.",
        "Check the value against the lab records and correct it, or explain why it is right.",
    ),
    "foreign_compound": (
        "Part of the Methods may describe a different study, so it is unclear which experiment was done.",
        "Rewrite the passage to describe the experiment as it was done in this study, or explain the role of this compound.",
    ),
    "problematic_cell_line": (
        "Findings in a contaminated or misidentified line may not hold for the cancer type the paper names.",
        "Authenticate the line (STR profile) and report the result, or repeat the key experiments in an authenticated line.",
    ),
}


def _alnum(text: str) -> str:
    return alnum(text or "")


def _repeats_consistency(reasons: list[dict[str, Any]], ctx: AnalysisContext) -> int:
    """Mark model concerns whose quoted passage is one a consistency finding already quotes (either contains the other)."""
    report = ctx.consistency
    if report is None:
        return 0
    quoted = [_alnum(q.quote) for f in report.findings[:MAX_CONSISTENCY_REASONS] for q in f.quotes if len(_alnum(q.quote)) >= 20]
    count = 0
    for reason in reasons:
        evidence = _alnum(reason.get("evidence", ""))
        if len(evidence) >= 20 and any(evidence in q or q in evidence for q in quoted):
            reason["_repeats_consistency"] = True
            count += 1
    return count


def _consistency_reasons(ctx: AnalysisContext) -> list[dict[str, Any]]:
    """Reasons written by code from the consistency findings: each quotes the passages it rests on."""
    report = ctx.consistency
    if report is None or not report.findings:
        return []
    out: list[dict[str, Any]] = []
    for finding in report.findings[:MAX_CONSISTENCY_REASONS]:
        impact, solution = _CONSISTENCY_TEXT.get(finding.kind, ("", "Check and correct the passages quoted."))
        first = finding.quotes[0] if finding.quotes else None
        out.append({
            "reason": finding.message, "problem": finding.message, "severity": finding.severity,
            "evidence": first.quote[:300] if first else "",
            "location": " / ".join(dict.fromkeys(q.section for q in finding.quotes if q.section)) or "",
            "impact_area": "scientific_validity", "impact": impact, "fix": solution, "solution": solution,
            "origin": "consistency", "criterion": "rigor",
        })
    return out


# ---------------------------------------------------------------- Stage 5: the score, computed in code
#
# The model gives a score per criterion. Code then applies limits the scores must respect whatever the model
# wrote, and computes the overall score. These limits are first settings: tune them on the labelled evaluation
# set (scripts/eval_scoring.py --compare), never on a single paper.
RETRACTED_SCORE_CAP = 5  # a retracted article: no recommendation
CONCERN_SCORE_CAP = 40  # under an expression of concern: below a recommendation
RIGOR_CAP_ONE_CONTRADICTION = 60  # rigor with a major inconsistency in the paper's own values
RIGOR_CAP_SEVERAL_CONTRADICTIONS = 45
SEVERAL_CONTRADICTIONS = 3
CRITERION_CAP_MAJOR = 75  # a criterion with a verified major concern bearing on it
CRITERION_CAP_DESK_REJECT = 40  # a criterion with a verified desk-reject concern bearing on it


def _cap(criterion: dict[str, Any], cap: int, why: str) -> None:
    if int(criterion["score"]) > cap:
        criterion["score"] = cap
        criterion.setdefault("adjustments", []).append(f"Capped at {cap}: {why}")


def _consistency_caps(criteria: dict[str, dict[str, Any]], ctx: AnalysisContext) -> None:
    """A major inconsistency in the paper's own values caps rigor, however the model scored it."""
    report = ctx.consistency
    rigor = criteria.get("rigor")
    if report is None or rigor is None:
        return
    majors = sum(1 for f in report.findings if f.severity == "major")
    if majors >= SEVERAL_CONTRADICTIONS:
        _cap(rigor, RIGOR_CAP_SEVERAL_CONTRADICTIONS, f"{majors} major inconsistencies in the paper's own values.")
    elif majors:
        _cap(rigor, RIGOR_CAP_ONE_CONTRADICTION,
             f"{majors} major inconsistenc{'y' if majors == 1 else 'ies'} in the paper's own values.")


def _concern_caps(criteria: dict[str, dict[str, Any]], reasons: list[dict[str, Any]]) -> None:
    """A criterion cannot score high while a verified concern on it says otherwise (scores and concerns agree)."""
    for reason in reasons:
        criterion = criteria.get(str(reason.get("criterion") or ""))
        if criterion is None or criterion.get("computed") or reason.get("origin") != "model":
            continue
        if reason.get("severity") == "desk_reject":
            _cap(criterion, CRITERION_CAP_DESK_REJECT, "a desk-reject concern bears on it.")
        elif reason.get("severity") == "major":
            _cap(criterion, CRITERION_CAP_MAJOR, "a major concern bears on it.")


def _publication_cap(pub: PublicationStatus | None) -> int | None:
    if pub is None:
        return None
    if pub.status == "retracted":
        return RETRACTED_SCORE_CAP
    if pub.status == "expression_of_concern":
        return CONCERN_SCORE_CAP
    return None


def score_report(out: dict[str, Any], ctx: AnalysisContext) -> None:
    """Criteria scores and the overall score, from the model's scores and the limits above.

    Always starts from the model's own criterion scores (`raw_score`), so it can run again after a later
    check changes the concerns (services.evidence_check) and give the same result for the same inputs.
    """
    criteria: dict[str, dict[str, Any]] = out.get("criteria") or {}
    for criterion in criteria.values():
        if criterion.get("raw_score") is None:
            criterion["raw_score"] = int(criterion["score"])
        criterion["score"] = int(criterion["raw_score"])
        criterion["adjustments"] = []
    _consistency_caps(criteria, ctx)
    _concern_caps(criteria, list(out.get("rejection_reasons") or []))
    from_model = any(not c.get("computed") for c in criteria.values())
    total_weight = sum(float(c.get("weight") or 0) for c in criteria.values())
    if from_model and total_weight > 0:
        overall = sum(float(c.get("weight") or 0) * int(c["score"]) for c in criteria.values()) / total_weight
    else:
        overall = int(out.get("raw_overall") or 0)
    final = max(0, min(100, int(round(overall)) - int(out.get("score_penalty") or 0)))
    cap = _publication_cap(ctx.publication)
    if cap is not None:
        final = min(final, cap)
    out["mood_score"] = final
    out["mood"] = mood_for_score(final)


def _notice_words(pub: PublicationStatus, kinds: frozenset[str]) -> tuple[str, str]:
    """("on 2026-01-19 by the publisher (notice 10.x/y)", "https://doi.org/10.x/y") for the first matching notice."""
    notice = next((n for n in pub.notices if n.type in kinds), None)
    if notice is None:
        return "", ""
    by = {"publisher": "by the publisher", "retraction-watch": "according to Retraction Watch"}.get(
        notice.source.split(", ")[0], ""
    )
    if "publisher" in notice.source.split(", "):
        by = "by the publisher"
    words = " ".join(part for part in (f"on {notice.date}" if notice.date else "", by) if part)
    link = f"https://doi.org/{notice.doi}" if notice.doi else ""
    return words, link


def _integrity_reason(pub: PublicationStatus) -> dict[str, Any] | None:
    """A reason written by code from the publication record. It needs no quotation: it is a fact about the record."""
    from services.integrity import CONCERN_TYPES, RETRACTION_TYPES

    mark = next((m for m in pub.file_marks if m.kind != "expression_of_concern"), None)
    if pub.status == "retracted":
        words, link = _notice_words(pub, RETRACTION_TYPES)
        if words or link:
            problem = f"This article has been retracted {words}".rstrip() + "."
        else:
            problem = f'The file is marked "{mark.text}": this article has been retracted.' if mark else "This article has been retracted."
        return {
            "reason": problem, "problem": problem, "severity": "desk_reject",
            "evidence": mark.text if mark else "", "location": "publication record", "impact_area": "scientific_validity",
            "impact": "A retracted article cannot be published or resubmitted as it stands, and its findings should not be relied on or cited as valid evidence.",
            "fix": "", "solution": (
                f"Read the retraction notice{f' ({link})' if link else ''} for the reasons the publisher gave. A new "
                "submission has to address them with new, verifiable data. The analysis below describes the text only."
            ),
        }
    if pub.status == "expression_of_concern":
        words, link = _notice_words(pub, CONCERN_TYPES)
        problem = f"The publisher has issued an expression of concern about this article {words}".rstrip() + "."
        return {
            "reason": problem, "problem": problem, "severity": "major", "evidence": "", "location": "publication record",
            "impact_area": "scientific_validity",
            "impact": "The publisher has doubts about the reliability of this work that are not yet resolved.",
            "fix": "", "solution": (
                f"Read the notice{f' ({link})' if link else ''} and answer each concern it raises, with the original data "
                "where it asks for them, before relying on or resubmitting this work."
            ),
        }
    return None


def _apply_publication_record(out: dict[str, Any], ctx: AnalysisContext, not_assessed: list[str]) -> None:
    """What the publication record decides, whatever the text reads like.

    Retracted: the score is capped and no recommendation is made. Expression of concern: the score is capped lower
    than a recommendation. Published (or corrected): it passed peer review, so nothing is a desk rejection.
    """
    pub = ctx.publication
    if pub is None:
        return
    # A retracted or flagged paper: the record itself is the desk rejection. A paper published in the target
    # journal passed review there, so nothing in it is a desk rejection at that journal.
    no_desk_reject = pub.status in ("retracted", "expression_of_concern") or (
        pub.status in ("published", "corrected") and bool(pub.at_target_journal)
    )
    for reason in out.get("rejection_reasons") or []:
        if no_desk_reject and reason.get("severity") == "desk_reject":
            reason["severity"] = "major"
    record_reason = _integrity_reason(pub)
    if record_reason is not None:
        _attach_reason_text(ctx, out.get("sections") or {}, [record_reason])
        record_reason["section"] = ""
        record_reason["origin"] = "publication_record"
        out["rejection_reasons"] = [record_reason] + list(out.get("rejection_reasons") or [])
    if pub.status == "retracted":
        notice = "This file is a retraction notice, not a paper." if pub.is_notice else ""
        out["verdict"] = " ".join(
            filter(None, [record_reason["problem"] if record_reason else "Retracted.", notice,
                          "No recommendation is made; the analysis below describes the text only."])
        )
        out["oracle_letter"] = (
            "Before anything else: this article has been retracted, so it is not a candidate for publication and its "
            "findings should not be cited as valid. What follows is an assessment of the text as written, not a "
            "recommendation.\n\n" + str(out.get("oracle_letter") or "")
        )
        not_assessed.insert(0, f"The overall score is capped at {RETRACTED_SCORE_CAP} because the article has been retracted.")
    elif pub.status == "expression_of_concern":
        not_assessed.insert(
            0, f"The overall score is capped at {CONCERN_SCORE_CAP} because the publisher has issued an expression of concern."
        )


def finalize_payload(parsed: dict[str, Any], ctx: AnalysisContext | None) -> dict[str, Any]:
    """Turn the model's JSON into a checked report. Without a context only the section shape is normalised."""
    out = dict(parsed)
    notes: list[str] = []
    early_type = (
        ctx.article_type if ctx is not None and ctx.article_type in pp.ARTICLE_TYPES
        else pp.coerce_article_type(parsed.get("paper_format"))
    )
    sections, roles = _clean_sections(parsed.get("sections"), ctx, notes, early_type)
    out["sections"] = sections
    out["field"] = _clean_field(parsed.get("field"))
    out.pop("absent_sections", None)
    if ctx is None:
        return out

    _attach_content(ctx, sections)
    full_texts = section_full_texts(ctx, sections)

    # ---- article type: stated in the file or chosen by the author wins over the model's reading
    model_type = pp.coerce_article_type(parsed.get("paper_format"))
    article_type = ctx.article_type if ctx.article_type in pp.ARTICLE_TYPES else (model_type or pp.UNKNOWN)
    profile = pp.get_profile(article_type)

    # ---- each section's checklist, by code
    _attach_checks(ctx, sections, full_texts, article_type, notes)

    # ---- a rejection reason must point to text that is in the paper
    unverified: list[dict[str, Any]] = []
    out["rejection_reasons"] = _clean_reasons(
        out.get("rejection_reasons"), ctx, notes, _section_starts(ctx, sections), unverified,
        frozenset(key for key, _ in profile.criteria),
    )
    _attach_reason_text(ctx, sections, out["rejection_reasons"])
    if article_type in pp.NON_RESEARCH_TYPES:
        # An editorial is invited: it is never sent to review, so it cannot be desk-rejected.
        for reason in out["rejection_reasons"]:
            if reason.get("severity") in ("desk_reject", "major"):
                reason["severity"] = "minor"

    # ---- remove quotations that are not in the paper, flag numbers that are not
    out["unverified_concerns"] = unverified
    consistency_reasons = _consistency_reasons(ctx)
    if consistency_reasons:
        _attach_reason_text(ctx, sections, consistency_reasons)
        repeated = _repeats_consistency(out["rejection_reasons"], ctx)
        if repeated:
            notes.append(f"{repeated} concern(s) repeated an inconsistency the code had already found; shown once.")
        out["rejection_reasons"] = consistency_reasons + [
            r for r in out["rejection_reasons"] if not r.get("_repeats_consistency")
        ]
    removed = [0]
    for container, key in _text_fields(out):
        container[key] = _scrub_quotes(container[key], ctx, removed)
    if removed[0]:
        notes.append(f"{removed[0]} quotation(s) in the report were removed because they are not in the manuscript.")
    checked_text = " ".join(str(c[k]) for c, k in _text_fields(out))
    for item in _ungrounded_numbers(checked_text, ctx)[:4]:
        notes.append(f'"{item}" appears in the report but not in the manuscript text. Check it before relying on it.')

    # ---- criteria and overall score
    criteria = _criteria_from_model(parsed.get("criteria"), profile)
    llm_criteria_present = bool(criteria)
    citation = _citation_criterion(ctx)
    if citation is not None and llm_criteria_present:
        criteria[pp.CITATION_CRITERION] = citation
    if not llm_criteria_present:
        notes.append("The model returned no criteria, so the overall score is its own estimate.")

    # ---- what the uploaded file could not show
    not_assessed: list[str] = []
    methods_present = "methods" in roles or pp.ROLE_METHODS in ctx.section_roles.values()
    penalty = 0
    if article_type == pp.RESEARCH_ARTICLE and not methods_present:
        if ctx.refers_to_si:
            penalty = SI_PENALTY
            not_assessed.append(
                "No Experimental section in the file, and the paper points to its Supporting Information (ESI). "
                f"Experimental details and reproducibility could not be assessed; the overall score is {SI_PENALTY} points lower for this."
            )
        else:
            penalty = MISSING_METHODS_PENALTY
            not_assessed.append(
                "No Experimental / Methods section was found in the uploaded file. "
                f"The overall score is {MISSING_METHODS_PENALTY} points lower for this."
            )
    elif article_type == pp.COMMUNICATION and ctx.refers_to_si:
        penalty = SI_PENALTY
        not_assessed.append(
            "Supporting Information (ESI) was not part of the uploaded file. Experimental details and "
            f"reproducibility could not be assessed; the overall score is {SI_PENALTY} points lower for this."
        )
    if ctx.structure_mode == "detected" and ctx.section_roles and article_type in pp.ARTICLE_TYPES:
        for role in profile.expected:
            if role not in ctx.section_roles.values() and role != pp.ROLE_METHODS:
                not_assessed.append(f'No "{role}" section was found, though a {profile.label.lower()} normally has one.')
    if article_type == pp.UNKNOWN:
        not_assessed.append("The article type could not be determined, so no section was treated as expected.")
    if article_type in pp.NON_RESEARCH_TYPES:
        not_assessed.append(
            f"This is an {profile.label.lower()}, not a research paper. It was judged on purpose, accuracy, clarity "
            "and fit, not on novelty or rigor, and an editorial is never desk-rejected."
        )
    not_assessed.extend(ctx.parse_warnings)

    _apply_publication_record(out, ctx, not_assessed)
    out["criteria"] = criteria
    out["score_penalty"] = penalty
    out["raw_overall"] = (_to_score(parsed.get("mood_score")) or 0) if not llm_criteria_present else None
    score_report(out, ctx)
    out["paper_format"] = article_type
    out["not_assessed"] = not_assessed
    out["grounding_notes"] = notes[:MAX_GROUNDING_NOTES]
    out["previous_issues"] = _previous_issue_statuses(parsed.get("previous_issues"), ctx) if ctx.previous_issues else []
    if ctx.previous_issues:
        for container in out["previous_issues"]:
            container["note"] = _scrub_quotes(container["note"], ctx, [0])
    return out


def describe_for_log(ctx: AnalysisContext | None) -> str:
    if ctx is None:
        return "no-context"
    return json.dumps(
        {"type": ctx.article_type, "source": ctx.type_source, "mode": ctx.structure_mode, "si": ctx.refers_to_si}
    )
