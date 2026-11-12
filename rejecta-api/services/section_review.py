"""The section review: one model reading per section, with every quote checked against the paper.

The main analysis writes one sentence per section, because it has to write everything else in the same answer.
Here each section is read on its own, with what a strong section of its kind does, and with the checklist code
has already decided for it (services.section_checks) as facts. The model returns, per section, a short summary,
up to two strengths and up to four findings, each finding with a severity, the exact words it is about and a fix.

Then code checks the answer, the way rejection reasons are checked:
- a quote that is not in the section (or anywhere in the paper) removes the finding or strength;
- a fix that shares nothing specific with its problem and quote is advice for any paper, so the finding goes;
- a finding about a passage the checklist already quotes is the checklist's item again, so it goes.

Sections go to the model in small batches that run at the same time, so a long paper gets the same depth per
section as a short one. With headings found by the parser the review starts together with the main analysis.

Never raises: if the review cannot be done, the report keeps the main analysis's sections and says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from models import AnalysisResult, SectionCheck
from services import paper_profile as pp
from services.analysis_guard import (
    ANCHOR_PREFIX_CHARS,
    AnalysisContext,
    _stems,
    alnum,
    compute_checks,
    section_full_texts,
)

logger = logging.getLogger(__name__)

SECTION_REVIEW_PROMPT_VERSION = "2026-11-13.5"  # .5: fix plan per section
MAX_SECTIONS_PER_CALL = 4
MAX_CHARS_PER_CALL = 60_000
MIN_SECTION_CHARS = 2_000  # the least of a section's text a call sends
REVIEW_BUDGET_SECONDS = 120
MAX_FINDINGS = 4
MAX_STRENGTHS = 2
MIN_QUOTE_CHARS = 20  # letters and digits; shorter quotes match by chance
MAX_TEXT_CHARS = 500
SEVERITIES = ("major", "minor")

# What a strong section of each kind does: the yardstick the model reads each section against.
_STANDARDS = {
    pp.ROLE_INTRODUCTION: (
        "states the problem and why it matters, what is already known (with citations), the specific gap, and "
        "ends with what this paper does and finds"
    ),
    pp.ROLE_METHODS: (
        "lets another group repeat the work: materials and their sources, every procedure with its conditions, "
        "instruments and software with versions, sample sizes and replicates, the statistics; for computational "
        "work the data and splits, the hyperparameters and how they were chosen, and the baselines"
    ),
    pp.ROLE_RESULTS: (
        "backs every claim with data that are shown (a figure or table), gives values with their spread and n, "
        "uses controls or baselines, compares fairly with earlier work, and does not interpret beyond the data"
    ),
    pp.ROLE_DISCUSSION: (
        "interprets the results against earlier work, states the limitations, claims no more than the results "
        "show, and says what follows from them"
    ),
    pp.ROLE_THEMATIC: (
        "covers the key work on its topic with citations, compares the approaches critically instead of listing "
        "them, and draws a conclusion of its own"
    ),
}
_STANDARD_OTHER = "does what its heading promises, clearly and with evidence for each claim"

_SYSTEM = (
    "You are an experienced handling editor reading a manuscript section by section before deciding whether to "
    "send it to review. You are specific, fair and literal: every point names content of this paper and quotes "
    "it. You do not invent problems, and you say so when a section is good. Return ONLY valid JSON."
)

_RULES = """For EACH section under REVIEW THESE SECTIONS, return one entry with its exact "name".

- "summary": two sentences: what this section does, and its main weakness (or why it is strong). Specific to this paper.
- "strengths": 0-2 things a reviewer would praise, each with "quote": 8-40 consecutive words copied exactly from that section.
- "findings": 0-4 problems, the most important first. Each has:
  - "severity": "major" when a reviewer would require it before acceptance (a claim the data shown do not
    support, a missing control or baseline, a key step that cannot be repeated, a wrong statement); "minor" for
    a fixable detail, wording or presentation.
  - "problem": one or two sentences that name the specific content (the experiment, value, claim or step).
  - "quote": 8-40 consecutive words copied EXACTLY from that section's text, the passage the problem is about.
    For something missing, quote the sentence where it belongs (e.g. the sentence that describes the procedure).
  - "fix": one concrete action for this paper, naming exactly what to add or change.
- Advice that would fit any paper ("improve clarity", "add more details", "expand the discussion") is not a
  finding unless it says exactly what and where.
- The items "already found by code" are shown to the author already: do not repeat them. You may build on them.
- Use only the text given. Every number you write must appear in it. The Supporting Information is not provided:
  never fault a section for what normally sits there.
- A good section gets few findings or none. Never pad.

Return {"sections": [{"name": "...", "summary": "...", "strengths": [{"point": "...", "quote": "..."}],
"findings": [{"severity": "major", "problem": "...", "quote": "...", "fix": "..."}]}]}
The "sections" list MUST have one entry for every section under REVIEW THESE SECTIONS, in the same order: an
entry with an empty "findings" list is the right answer for a section with nothing to fault.
"""

_CHECK_MARK = {"missing": "✗", "issue": "!", "met": "✓", "info": "·"}


@dataclass
class SectionInput:
    name: str
    title: str
    role: str
    text: str
    checks: list[SectionCheck] = field(default_factory=list)


@dataclass
class SectionReview:
    """The model's answer per section name (unchecked), and whether the review ran."""

    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "not_run"  # done | partial | failed | not_run
    note: str = ""


# --------------------------------------------------------------------------- inputs

def inputs_from_parsed(ctx: AnalysisContext) -> list[SectionInput]:
    """The sections the parser found under headings, with their checklists: ready before the main analysis."""
    texts = {name: text.strip() for name, text in ctx.section_texts.items() if text.strip()}
    article_type = ctx.article_type if ctx.article_type in pp.ARTICLE_TYPES else pp.UNKNOWN
    checks = compute_checks(ctx, texts, ctx.section_roles, ctx.section_titles, article_type)
    return [
        SectionInput(name, ctx.section_titles.get(name, ""), ctx.section_roles.get(name, "other"), text, checks.get(name, []))
        for name, text in texts.items()
    ]


def inputs_from_analysis(ctx: AnalysisContext, analysis: AnalysisResult) -> list[SectionInput]:
    """The sections the main analysis divided the paper into (a paper without headings)."""
    sections = {name: s.model_dump() for name, s in analysis.sections.items()}
    texts = section_full_texts(ctx, sections)
    return [
        SectionInput(name, str(sections[name].get("title") or ""), str(sections[name].get("role") or "other"),
                     text, list(analysis.sections[name].checks))
        for name, text in texts.items()
    ]


# --------------------------------------------------------------------------- the model call

def _fit(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = int(budget * 0.7)
    return text[:head].rstrip() + "\n[… middle of the section omitted …]\n" + text[-(budget - head):].lstrip()


def _batches(inputs: list[SectionInput]) -> list[list[SectionInput]]:
    batches: list[list[SectionInput]] = []
    current: list[SectionInput] = []
    size = 0
    for item in inputs:
        length = min(len(item.text), MAX_CHARS_PER_CALL)
        if current and (len(current) >= MAX_SECTIONS_PER_CALL or size + length > MAX_CHARS_PER_CALL):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        batches.append(current)
    return batches


def _section_block(item: SectionInput, budget: int) -> str:
    heading = f' — heading in the paper: "{item.title}"' if item.title and item.title.lower() != item.name.lower() else ""
    lines = [
        f'### name: "{item.name}"{heading} — role: {item.role} — {len(item.text.split()):,} words',
        f"A strong section of this kind {_STANDARDS.get(item.role, _STANDARD_OTHER)}.",
    ]
    found = [c for c in item.checks if c.status in ("missing", "issue", "met")]
    if found:
        lines.append("Already found by code (shown to the author):")
        lines.extend(f"- {_CHECK_MARK[c.status]} {c.label}" + (f": {c.detail}" if c.detail else "") for c in found)
    lines.append(f"TEXT:\n<<<\n{_fit(item.text, budget)}\n>>>")
    return "\n".join(lines)


def _prompt(batch: list[SectionInput], all_names: list[str], ctx: AnalysisContext, journal: str, article_type: str) -> str:
    share = max(MIN_SECTION_CHARS, MAX_CHARS_PER_CALL // max(1, len(batch)))
    type_label = pp.get_profile(article_type).label if article_type in pp.ARTICLE_TYPES else "not determined"
    return (
        f"{_RULES}\nTARGET JOURNAL: {journal or 'not given'}\nARTICLE TYPE: {type_label}\n"
        f"ABSTRACT:\n{_fit(ctx.abstract, 3000)}\n\nSECTIONS OF THE PAPER, in order: "
        + ", ".join(f'"{n}"' for n in all_names)
        + "\n\nREVIEW THESE SECTIONS:\n\n"
        + "\n\n".join(_section_block(item, share) for item in batch)
    )


async def _call(provider: Any, prompt: str) -> dict[str, dict[str, Any]]:
    raw = await provider.generate(_SYSTEM, prompt, temperature=0.0)
    parsed = provider.parse_json_response(raw) if hasattr(provider, "parse_json_response") else json.loads(raw)
    out: dict[str, dict[str, Any]] = {}
    for entry in (parsed or {}).get("sections") or [] if isinstance(parsed, dict) else []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"] not in out:
            out[entry["name"]] = entry
    return out


async def review_sections(
    inputs: list[SectionInput], journal: str, provider: Any, ctx: AnalysisContext, article_type: str | None = None
) -> SectionReview:
    """The model's reading of every section, in parallel batches. Never raises."""
    if not inputs or not hasattr(provider, "generate"):
        return SectionReview()
    kind = article_type or (ctx.article_type if ctx.article_type in pp.ARTICLE_TYPES else pp.UNKNOWN)
    names = [item.name for item in inputs]
    merged: dict[str, dict[str, Any]] = {}
    failed = 0
    total_calls = 0

    async def run(batches: list[list[SectionInput]]) -> None:
        nonlocal failed, total_calls
        results = await asyncio.gather(
            *[_call(provider, _prompt(batch, names, ctx, journal, kind)) for batch in batches], return_exceptions=True
        )
        for result in results:
            total_calls += 1
            if isinstance(result, BaseException):
                failed += 1
                logger.warning("A section review call failed", exc_info=result)
                continue
            merged.update({name: entry for name, entry in result.items() if name in names and name not in merged})

    async def everything() -> None:
        await run(_batches(inputs))
        # The model sometimes answers for only the first section of a batch, and not the same ones from one run to
        # the next. A section it skipped is asked for again, on its own, so each gets a full reading.
        left = [item for item in inputs if item.name not in merged]
        if left and failed < total_calls:
            await run([[item] for item in left])

    try:
        await asyncio.wait_for(everything(), REVIEW_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Section review timed out")
        if not merged:
            return SectionReview(status="failed", note="The section-by-section review took too long and was left out.")
        failed += 1
    if not merged:
        return SectionReview(status="failed", note="The section-by-section review could not be completed this time.")
    missing = [n for n in names if n not in merged]
    if missing or failed:
        return SectionReview(
            sections=merged, status="partial",
            note="Part of the section-by-section review could not be completed this time"
                 + (f" ({', '.join(missing)})." if missing else "."),
        )
    return SectionReview(sections=merged, status="done")


# --------------------------------------------------------------------------- checking the answer

def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("\"“”")[:limit]


def _found(quote: str, own: str, paper: str) -> bool:
    """Whether the quote is in the section (or, failing that, anywhere in the paper), on letters and digits."""
    # The whole quotation must be there (pieces joined by "…" each on their own): a quote that is right at its start
    # and drifts later is not a copy, and every quotation in the report is promised to be one.
    pieces = [alnum(p) for p in re.split(r"\.{3}|…", quote)]
    pieces = [p for p in pieces if len(p) >= MIN_QUOTE_CHARS]
    if not pieces:
        return False
    return any(all(piece in haystack for piece in pieces) for haystack in (own, paper))


_FIGURE_REF = re.compile(r"\b(Fig(?:ure)?s?\.?|Table|Scheme)\s*(\d+)", re.IGNORECASE)
_CAPTION_START = re.compile(r"^\s*(?:Fig(?:ure)?s?\.?|Table|Scheme|Chart)\s*S?\d+[A-Za-z]?\s*[:.|]", re.IGNORECASE)
_PREFIX = 40  # letters and digits of a quote's start that two quotes of one passage share even when one is cut short


_LABEL_STOP = frozenset("the and for with not are was that this from into per or of in is no a an to".split())


def _topic_words(text: str) -> set[str]:
    """What a label or a problem is about: its words of three letters or more, minus small words, cut to five letters."""
    return {w[:5] for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _LABEL_STOP}


def _figure_keys(text: str) -> set[tuple[str, str]]:
    return {("fig" if w.lower().startswith("fig") else w.lower(), n) for w, n in _FIGURE_REF.findall(text)}


def _repeats_check(problem: str, quote: str, checks: list[dict[str, Any]]) -> bool:
    """A finding the checklist already shows: the same passage, the same figure, or the same quoted word."""
    mine = alnum(quote)
    own_figures = _figure_keys(problem)
    for check in checks:
        if check.get("status") not in ("missing", "issue"):
            continue
        theirs = alnum(str(check.get("quote") or ""))
        if len(theirs) >= MIN_QUOTE_CHARS and (
            mine in theirs or theirs in mine or (len(mine) >= _PREFIX and mine[:_PREFIX] == theirs[:_PREFIX])
        ):
            return True
        label = str(check.get("label") or "")
        if own_figures and own_figures & _figure_keys(label):  # "Fig. 1 is never referred to" said again
            return True
        label_words = _topic_words(label)
        if len(label_words) >= 2 and len(label_words & _topic_words(problem)) >= 2:
            return True  # "Data / code availability" said again, whatever sentence the model quoted for it
        term = re.search(r"“([^”]{4,})”", label)  # "Strong claim: “dramatically improve”" said again
        if term and term.group(1).lower() in f"{problem} {quote}".lower():
            return True
    return False


def _checked(entry: dict[str, Any], own: str, paper: str, checks: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    """The section's summary, strengths and findings that survive the checks, and how many were removed."""
    removed = 0
    strengths: list[dict[str, str]] = []
    for item in entry.get("strengths") or []:
        if not isinstance(item, dict):
            continue
        point, quote = _text(item.get("point")), _text(item.get("quote"), 400)
        if len(point) >= 10 and _found(quote, own, paper) and not _CAPTION_START.match(quote):  # a caption is not prose
            strengths.append({"point": point, "quote": quote})
        else:
            removed += 1
    findings: list[dict[str, str]] = []
    for item in entry.get("findings") or []:
        if not isinstance(item, dict):
            continue
        problem, quote, fix = _text(item.get("problem")), _text(item.get("quote"), 400), _text(item.get("fix"))
        severity = str(item.get("severity") or "").strip().lower()
        severity = severity if severity in SEVERITIES else "minor"
        if (
            len(problem) < 20 or len(fix) < 15 or not _found(quote, own, paper)
            or not (_stems(fix) & _stems(f"{problem} {quote}"))
            or _repeats_check(problem, quote, checks)
        ):
            removed += 1
            continue
        findings.append({"severity": severity, "problem": problem, "quote": quote, "fix": fix})
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))  # stable: the model's order within a severity
    summary = _text(entry.get("summary"), 600)
    return (
        {"summary": summary or None, "strengths": strengths[:MAX_STRENGTHS], "findings": findings[:MAX_FINDINGS]},
        removed,
    )


# --------------------------------------------------------------------------- the score, in code

START_SCORE = 85  # a typical section of a paper the journal accepts has minor weaknesses only
# The checklist is decided by code and is the same on every run, so it costs full weight. The model's findings
# are not: its severity differs between runs of one text (a "major" in one, nothing in the next), so they cost
# less and are capped; the evaluation (scripts/eval_metrics.py) measures how much the score still moves.
MAJOR_FINDING_COST = 7
MINOR_FINDING_COST = 2
MAJOR_CHECK_COST = 8
MINOR_CHECK_COST = 2
STRENGTH_BONUS = 2
MAX_STRENGTH_BONUS = 4
MAX_FINDING_COST = 14
MAX_CHECK_COST = 20
SCORE_FLOOR = 15
SCORE_CEILING = 95


def section_score(findings: list[dict[str, str]], strengths: list[dict[str, str]], checks: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """(score, the steps that make it). The same findings and checklist always give the same score."""
    basis = [f"Start at {START_SCORE}: a typical section of an accepted paper has only minor weaknesses."]
    finding_cost = 0
    for item in findings:
        cost = MAJOR_FINDING_COST if item["severity"] == "major" else MINOR_FINDING_COST
        finding_cost += cost
        basis.append(f"−{cost} {item['severity']} finding: {item['problem'][:90]}")
    if finding_cost > MAX_FINDING_COST:
        basis.append(f"Findings cost at most {MAX_FINDING_COST} in all.")
    check_cost = 0
    for check in checks:
        if check.get("status") not in ("missing", "issue"):
            continue
        cost = MAJOR_CHECK_COST if check.get("severity") == "major" else MINOR_CHECK_COST
        check_cost += cost
        basis.append(f"−{cost} checklist: {check.get('label', '')}")
    if check_cost > MAX_CHECK_COST:
        basis.append(f"The checklist costs at most {MAX_CHECK_COST} in all.")
    bonus = min(MAX_STRENGTH_BONUS, STRENGTH_BONUS * len(strengths))
    if bonus:
        basis.append(f"+{bonus} for {len(strengths)} strength(s) shown in the text.")
    score = START_SCORE - min(finding_cost, MAX_FINDING_COST) - min(check_cost, MAX_CHECK_COST) + bonus
    return max(SCORE_FLOOR, min(SCORE_CEILING, score)), basis


MAX_FIX_STEPS = 6


def build_fix_plan(findings: list[dict[str, str]], checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What to change in a section, most important first. Only verified material goes in: a review finding has
    a quotation found in the paper, a checklist item was decided by code. Major before minor; within a severity
    the review's findings (about content) come before the checklist's (about form)."""
    items: list[tuple[int, int, dict[str, Any]]] = []
    for order, f in enumerate(findings):
        items.append((0 if f["severity"] == "major" else 2, order, {
            "severity": f["severity"], "problem": f["problem"], "quote": f["quote"], "action": f["fix"], "source": "review",
        }))
    for order, c in enumerate(checks):
        if c.get("status") not in ("missing", "issue") or not c.get("fix"):
            continue
        severity = "major" if c.get("severity") == "major" else "minor"
        detail = str(c.get("detail") or "")
        items.append((1 if severity == "major" else 3, order, {
            "severity": severity, "problem": f"{c.get('label', '')}. {detail}".strip(". ") + ".",
            "quote": str(c.get("quote") or ""), "action": str(c["fix"]), "source": "checklist",
            "kind": "add" if c.get("status") == "missing" else "change",
        }))
    items.sort(key=lambda item: item[:2])
    return [{"step": number, **item[2]} for number, item in enumerate(items[:MAX_FIX_STEPS], start=1)]


def apply_review(analysis: AnalysisResult, review: SectionReview, ctx: AnalysisContext) -> AnalysisResult:
    """The report with each section's checked summary, strengths and findings, its score computed from them, and
    a fix plan. A review that did not run still gives each section a fix plan from its checklist."""
    payload = analysis.model_dump(mode="json")
    notes = list(payload.get("grounding_notes") or [])
    if review.note:
        notes.append(review.note)
    texts = section_full_texts(ctx, payload["sections"])
    removed = 0
    for name, section in payload["sections"].items():
        entry = review.sections.get(name)
        if entry is not None:
            checked, dropped = _checked(entry, alnum(texts.get(name, "")), ctx.corpus, section.get("checks") or [])
            section.update(checked)
            removed += dropped
            section["llm_score"] = section["score"]
            section["score"], section["score_basis"] = section_score(
                checked["findings"], checked["strengths"], section.get("checks") or []
            )
        plan = build_fix_plan(section.get("findings") or [], section.get("checks") or [])
        offered = alnum(section.get("tool_text") or "")
        for step in plan:  # a step about words outside the text Calibrate receives cannot be applied to it
            first_piece = alnum(re.split(r"\.{3}|…", step["quote"])[0])
            step["in_calibrate_text"] = not (offered and len(first_piece) >= MIN_QUOTE_CHARS and first_piece[:60] not in offered)
        section["fix_plan"] = plan
        if entry is not None or plan:
            # the model's one-line fix ("Emphasize the unique aspects ...") is replaced by the plan's first step
            section["fix"] = plan[0]["action"] if plan else None
            if entry is not None:
                section["issue"] = plan[0]["problem"] if plan else None
    if removed:
        notes.append(
            f"{removed} point(s) of the section review were left out: their quotation is not in the paper, their fix "
            "would fit any paper, or the checklist already shows them."
        )
    payload["grounding_notes"] = notes
    return AnalysisResult.model_validate(payload)
