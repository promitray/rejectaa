"""Every statement in the report that the paper LACKS something is checked against the whole paper before it stays.

A criticism that quotes the paper can be checked by matching the quotation. "The paper lacks a controlled
experiment" cannot: there is nothing to quote. A model writes such sentences from a reviewer's checklist
("benchmark paper -> needs a controlled experiment") without noticing that Section 3 IS that experiment, just
called "Explanation 1". So, after the report is assembled and before the editor's letter is written:

A. Collect: every sentence of the report that asserts absence ("lacks", "does not report", "no evidence of", a
   finding marked "not reported"): section findings, the main analysis's concerns, section summaries, criterion
   reasons, the novelty assessment, journal fit, verdict and letter.
B. Verify: one model call with the WHOLE paper, numbered by sentence (services.paper_index), and the statements,
   each with what counts as evidence for its kind (knowledge/absence_claims.yaml: "an experiment that varies the
   suspected cause and measures the effect counts, whatever the paper calls it"). A paper too long to send whole
   is searched per statement instead (keywords + embeddings, several queries, the best passages of every section).
   For each: "present" (cite the sentences), "partly" (cite what the paper does, restate what is still missing),
   or "absent".
C. Resolve, in code: present -> the statement goes (a finding is removed, a concern is set aside as unverified, a
   sentence is cut); partly -> it is replaced by the restatement, which names what the paper does; absent -> it
   stays, marked as searched. Section scores, fix plans, the concerns taken from findings and the overall score
   are then computed again.

Cited ids must exist in the paper. Never raises: if the check cannot run, the report stays as it was and says so.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from models import AnalysisResult
from services.analysis_guard import AnalysisContext, alnum, section_full_texts
from services.paper_index import PaperIndex, build_index, ids_in, sort_key

logger = logging.getLogger(__name__)

AUDIT_PROMPT_VERSION = "2026-11-14.10"  # .10: each element located and stated on its own; pointers are not evidence
KINDS_FILE = Path(__file__).resolve().parents[1] / "knowledge" / "absence_claims.yaml"
FULL_PAPER_CHARS = 140_000  # the numbered paper up to this size is read whole; longer papers are searched
MAX_ITEMS = 30
PASSAGES_PER_QUERY = 6
PASSAGES_PER_SECTION = 2
MAX_PASSAGES_PER_ITEM = 16
MIN_CORRECTED_CHARS = 30
MIN_QUOTE_CHARS = 20  # letters and digits of the quotation that shows the thing is there

# A statement that something is not there.
ASSERTS_ABSENCE = re.compile(
    r"\b(lacks?|lacking|missing|absent|there is no|no (?:\w+ ){0,3}(?:tests?|experiments?|analys[ie]s|controls?|ablations?|"
    r"comparisons?|statements?|evidence|mention|data|details?|values?|measurements?|discussion|justification)|does not "
    r"(?:report|include|provide|state|show|mention|describe|present|conduct|perform|specify|compare|test|isolate|give|"
    r"explain|address|discuss)|did not|not (?:reported|provided|stated|included|described|conducted|performed|mentioned|"
    r"tested|specified|given|shown|isolated|addressed|discussed)|without (?:any |a |an )?(?:mention|discussion|report)|"
    r"without (?:any|a|an)|no mention|fails? to (?:report|include|provide|"
    r"show|address|test|isolate|compare|discuss))\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")


def asserts_absence(finding: dict[str, Any]) -> bool:
    return finding.get("status") == "not_reported" or bool(ASSERTS_ABSENCE.search(str(finding.get("problem") or "")))


# --------------------------------------------------------------------------- kinds of absence claim

@dataclass(frozen=True)
class Kind:
    id: str
    detect: re.Pattern[str]
    query: str
    evidence: str


@lru_cache(maxsize=1)
def kinds() -> tuple[Kind, ...]:
    raw = yaml.safe_load(KINDS_FILE.read_text(encoding="utf-8")) or {}
    return tuple(
        Kind(str(k["id"]), re.compile(str(k["detect"]), re.IGNORECASE), " ".join(str(k["query"]).split()),
             " ".join(str(k["evidence"]).split()))
        for k in raw.get("kinds") or []
    )


def kinds_version() -> str:
    raw = yaml.safe_load(KINDS_FILE.read_text(encoding="utf-8")) or {}
    return str(raw.get("version") or "")


def kind_of(text: str) -> Kind:
    for kind in kinds():
        if kind.detect.search(text):
            return kind
    return kinds()[-1]


# --------------------------------------------------------------------------- A. collect

@dataclass
class Item:
    id: str
    where: str  # what the reader sees: "finding in Benchmarking Results", "concern", "summary of ..."
    text: str
    kind: Kind
    locate: tuple[Any, ...]  # how to put the answer back
    claim: str = ""
    passages: list[str] = field(default_factory=list)
    # The section the statement is about (a finding or summary of that section, not worded about the whole paper),
    # with how the reader sees it ("Appendix D: Discussion and Conclusion, p. 11"). None: the whole paper.
    scope: str | None = None
    scope_title: str = ""


# Worded about the whole paper ("the paper never discusses X", "the authors do not report"): judged on the whole
# paper even when attached to a section. Worded about a section ("this section does not ..."), or neither: the section.
# the paper is the one that lacks it ("the paper does not state", "the authors never report"), not merely named in
# passing ("important for the paper's claims")
_PAPER_WIDE = re.compile(
    r"\b(?:the|this) (?:paper|manuscript|study|work|article|authors?) (?:does not|do not|did not|never|lacks?|fails? to|"
    r"omits?|provides? no|gives? no|reports? no|offers? no|includes? no)\b|\banywhere\b|\bthroughout\b|"
    r"\b(?:in|across) the (?:whole|entire) (?:paper|manuscript)\b",
    re.IGNORECASE,
)
_SECTION_WORDED = re.compile(r"\b(?:the|this) (?:section|appendix|introduction|discussion|conclusion|results|methods)\b", re.IGNORECASE)


def scope_of(text: str, section: str | None) -> str | None:
    """The section a statement is judged in: its own section, unless it is worded about the whole paper."""
    if section is None:
        return None
    if _PAPER_WIDE.search(text) and not _SECTION_WORDED.search(text):
        return None
    return section


def _place(name: str | None, section: dict[str, Any]) -> str:
    """How the reader finds a section: "Appendix D: Discussion and Conclusion", "Section 2: Benchmarking Results"."""
    if not name:
        return ""
    title = section.get("title") or name.capitalize()
    label = section.get("label") or ""
    if section.get("part") == "appendix":
        return f"Appendix {label}: {title}" if label.isalpha() else f"Appendix: {title}"
    return f"Section {label}: {title}" if label else title


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text or "") if s.strip()]


def collect(payload: dict[str, Any]) -> list[Item]:
    """Every statement of absence in the report, in a fixed order: findings, concerns, then prose."""
    items: list[Item] = []

    def add(where: str, text: str, locate: tuple[Any, ...], claim: str = "", section: str | None = None) -> None:
        if len(items) < MAX_ITEMS:
            scope = scope_of(text, section)
            title = _place(scope, (payload.get("sections") or {}).get(scope) or {}) if scope else ""
            items.append(Item(f"A{len(items) + 1}", where, text, kind_of(text), locate, claim, scope=scope, scope_title=title))

    sections = payload.get("sections") or {}
    for name, section in sections.items():
        title = section.get("title") or name
        for i, finding in enumerate(section.get("findings") or []):
            if asserts_absence(finding) and not finding.get("absence_checked"):
                add(f"finding in {title}", finding["problem"], ("finding", name, i), finding.get("claim") or "", name)
    for i, reason in enumerate(payload.get("rejection_reasons") or []):
        if reason.get("origin") == "model" and ASSERTS_ABSENCE.search(str(reason.get("problem") or reason.get("reason") or "")):
            add("concern", str(reason.get("problem") or reason.get("reason")), ("reason", i))
    prose: list[tuple[str, tuple[Any, ...], str]] = []
    for name, section in sections.items():
        title = section.get("title") or name
        prose.append((f"summary of {title}", ("section", name, "summary"), section.get("summary") or ""))
        if not section.get("score_basis"):  # a reviewed section's issue is its first verified finding
            prose.append((f"issue of {title}", ("section", name, "issue"), section.get("issue") or ""))
    for key, criterion in (payload.get("criteria") or {}).items():
        prose.append((f"reason for the {key} score", ("criterion", key), criterion.get("reason") or ""))
    for key in ("novelty_assessment", "journal_fit", "verdict", "oracle_letter"):
        prose.append((key.replace("_", " "), ("field", key), payload.get(key) or ""))
    for where, locate, text in prose:
        for n, sentence in enumerate(_sentences(text)):
            if ASSERTS_ABSENCE.search(sentence):
                add(where, sentence, (*locate, n), section=locate[1] if locate[0] == "section" else None)
    return items


# --------------------------------------------------------------------------- B. verify

_SYSTEM = (
    "You check statements that a manuscript lacks something. You read the manuscript given and judge what it "
    "actually does, not what it calls it. You cite the ids of the sentences you rely on. Return ONLY valid JSON."
)
_RULES = """Each ITEM below is a statement that the paper lacks something. For each:

1. Split the statement into its PARTS: each distinct THING it says is missing (usually 1-4), named as a short noun
   phrase of what is missing, never the statement itself. "Does not report n, statistical tests, or P values" has
   three parts: "n"; "a statistical test"; "P values". "Lacks a controlled experiment to isolate the cause" has
   one: "a controlled experiment isolating the cause". "The section claims X but gives no quantitative evidence
   for it" has one: "quantitative results supporting X".
2. For each part, search the WHOLE paper (appendices and captions included) and give
   {"part": "...", "found": true|false, "where": "section or appendix", "quotes": ["...", "..."]}: up to 3
   quotations, each the exact words (up to 40, consecutive) of one passage that contains THAT part, the most
   direct first (for results: the sentence that reports them, e.g. "As shown in Table 2, ..."). A part is found
   wherever it is, even only in an appendix or a caption: then say where (e.g. "Appendix A only, not in the main
   text").
3. Verdict: "present" if every part is found; "partly" if some are (the paper has it but incomplete, or only in
   an appendix); "absent" only if no part is found anywhere.
4. For "partly", write "corrected": the statement restated so that it FIRST names what the paper does, and where
   (e.g. "Standard deviations over 50 runs are given in Appendix A"), and THEN states precisely what is still
   missing (e.g. "but no significance test compares the models in the main text"). It must not deny anything
   the paper shows.

A part is "found" wherever in the paper it is, even if the statement talks about one section: always search the
whole paper and quote what you find.
Locate EACH part on its own: never give one part's location or quotation to another part of the same statement.
If part A is in Section X and part B is nowhere, say so for each separately.
A sentence that only says where a thing is ("the standard deviations can be seen in the appendix", "see the
Supporting Information") is a POINTER, not the thing: quote the thing itself where it is shown (the table, the
values). If you find only the pointer, set "found": false and put the pointer sentence in "quotes".
Be exact: "absent" and "present but incomplete" are different findings. The topic alone is not a part found: a
passage that describes preprocessing does not show that it was fitted on the training data only; a passage that
mentions a figure does not show error bars in it. Use each item's "counts as evidence" note. Cite in "evidence"
the sentence ids (e.g. "P14.2") of the parts found.
Return {"items": [{"id": "A1", "parts": [{"part": "...", "found": false, "where": "", "quote": ""}],
"verdict": "absent", "evidence": [], "corrected": "", "note": "one sentence"}]} (each part with its "quotes")"""


def _paper_index(payload: dict[str, Any], ctx: AnalysisContext) -> PaperIndex:
    """The paper with the same sentence ids as the section review (same texts, same order)."""
    if ctx.structure_mode == "detected" and ctx.section_texts:
        texts = {name: text for name, text in ctx.section_texts.items() if text.strip()}
    else:
        texts = section_full_texts(ctx, payload.get("sections") or {})
    return build_index(ctx.abstract, texts)


async def _passages(items: list[Item], index: PaperIndex, provider: Any) -> None:
    """For a paper too long to read whole: per statement, the best passages for its kind's query and for its own
    words, and the best passages of every section (appendices included)."""
    from services.retrieval import build_search, embed_texts

    search = await build_search(index, provider)
    queries: list[str] = []
    for item in items:
        queries += [f"{item.kind.query} {item.claim}", item.text]
    vectors = await embed_texts(provider, queries)
    for n, item in enumerate(items):
        found: list[str] = []
        for query, vector in ((queries[2 * n], vectors[2 * n]), (queries[2 * n + 1], vectors[2 * n + 1])):
            found += search.search(query, k=PASSAGES_PER_QUERY, query_vector=vector)
            for section in index.sections:
                found += search.search(query, k=PASSAGES_PER_SECTION, query_vector=vector, sections={section})
        item.passages = list(dict.fromkeys(found))[:MAX_PASSAGES_PER_ITEM]


def _item_block(item: Item, index: PaperIndex, with_passages: bool) -> str:
    # The checker is not told the statement's scope: it searches the whole paper and quotes what it finds. Which
    # section each quotation is in, and what that means for a statement about one section, is decided by code.
    lines = [f"### {item.id} ({item.where})", f"STATEMENT: {item.text}"]
    if item.claim:
        lines.append(f"ABOUT THE PAPER'S CLAIM: {item.claim}")
    lines.append(f"COUNTS AS EVIDENCE: {item.kind.evidence}")
    if with_passages:
        lines.append("PASSAGES (from every section of the paper):\n" + index.numbered_paragraphs(item.passages))
    return "\n".join(lines)


async def verify(items: list[Item], index: PaperIndex, provider: Any) -> tuple[dict[str, dict[str, Any]], str]:
    """The checker's answer per item id, and the mode ("full paper" or "retrieval")."""
    whole ="\n\n".join(f"## {name.upper()}\n{index.numbered(name)}" for name in index.sections)
    if len(whole) <= FULL_PAPER_CHARS:
        mode = "full paper"
        prompt = f"THE PAPER, one sentence per line with its id:\n\n{whole}\n\n{_RULES}\n\nITEMS:\n\n" + "\n\n".join(
            _item_block(item, index, False) for item in items
        )
    else:
        mode = "retrieval"
        await _passages(items, index, provider)
        prompt = (
            f"THE PAPER'S OUTLINE: {', '.join(index.sections)}. The paper is long: each item comes with the passages "
            f"most likely to answer it, from every section.\n\n{_RULES}\n\nITEMS:\n\n"
            + "\n\n".join(_item_block(item, index, True) for item in items)
        )
    raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0)
    raw = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
    answers = {str(v.get("id")): v for v in (raw or {}).get("items") or [] if isinstance(v, dict)} if isinstance(raw, dict) else {}
    return answers, mode


# --------------------------------------------------------------------------- C. resolve

_ID_REFERENCE = re.compile(r"\s*\(?\[?\bP\d+(?:\.\d+)?\]?(?:\s*(?:,|and)\s*\[?P\d+(?:\.\d+)?\]?)*\)?")


def _without_ids(text: str) -> str:
    return re.sub(r"\s+([.,;:])", r"\1", _ID_REFERENCE.sub("", text or "")).strip()


def _clean(text: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("\"“”")[:limit]


_SMALL_WORDS = frozenset("that this with from have does were been their there which paper section also only".split())


def _words(text: str) -> set[str]:
    """Content words of four letters or more: a restatement must share one with the statement or the passages."""
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _SMALL_WORDS}


def locate_quote(quote: str, index: PaperIndex) -> list[str]:
    """The sentence ids whose words contain `quote` (letters and digits), within one sentence or across two that
    follow each other. Empty when the words are not in the paper."""
    ids = sorted(index.sentences, key=sort_key)
    texts = [alnum(index.sentences[i].text) for i in ids]
    for n, text in enumerate(texts):
        if quote in text:
            return [ids[n]]
    for n in range(len(texts) - 1):
        if quote in texts[n] + texts[n + 1]:
            return [ids[n], ids[n + 1]]
    return []


# A sentence that only says where a thing is ("The standard deviations can be seen in the appendix", "see the
# Supporting Information") is a pointer, not the thing: it counts only if the thing itself is found where it points.
_POINTER = re.compile(
    r"\b(?:can be (?:seen|found)|is|are|see|refer to|available|deferred|moved|placed)\b[^.]{0,40}\b(?:in|at|to)\s+"
    r"(?:the\s+)?(?:online\s+)?"
    r"(?:appendix|appendices|supplementary|supplemental|supporting information|esi|\bsi\b|extended data)",
    re.IGNORECASE,
)
_UPLOAD_ONLY = re.compile(r"(?i)supplementary|supplemental|supporting information|\besi\b|\bsi\b|extended data")


@dataclass
class Part:
    name: str
    found: bool  # found AND its quotation (not a mere pointer) is in the paper
    where: str
    ids: list[str]
    sections: tuple[str, ...] = ()  # the sections its quotation is in, by sentence id (code, not the checker)
    pointer: str = ""  # the paper only says where it is ("... can be seen in the appendix"), word for word
    pointer_ids: tuple[str, ...] = ()

    def found_in(self, section: str | None) -> bool:
        return self.found and (section is None or section in self.sections)


def _parts(answer: dict[str, Any], index: PaperIndex) -> list[Part]:
    """The statement's parts as the checker split them, each located ON ITS OWN: a part counts as found only when one
    of its own quotations is in the paper and is not merely a pointer to somewhere else (the quotation is the
    evidence, not the ids; one part's location is never given to another)."""
    out: list[Part] = []
    for raw in answer.get("parts") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean(raw.get("part"), 120)
        if not name:
            continue
        quotes = raw.get("quotes") if isinstance(raw.get("quotes"), list) else []
        quotes = [*quotes, raw.get("quote")]
        located: list[str] = []
        pointers: list[str] = []
        if raw.get("found") is True:
            for quote in quotes[:4]:
                text = alnum(_clean(quote))
                if len(text) < MIN_QUOTE_CHARS:
                    continue
                ids = locate_quote(text, index)
                if not ids:
                    continue
                if _POINTER.search(index.text_of(ids)) and _POINTER.search(_clean(quote)):
                    pointers += [i for i in ids if i not in pointers]
                else:
                    located += [i for i in ids if i not in located]
        located = sorted(located, key=sort_key)
        sections = tuple(dict.fromkeys(s for s in (index.section_of(i) for i in located) if s))
        pointer = index.text_of(sorted(pointers, key=sort_key))[:240] if pointers and not located else ""
        out.append(Part(name, bool(located), _clean(raw.get("where"), 80), located, sections, pointer,
                        tuple(sorted(pointers, key=sort_key)) if pointer else ()))
    return out


def _part_clause(p: Part, item: Item, titles: dict[str, str]) -> str:
    """One element, with its own location only."""
    if p.found:
        places = ", ".join(titles.get(s, s) for s in p.sections) or p.where
        if item.scope and p.found_in(item.scope):
            return f"{p.name} is given in this section"
        return f"{p.name} is given in {places}" if places else f"{p.name} is given"
    if p.pointer:
        short = p.pointer if len(p.pointer) <= 120 else p.pointer[:117].rstrip() + "…"
        not_here = (
            "it is not part of the uploaded file" if _UPLOAD_ONLY.search(p.pointer)
            else "the appendices in the file do not show it"
        )
        return f"{p.name} is only referred to (“{short}”), but {not_here}"
    return f"{p.name} is not given anywhere in the paper"


def _per_part(item: Item, parts: list[Part], titles: dict[str, str]) -> str:
    """The statement restated element by element, each with its own location (never one location for several)."""
    clauses = [_part_clause(p, item, titles) for p in parts]
    missing_here = [p.name for p in parts if not p.found_in(item.scope)]
    own_card = item.locate[0] in ("finding", "section") and item.scope
    if len(parts) == 1 and own_card and missing_here:
        # one element: "This section does not give X; it is given in Section 3" / "...: it is only referred to (...)"
        rest = clauses[0][len(parts[0].name):].strip()
        return f"This section does not give {parts[0].name}; it {rest}."
    if own_card and missing_here and any(p.found for p in parts):
        lead = f"This section does not give {', '.join(missing_here)}. In the paper: "
    elif own_card and missing_here:
        lead = "This section: "
    else:
        lead = ""
    text = lead + "; ".join(clauses)
    return (text[0].upper() + text[1:] if text else text) + "."


def _decide(answer: dict[str, Any], item: Item, index: PaperIndex, titles: dict[str, str] | None = None) -> tuple[str, list[str], str]:
    """(verdict, cited ids that exist, restatement). With parts, code decides from the parts whose quotations are
    in the paper, and from WHERE they are: for a statement about one section only text in that section removes it.
    All parts found (in scope) -> present; some, or found only in other sections -> partly (with a cross-reference);
    none anywhere -> absent, whatever verdict the checker wrote. Without parts, the checker's verdict stands only if
    its quotation is in the paper (and, for a section-scoped statement, in that section)."""
    titles = titles or {}
    parts = _parts(answer, index)
    if parts:
        found_ids = sorted({i for p in parts if p.found for i in p.ids}, key=sort_key)
        cited = sorted({i for i in ids_in(answer.get("evidence")) if i in index.sentences} | set(found_ids), key=sort_key)
        if not any(p.found for p in parts):
            if any(p.pointer for p in parts):
                return "partly", [], _per_part(item, parts, titles)  # only pointed to: say so, and where it points
            return "absent", [], ""
        if all(p.found_in(item.scope) for p in parts):
            return "present", cited, ""
        if any(p.found and not p.found_in(item.scope) for p in parts) or any(p.pointer for p in parts) or len(parts) > 1:
            # several elements, an element found elsewhere, or one only pointed to: stated element by element, each
            # with its own location, by code (the checker's restatement tends to merge them)
            return "partly", cited, _per_part(item, parts, titles)
        corrected = _without_ids(_clean(answer.get("corrected")))
        sound = len(corrected) >= MIN_CORRECTED_CHARS and bool(_words(corrected) & _words(f"{item.text} {index.text_of(found_ids)}"))
        return "partly", cited, corrected if sound else _per_part(item, parts, titles)
    verdict = str(answer.get("verdict") or "").strip().lower()
    cited = sorted({i for i in ids_in(answer.get("evidence")) if i in index.sentences}, key=sort_key)
    if verdict not in ("present", "partly") or not cited:
        return "absent", [], ""
    quote = alnum(_clean(answer.get("quote")))
    located = locate_quote(quote, index) if len(quote) >= MIN_QUOTE_CHARS else []
    if located:
        # the quotation is the evidence; the ids only point to it, and a model often points one sentence off
        cited = sorted(set(cited) | set(located), key=sort_key)
        if verdict == "present" and item.scope and item.scope not in {index.section_of(i) for i in located}:
            return "absent", [], ""  # the evidence is in another section: it does not remove a gap of this one
    elif verdict == "present":
        # no words of the paper that contain the thing: that it is there is not shown, so the statement stays
        return "absent", [], ""
    if verdict == "partly":
        corrected = _without_ids(_clean(answer.get("corrected")))
        if len(corrected) < MIN_CORRECTED_CHARS or not (_words(corrected) & _words(f"{item.text} {index.text_of(cited)}")):
            return "absent", [], ""  # a restatement that says nothing usable: the statement stays, marked searched
        return "partly", cited, corrected
    return "present", cited, ""


# --------------------------------------------------------------------------- B2. confirm before removing

_CONFIRM_SYSTEM = (
    "You decide whether a sentence from a manuscript states a specific thing. You are literal: being about the same "
    "topic is not stating it. Return ONLY valid JSON."
)
_CONFIRM_RULES = """A reviewer said a part of a manuscript lacks something. Each item gives the reviewer's STATEMENT, the THING it
says is missing, what COUNTS AS EVIDENCE for that kind of thing, and the sentence(s) of the manuscript offered as
showing that the THING is there. Judge by the COUNTS AS EVIDENCE note. Answer:
- A sentence that only says where the THING is ("can be seen in the appendix", "see the SI") does not state it.
- "states": one of the sentences itself states or reports the THING (in its own words or other terms, but the
  THING itself; a sentence that reports what an experiment, table or figure shows states those results: "the
  performance of deep models improves dramatically as the level of smoothing increases" reports a quantitative
  result);
- "related": the sentence is about the same topic but does not state the THING (e.g. it gives a different cause,
  a general remark, or only part of it);
- "no": it does not.
Example: THING "the failure is not solely due to small dataset size"; sentence "These phenomena can be attributed
to the unique patterns of molecular data" -> "related" (it gives a cause, but says nothing about dataset size).
Return {"items": [{"id": "E1", "answer": "related"}]}"""


async def confirm_removals(items: list[Item], answers: dict[str, dict[str, Any]], index: PaperIndex, provider: Any) -> int:
    """Every part counted as found is checked once more, on its own: do its sentences STATE the thing, or are they
    only on the same topic? This holds for every use of a found part: removing a statement, narrowing it ("the paper
    gives X, but not Y") and a cross-reference ("covered in Section 3") all claim the paper has the thing. A part
    whose sentences only relate is counted as not found (in `answers`, in place). Returns how many were demoted.
    A failed call confirms nothing, so nothing is removed or narrowed on its strength."""
    requests: list[tuple[str, dict[str, Any], list[str], Item]] = []
    for item in items:
        answer = answers.get(item.id) or {}
        raw_parts = [r for r in answer.get("parts") or [] if isinstance(r, dict) and _clean(r.get("part"), 120)]
        for raw, part in zip(raw_parts, _parts(answer, index)):
            if part.found:
                requests.append((f"E{len(requests) + 1}", raw, part.ids, item))
    if not requests:
        return 0
    # the same yardstick as the check itself: what counts as having this kind of thing
    blocks = [
        f"### {rid}\nSTATEMENT (the reviewer's): {_clean(item.text, 400)}\nTHING said to be missing: "
        f"{_clean(raw.get('part'), 200)}\nCOUNTS AS EVIDENCE: {item.kind.evidence}\n"
        f"SENTENCES of the paper: {index.text_of(ids)[:900]}"
        for rid, raw, ids, item in requests
    ]
    try:
        raw_text = await provider.generate(_CONFIRM_SYSTEM, f"{_CONFIRM_RULES}\n\n" + "\n\n".join(blocks), temperature=0.0)
        parsed = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
        verdicts = {str(v.get("id")): str(v.get("answer") or "").lower() for v in (parsed or {}).get("items") or [] if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 - nothing unconfirmed is removed
        logger.warning("Confirming removals failed; keeping the statements", exc_info=True)
        verdicts = {}
    demoted = 0
    for rid, raw, _, _ in requests:
        if verdicts.get(rid) != "states":
            raw["found"] = False
            raw["related_only"] = True
            demoted += 1
    return demoted


def _replace_sentence(text: str, n: int, new: str | None) -> str:
    parts = _sentences(text)
    if n >= len(parts):
        return text
    if new:
        parts[n] = new if new.endswith((".", "!", "?")) else new + "."
    else:
        parts.pop(n)
    return " ".join(parts).strip()


MIN_SHARED_WORDS = 2  # long content words two statements share to be about the same thing


def _long_words(text: str) -> set[str]:
    return {w[:7] for w in re.findall(r"[a-z]{5,}", text.lower()) if w not in _SMALL_WORDS}


def _reconcile(items: list[Item], decisions: dict[str, tuple[str, list[str], str]]) -> int:
    """Two statements about the same thing in the same section (a finding and a summary sentence: "does not address
    the dataset-size claim") must not get opposite outcomes. When one would be removed ("present") and the other
    kept or kept with a cross-reference, the removal is cancelled: the more careful outcome wins. Returns how many."""
    changed = 0
    for item in items:
        verdict = decisions[item.id][0]
        if verdict != "present" or item.scope is None:
            continue
        mine = _long_words(item.text)
        for other in items:
            if other is item or other.scope != item.scope or decisions[other.id][0] == "present":
                continue
            if len(mine & _long_words(other.text)) >= MIN_SHARED_WORDS:
                decisions[item.id] = ("absent", [], "")
                changed += 1
                break
    return changed


def apply(payload: dict[str, Any], items: list[Item], answers: dict[str, dict[str, Any]], index: PaperIndex) -> dict[str, Any]:
    """Put the verdicts back into the report. Returns the audit's account."""
    account: dict[str, Any] = {"checked": len(items), "present": 0, "partly": 0, "absent": 0, "items": []}
    removed_findings: list[tuple[str, int]] = []
    removed_reasons: list[int] = []
    sentence_edits: list[tuple[tuple[Any, ...], str | None]] = []
    ledger = payload.get("evidence_ledger") or None
    titles = {name: _place(name, section) for name, section in (payload.get("sections") or {}).items()}
    decisions = {item.id: _decide(answers.get(item.id) or {}, item, index, titles) for item in items}
    kept_for_consistency = _reconcile(items, decisions)
    account["kept_for_consistency"] = kept_for_consistency
    for item in items:
        answer = answers.get(item.id) or {}
        verdict, cited, corrected = decisions[item.id]
        parts = _parts(answer, index)
        elsewhere_only = bool(parts) and all(p.found for p in parts) and not all(p.found_in(item.scope) for p in parts)
        account[verdict] += 1
        shown = index.text_of(cited)[:600] if cited else ""
        account["items"].append({
            "where": item.where, "statement": item.text[:400], "kind": item.kind.id, "verdict": verdict,
            "evidence_ids": cited, "evidence": shown, "corrected": corrected,
            "scope": item.scope_title or "the whole paper",
            "parts": [
                {"part": p.name, "found": p.found, "where": ", ".join(titles.get(s, s) for s in p.sections) or p.where,
                 "in_scope": p.found_in(item.scope), "evidence_ids": p.ids or list(p.pointer_ids), "pointer": p.pointer}
                for p in parts
            ],
        })
        kind = item.locate[0]
        if kind == "finding":
            _, name, i = item.locate
            finding = payload["sections"][name]["findings"][i]
            finding["absence_checked"] = True
            if verdict == "present":
                removed_findings.append((name, i))
                if ledger is not None:
                    ledger.setdefault("removed_findings", []).append({
                        "section": name, "check": finding.get("check"), "problem": finding["problem"][:300],
                        "reason": "found in the paper", "detail": f"{', '.join(cited)}: {shown[:240]}",
                    })
                    removed = ledger.setdefault("removed", {})
                    removed["found_in_paper"] = removed.get("found_in_paper", 0) + 1
            elif verdict == "partly":
                finding["problem"] = corrected
                finding["addressed"], finding["addressed_ids"] = shown, cited
                if finding.get("status") == "not_reported":
                    finding["status"], finding["kind"] = "problem", "change"
                if elsewhere_only:
                    # the paper does it, only not in this section: a gap of this section, not of the paper. The
                    # fix and the consequence written for "the paper lacks it" no longer hold.
                    covered = ", ".join(dict.fromkeys(
                        titles.get(sec, sec) for part in parts for sec in part.sections if sec != item.scope
                    ))
                    finding["severity"] = "minor"
                    finding["cross_reference"] = True
                    finding["fix"] = (
                        f"Point to {covered} from this section where the claim is made (e.g. \"see {covered}\"), and "
                        "state its result here in one sentence."
                    )
                    finding["impact"] = f"A reader of this section does not see the evidence for the claim; it is in {covered}."
                    finding["kind"] = "add"
                if ledger is not None:
                    removed = ledger.setdefault("removed", {})
                    removed["narrowed"] = removed.get("narrowed", 0) + 1
            else:
                finding["searched"] = True
        elif kind == "reason":
            reason = payload["rejection_reasons"][item.locate[1]]
            if verdict == "present":
                reason["unverified_reason"] = f"The paper does contain what this concern says is missing ({', '.join(cited)}: “{shown[:200]}”)."
                removed_reasons.append(item.locate[1])
            elif verdict == "partly":
                reason["problem"] = reason["reason"] = corrected
        else:
            if verdict == "present":
                sentence_edits.append((item.locate, None))
            elif verdict == "partly":
                sentence_edits.append((item.locate, corrected))

    # sentences: last first, so the positions of the others stay right
    for locate, new in sorted(sentence_edits, key=lambda e: e[0][-1], reverse=True):
        kind = locate[0]
        if kind == "section":
            _, name, key, n = locate
            payload["sections"][name][key] = _replace_sentence(payload["sections"][name].get(key) or "", n, new) or None
        elif kind == "criterion":
            _, key, n = locate
            payload["criteria"][key]["reason"] = _replace_sentence(payload["criteria"][key].get("reason") or "", n, new)
        elif kind == "field":
            _, key, n = locate
            payload[key] = _replace_sentence(payload.get(key) or "", n, new)
    for name, i in sorted(removed_findings, reverse=True):
        payload["sections"][name]["findings"].pop(i)
    if removed_reasons:
        moved = [payload["rejection_reasons"][i] for i in sorted(removed_reasons)]
        payload["rejection_reasons"] = [r for i, r in enumerate(payload["rejection_reasons"]) if i not in set(removed_reasons)]
        payload.setdefault("unverified_concerns", []).extend(moved)
    account["changed_sections"] = sorted({name for name, _ in removed_findings} | {
        item.locate[1] for item in items if item.locate[0] == "finding"
    })
    return account


async def audit(analysis: AnalysisResult, ctx: AnalysisContext, provider: Any) -> AnalysisResult:
    """The report with every statement of absence checked against the paper. Never raises."""
    from services.section_review import refresh_after_audit

    payload = analysis.model_dump(mode="json")
    items = collect(payload)
    if not items or not hasattr(provider, "generate"):
        return analysis
    index = _paper_index(payload, ctx)
    if not index.sentences:
        return analysis
    try:
        answers, mode = await verify(items, index, provider)
    except Exception:  # noqa: BLE001 - the report stays, with a note
        logger.warning("Absence audit failed; the report keeps its statements", exc_info=True)
        notes = list(payload.get("grounding_notes") or [])
        notes.append("Statements that something is missing could not be checked against the whole paper this time.")
        payload["grounding_notes"] = notes
        return AnalysisResult.model_validate(payload)
    rejected = await confirm_removals(items, answers, index, provider)
    account = apply(payload, items, answers, index)
    account["mode"] = mode
    account["not_specific"] = rejected
    account["version"] = AUDIT_PROMPT_VERSION
    refresh_after_audit(payload, ctx, account["changed_sections"])
    payload["absence_audit"] = account
    if account["present"] or account["partly"]:
        notes = list(payload.get("grounding_notes") or [])
        notes.append(
            f"{account['checked']} statement(s) that something is missing were checked against the {mode}: "
            f"{account['present']} removed (the paper has it), {account['partly']} corrected to say what the paper does."
        )
        payload["grounding_notes"] = notes
    return AnalysisResult.model_validate(payload)
