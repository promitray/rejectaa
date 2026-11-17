"""The editor's verdict, letter and journal-fit reasoning, written from verified material only.

The main analysis writes its letter in the same answer as everything else, before any check has run, so it can
praise or criticise a section that is not there ("the Discussion and Conclusion section could ...") or say the
paper fits a journal for reasons it never states. Here the letter is written last, by one more model call that is
given only numbered facts that have already passed the checks:

    F  concerns: the report's verified concerns and the section review's findings (each with its quotation)
    S  strengths: the section review's strengths (each with its quotation)
    C  the paper's claims (claim ledger)          O  structure observations by code (main text / appendix)
    V  the publication context the file states    J  the target journal: how it decides, its standing, the
                                                     closest papers it has published

Every sentence it writes must cite the ids it rests on, as JSON. Code then keeps a sentence only if:
- it cites ids that exist, and a critical sentence cites a concern (F) or an observation (O);
- every section and figure it names is in the verified outline (services.outline);
- every quotation is in the paper, and every number is in the paper or in the facts given.
The decision (send to review / revise first / desk reject) is made by code from the score and the concerns; the
model only words it, and a verdict that says otherwise is replaced by a plain one.

Never raises: if the letter cannot be written or too little of it survives, the main analysis's letter stays.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from models import AnalysisResult
from services import outline
from services.analysis_guard import AnalysisContext, _scrub_quotes
from services.journal_policy import SELECTIVE, SOUNDNESS, policy_for

logger = logging.getLogger(__name__)

LETTER_PROMPT_VERSION = "2026-11-13.3"  # .3: decision in plain words; publication context is not a merit
MAX_CONCERNS = 8
MAX_STRENGTHS = 4
MAX_QUOTE_WORDS = 25
MIN_LETTER_SENTENCES = 2
REVIEW, REVISE, DESK_REJECT = "send_to_review", "revise_first", "desk_reject"
REVISE_BELOW = 65  # an overall score below this, or two or more major concerns: revise before review
DESK_REJECT_BELOW = 40

_DECISION_WORDS = {
    REVIEW: ("send", "review"),
    REVISE: ("revis",),
    DESK_REJECT: ("desk reject", "desk-reject"),
}
_DECISION_TEXT = {
    REVIEW: "send it to review",
    REVISE: "return it to the authors for major revision before review",
    DESK_REJECT: "desk reject it",
}
_FALLBACK_VERDICT = {
    REVIEW: "I would send this to review.",
    REVISE: "I would return this for major revision before sending it to review.",
    DESK_REJECT: "I would desk reject this in its current form.",
}
# A sentence that finds fault with the paper. Contrast words alone ("however", "but") are not enough: a sentence
# that only sets context ("previous studies were limited ...") is not a criticism of this paper.
_CRITICAL = re.compile(
    r"\b(lacks?|lacking|missing|absent|not (?:report|state|provide|show|include|clear|justif|address|describ)\w*|"
    r"does not|do not|fails?|insufficient|unclear|weak|weakness|concern|unsupported|overclaim\w*|inconsistent|"
    r"unfair|questionable|undermin\w*|must (?:add|report|provide|address|state|clarify))\b",
    re.IGNORECASE,
)
_ID = re.compile(r"^[FSCOVJ]\d{1,2}$")

_SYSTEM = (
    "You are Professor Aldric Frowns, a demanding but fair handling editor. You write a short decision letter to "
    "the authors. You use ONLY the numbered facts given, and every sentence cites the ids of the facts it rests on. "
    "You never mention a section, figure, number or paper that is not in the facts. Return ONLY valid JSON."
)

_RULES = """Write:
- "verdict": ONE sentence in the first person that states the DECISION given below and its main reason. Cite ids.
- "letter": 4-7 sentences to the authors, in order: what the paper sets out to show (C ids), what is strong
  (S ids), the most important concerns, most serious first (F ids; say where: the section heading given with each
  fact, and "Appendix X" for appendix material), structural observations if they matter (O ids), and what would
  change the decision. Specific to this paper: name its datasets, models, compounds or values as the facts give
  them. No generic advice ("improve clarity", "expand the discussion").
- "journal_fit": 2-4 sentences on fit with the target journal: how the journal decides (J ids), how this paper's
  topic relates to what the journal publishes (J ids for its closest papers, C ids), and the publication context
  the file states (V ids) if there is one: say plainly that the work was already presented or posted there and
  that the authors must check the journal's policy on prior presentation and preprints. It is not a merit and not
  evidence of relevance. Do not claim knowledge of the journal beyond the J facts.
Each sentence is {"text": "...", "refs": ["F1", "C2"]}. A sentence that criticises must cite an F, O or V id.
Quote the paper only with words given in the facts.

Return {"verdict": {"text": "...", "refs": [...]}, "letter": [{"text": "...", "refs": [...]}],
"journal_fit": [{"text": "...", "refs": [...]}]}"""


# --------------------------------------------------------------------------- the facts

def _short(text: str, words: int = MAX_QUOTE_WORDS) -> str:
    parts = re.sub(r"\s+", " ", text or "").strip().split(" ")
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


def _where(ctx: AnalysisContext, section: str) -> str:
    for e in outline.entries(ctx):
        if e.key == section:
            return e.display + (" [appendix]" if e.part == "appendix" else "")
    return section


def decision(analysis: AnalysisResult) -> str:
    """The editor's decision, by code: from the verified concerns and the overall score."""
    severities = [str(getattr(r.severity, "value", r.severity)) for r in analysis.rejection_reasons]
    if "desk_reject" in severities or analysis.mood_score < DESK_REJECT_BELOW:
        return DESK_REJECT
    if analysis.mood_score < REVISE_BELOW or severities.count("major") >= 2:
        return REVISE
    return REVIEW


def facts(analysis: AnalysisResult, ctx: AnalysisContext, journal_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The numbered facts the letter may use, each already verified."""
    out: dict[str, dict[str, Any]] = {}
    n = 0
    for reason in analysis.rejection_reasons:
        if n >= MAX_CONCERNS:
            break
        n += 1
        out[f"F{n}"] = {
            "kind": "concern", "severity": str(getattr(reason.severity, "value", reason.severity)),
            "where": _where(ctx, reason.section) if reason.section else (reason.location or ""),
            "problem": reason.problem or reason.reason, "quote": _short(reason.evidence),
        }
    seen = {re.sub(r"\W+", "", (f["problem"] or "").lower())[:60] for f in out.values()}
    for name, section in analysis.sections.items():
        for finding in section.findings:
            key = re.sub(r"\W+", "", finding.problem.lower())[:60]
            if n >= MAX_CONCERNS or key in seen:
                continue
            seen.add(key)
            n += 1
            out[f"F{n}"] = {
                "kind": "concern", "severity": finding.severity, "where": _where(ctx, name),
                "problem": finding.problem, "quote": _short(finding.quote),
            }
    s = 0
    for name, section in analysis.sections.items():
        for strength in section.strengths:
            if s >= MAX_STRENGTHS:
                break
            s += 1
            out[f"S{s}"] = {"kind": "strength", "where": _where(ctx, name), "point": strength.point, "quote": _short(strength.quote)}
    ledger = analysis.evidence_ledger
    for claim in (ledger.claims if ledger else [])[:6]:
        out[claim.id] = {"kind": "claim", "main": claim.main, "statement": claim.statement}
    for i, note in enumerate(outline.structure_notes(ctx, analysis.paper_format), start=1):
        out[f"O{i}"] = {"kind": "observation", "text": note}
    for i, statement in enumerate(ctx.venue_statements, start=1):
        out[f"V{i}"] = {"kind": "publication context stated in the file", "text": statement}
    j = 0
    policy = policy_for(ctx.journal)
    if policy.basis:
        j += 1
        out[f"J{j}"] = {"kind": "how the journal decides", "text": policy.basis}
    from services.llm.base import journal_standing

    standing = journal_standing(journal_data or {})
    if standing:
        j += 1
        out[f"J{j}"] = {"kind": "journal standing", "text": standing}
    similar = [p for p in (journal_data or {}).get("similar_papers") or [] if isinstance(p, dict) and p.get("title")]
    if similar:
        j += 1
        out[f"J{j}"] = {
            "kind": "the journal's published papers closest to this one",
            "papers": [f"{p['title']} ({p.get('year') or 'n.d.'})" for p in similar[:4]],
        }
    elif journal_data and journal_data.get("found"):
        j += 1
        out[f"J{j}"] = {"kind": "closest papers", "text": "No closely related paper was found among the journal's articles."}
    return out


# --------------------------------------------------------------------------- checking what was written

def _numbers_ok(text: str, ctx: AnalysisContext, facts_text: str) -> bool:
    allowed = ctx.numbers | set(re.findall(r"\d+(?:\.\d+)?", facts_text))
    return all(num in allowed for num in re.findall(r"\d+(?:\.\d+)?", text))


def _checked_units(raw: Any, given: dict[str, dict[str, Any]], ctx: AnalysisContext, facts_text: str, dropped: list[str]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        refs = [r for r in (item.get("refs") or []) if isinstance(r, str) and _ID.match(r) and r in given]
        if not text:
            continue
        why = ""
        critical = bool(_CRITICAL.search(text))
        if not refs:
            why = "cites no fact"
        elif critical and not any(r[0] in "FOV" for r in refs):
            why = "criticises without citing a concern"
        elif not _numbers_ok(text, ctx, facts_text):
            why = "has a number that is not in the paper"
        else:
            checked, gone = outline.check_prose(text, ctx)
            if gone or not checked:
                why = "names a section or figure the paper does not have"
            else:
                counter = [0]
                text = _scrub_quotes(checked, ctx, counter)
                if counter[0]:
                    why = "quotes words that are not in the paper"
        if why:
            dropped.append(f"{why}: {text[:140]}")
            continue
        units.append({"text": text, "refs": refs})
    return units


def _verdict(raw: Any, chosen: str, given: dict[str, dict[str, Any]], ctx: AnalysisContext, facts_text: str, dropped: list[str]) -> dict[str, Any]:
    units = _checked_units([raw] if isinstance(raw, dict) else [], given, ctx, facts_text, dropped)
    if units:
        text = units[0]["text"].lower()
        says = {d for d, words in _DECISION_WORDS.items() if any(w in text for w in words)}
        # "send ... to review" also matches a revise verdict ("revise before sending it to review")
        agrees = chosen in says and (chosen != REVIEW or not (says & {REVISE, DESK_REJECT}))
        if agrees:
            return units[0]
        dropped.append(f"verdict did not state the decision ({chosen}): {units[0]['text'][:120]}")
    top = next((f for f in given.values() if f["kind"] == "concern" and f.get("severity") in ("desk_reject", "major")), None)
    text = _FALLBACK_VERDICT[chosen]
    if top and chosen != REVIEW:
        text = f"{text[:-1]}: {top['problem'][0].lower()}{top['problem'][1:].rstrip('.')}."
    return {"text": text, "refs": [k for k, v in given.items() if v is top][:1]}


async def rewrite(analysis: AnalysisResult, ctx: AnalysisContext, journal_data: dict[str, Any], provider: Any) -> AnalysisResult:
    """The report with its verdict, letter and journal fit written from verified facts. Never raises."""
    pub = ctx.publication
    if pub is not None and pub.status in ("retracted", "expression_of_concern"):
        return analysis  # the publication record decides; its verdict is written by code
    if not hasattr(provider, "generate"):
        return analysis
    given = facts(analysis, ctx, journal_data)
    chosen = decision(analysis)
    facts_text = json.dumps(given, ensure_ascii=False)
    policy = policy_for(ctx.journal)
    tier = {SOUNDNESS: "soundness-only journal", SELECTIVE: "selective journal"}.get(policy.tier, "specialist journal")
    prompt = (
        f"{_RULES}\n\nTARGET JOURNAL: {ctx.journal or 'not given'} ({tier})\n"
        f"DECISION (made by the editor's rules; word it, do not change it): {_DECISION_TEXT[chosen]}\n"
        f"OVERALL SCORE: {analysis.mood_score}/100\n\nFACTS:\n{json.dumps(given, ensure_ascii=False, indent=1)}"
    )
    try:
        raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0)
        raw = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
    except Exception:  # noqa: BLE001 - the main analysis's letter stays
        logger.warning("Editor letter call failed; keeping the main analysis's letter", exc_info=True)
        return analysis
    if not isinstance(raw, dict):
        return analysis
    dropped: list[str] = []
    letter = _checked_units(raw.get("letter"), given, ctx, facts_text, dropped)
    fit = _checked_units(raw.get("journal_fit"), given, ctx, facts_text, dropped)
    verdict = _verdict(raw.get("verdict"), chosen, given, ctx, facts_text, dropped)
    payload = analysis.model_dump(mode="json")
    notes = list(payload.get("grounding_notes") or [])
    if len(letter) >= MIN_LETTER_SENTENCES:
        payload["oracle_letter"] = " ".join(u["text"] for u in letter)
        payload["letter_points"] = [
            {**u, "kind": "letter", "facts": {r: given[r] for r in u["refs"]}} for u in letter
        ] + [{**u, "kind": "journal_fit", "facts": {r: given[r] for r in u["refs"]}} for u in fit]
        if fit:
            payload["journal_fit"] = " ".join(u["text"] for u in fit)
    else:
        dropped.append("too little of the letter could be verified; the first letter was kept")
    payload["verdict"] = verdict["text"]
    if dropped:
        notes.append(f"Editor's letter: {len(dropped)} sentence(s) were left out because they could not be verified "
                     f"({dropped[0][:120]}).")
    payload["grounding_notes"] = notes
    return AnalysisResult.model_validate(payload)


def attach_context(analysis: AnalysisResult, ctx: AnalysisContext) -> AnalysisResult:
    """The verified outline, what code observed about it, the publication context and the journal's policy,
    for the report to show (they are facts, not the model's words)."""
    payload = analysis.model_dump(mode="json")
    payload["outline"] = [
        {"key": e.key, "title": e.title, "label": e.label, "part": e.part, "page": e.page, "role": e.role, "display": e.display,
         "children": [s for s in ctx.subsections if s.get("parent") == e.key]}
        for e in outline.entries(ctx)
    ]
    payload["structure_notes"] = outline.structure_notes(ctx, analysis.paper_format)
    payload["venue_statements"] = list(ctx.venue_statements)
    policy = policy_for(ctx.journal)
    payload["journal_policy"] = {"journal": policy.journal, "tier": policy.tier, "basis": policy.basis} if ctx.journal else None
    return AnalysisResult.model_validate(payload)
