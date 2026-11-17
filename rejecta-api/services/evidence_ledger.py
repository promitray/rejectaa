"""The evidence-ledger review: specific questions per section, answered with sentence ids and checked in code.

    paper ─► index (ids) ─┬─► claim ledger (1 call) ─► audit plan (code: claims × rubric packs)
                          └─► search index (keywords + embeddings)            │
                                                                              ▼
      per section, in parallel: section text + its questions + passages from other sections ─► answers (ids)
                                                                              │
          "not reported" ─► absence check (search the whole paper, 1 call) ─┐ │ "problem" ─► author defence (1 call)
                                                                            ▼ ▼
                       specificity gate (code) ─► severity from the rubric (code) ─► section findings

What keeps it specific: every question comes from a claim of this paper and a check of its field, every finding
cites the paper's sentences by id (code copies the words), and a fix must name something concrete (a value, a
figure or table, a named test, a term of this paper) or it is replaced by the check's own fix or dropped.

What keeps it from inventing: ids that were not shown are discarded; a finding without valid evidence is dropped;
"the paper does not report X" is searched for in the whole paper and asked again with what was found; every
criticism is put to an author's advocate with the passages most likely to answer it, and goes if they do.

The result has the section review's shape (services.section_review.SectionReview), so scoring, the fix plan and
the frontend work unchanged. Never raises: the caller falls back to the older section review.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from services.analysis_guard import _stems, AnalysisContext
from services.claim_ledger import LEDGER_PROMPT_VERSION, Ledger, build_ledger, ledger_lines
from services.local_store import digest, get_store
from services.paper_index import PaperIndex, build_index, ids_in, sort_key
from services.retrieval import PaperSearch, build_search, embed_texts
from services.absence_audit import _without_ids as without_ids
from services.absence_audit import asserts_absence, kind_of
from services.journal_policy import policy_for
from services.rubrics import Claim, Question, build_plan, field_from_journal, rubric_version

logger = logging.getLogger(__name__)

INVESTIGATION_PROMPT_VERSION = "2026-11-14.1"  # .14.1: statements of absence go to the whole-paper audit
EVIDENCE_LEDGER_VERSION = f"{LEDGER_PROMPT_VERSION}|{INVESTIGATION_PROMPT_VERSION}|{rubric_version()}"

MAX_SECTION_CHARS = 28_000  # numbered text of one section per call
PASSAGES_PER_QUESTION = 3
MAX_PASSAGES = 12  # paragraphs from other sections shown in one call
MAX_FREE_FINDINGS = 3
MAX_STRENGTHS = 2
CHECK_PASSAGES = 4  # paragraphs the absence check and the defence see per finding
CONCURRENT_CALLS = 4
LEDGER_BUDGET_SECONDS = 200
MIN_PROBLEM_CHARS = 30
MIN_FIX_CHARS = 20
STATUSES = ("supported", "problem", "not_reported", "cant_tell")
SAME_POINT_OVERLAP = 0.5  # share of content words two problems about the same sentences have in common

# Advice that fits any paper. Allowed only when the same sentence names something concrete.
_GENERIC = re.compile(
    r"\b(more (?:details?|information|clarity|thorough|comprehensive|rigorous)|further (?:details?|research|studies|work|"
    r"investigation|analysis)|consider (?:adding|including|providing|improving|expanding|discussing)|could be "
    r"(?:clarified|improved|expanded|strengthened|elaborated)|improve (?:the )?(?:clarity|readability|presentation|"
    r"flow)|expand (?:the |on )?(?:discussion|section)|elaborate (?:on|further)|be more specific|provide more context)\b",
    re.IGNORECASE,
)
_CONCRETE = re.compile(r"\d|\b(?:Fig(?:ure)?s?\.?|Tables?|Schemes?|Eqs?\.?|Equations?|Sections?|SI|Supporting Information)\b")

_SYSTEM = (
    "You are a senior reviewer for {journal}. You answer specific questions about one section of a manuscript and "
    "cite the ids of the sentences your answer rests on. You are fair: when the paper does what is asked, you say so. "
    "You never cite an id that is not shown, never invent content, and never give advice that would fit any paper. "
    "Return ONLY valid JSON."
)

_RULES = """ANSWER EACH QUESTION under QUESTIONS:
- "status": "supported" (the paper does ALL of what the question asks; cite where), "problem" (it does it only
  in part, unevenly, or in a way that is wrong or not enough; cite the sentences that show it), "not_reported"
  (neither the section nor the passages say it; cite the sentence where it belongs, if there is one),
  "cant_tell" (the text given cannot answer it).
- Check every part of the question against the cited sentences. If any part fails (for example, one group of
  models got a different budget, one dataset is treated differently, a value lacks its spread), the answer is
  "problem", and the problem names that part.
- When the paper is silent about how something was done, say that it is not stated. Never state as a fact what
  the paper does not say (not "X was done before splitting" but "the paper does not state whether X was done
  on the training data only").
- The Supporting Information and appendices are not provided: if the paper says the information is there,
  answer "supported", and do not raise a finding about it.
- "evidence": ids of sentences shown above (the section or the passages), e.g. ["P12.3"]. Never other ids.
- For "problem" and "not_reported" also give:
  "problem": what exactly is wrong or missing, naming this paper's own content (its datasets, models, compounds,
             values, figures), in one or two sentences;
  "impact": what it means for the claim, in one sentence;
  "fix": the concrete action: what to add or change and where (a table with named columns, a named test, a
         value with its unit, a figure, a sentence in a named section). Not "add more details".

THEN up to 3 more "findings": problems in THIS section that the questions do not cover and that a reviewer for the
target journal would raise (not about material placed in the appendix or Supporting Information). Each with "claim" (a claim id or null), "severity" ("major" only if it undermines a
main claim; otherwise "minor"), "evidence" (ids in this section), "problem", "impact", "fix" as above. A good
section gets none. Do not pad.

THEN "summary": two sentences about THIS section, naming its content: what it does, and its main weakness or why it
is strong. And up to 2 "strengths", each a point a reviewer would praise with "evidence" (ids in this section).

Return {"answers": [{"q": "Q1", "status": "...", "evidence": [...], "problem": "...", "impact": "...", "fix": "..."}],
"findings": [{"claim": null, "severity": "minor", "evidence": [...], "problem": "...", "impact": "...", "fix": "..."}],
"summary": "...", "strengths": [{"point": "...", "evidence": [...]}]}
"""

_DEFENCE_SYSTEM = (
    "You act for the authors of a manuscript and then decide as an impartial editor. You read only the passages given. "
    "Return ONLY valid JSON."
)
_DEFENCE_RULES = """For each criticism: first, as the authors' advocate, look in the passages for text that answers
it (the value, control, comparison or statement the criticism says is missing or wrong). Then, as an impartial
editor, decide:
- "resolved": the passages explicitly answer the whole criticism (cite the ids that do);
- "partly": they answer part of it (cite the ids), and give "corrected": the criticism restated in one or two
  sentences so that it claims ONLY what the passages do not answer (e.g. "trials are stated, but GNNs get 30
  where other models get 50");
- "stands": they do not answer it.
A criticism of how something was done is not answered by text that only says it was done. A citation of the
paper a method comes from is its source.
Return {"items": [{"id": "D1", "verdict": "stands", "evidence": [], "corrected": "", "note": "one sentence"}]}"""


@dataclass
class SectionPart:
    """One section as the ledger review sees it."""

    name: str
    title: str
    role: str
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Outcome:
    """What the ledger review produced: per-section entries (the section review's shape) and its account."""

    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    status: str = "not_run"
    note: str = ""


# --------------------------------------------------------------------------- helpers

def _text(value: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("\"“”")[:limit]


def _parse(provider: Any, raw: str) -> dict[str, Any]:
    parsed = provider.parse_json_response(raw) if hasattr(provider, "parse_json_response") else json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _allowed(index: PaperIndex, section: str, passages: list[str]) -> set[str]:
    out = {sid for pid in index.sections.get(section, []) for sid in index.paragraphs[pid].sentence_ids}
    for pid in passages:
        if pid in index.paragraphs:
            out.update(index.paragraphs[pid].sentence_ids)
    return out


def _evidence(value: Any, allowed: set[str]) -> list[str]:
    return sorted(dict.fromkeys(i for i in ids_in(value) if i in allowed), key=sort_key)


def specific(problem: str, fix: str, grounding: str) -> tuple[bool, bool]:
    """(the problem names this paper's content, the fix is concrete). `grounding`: the evidence, the claim, the
    question and the paper's facts: a problem or fix sharing no content word with them is about no paper in particular."""
    anchor = _stems(grounding)
    problem_ok = len(problem) >= MIN_PROBLEM_CHARS and bool(_stems(problem) & anchor)
    concrete = bool(_CONCRETE.search(fix)) or bool(_stems(fix) & anchor)
    fix_ok = len(fix) >= MIN_FIX_CHARS and concrete and not (_GENERIC.search(fix) and not _CONCRETE.search(fix))
    return problem_ok, fix_ok


# --------------------------------------------------------------------------- the per-section investigation

def _section_prompt(
    part: SectionPart, questions: list[Question], passages: list[str], index: PaperIndex, ledger: Ledger, journal: str
) -> str:
    lines = [
        f"TARGET JOURNAL: {journal or 'not given'}",
        ledger_lines(ledger),
        "",
        f'SECTION UNDER REVIEW: "{part.title or part.name}" (role: {part.role})',
        index.numbered(part.name, MAX_SECTION_CHARS),
    ]
    if passages:
        lines += ["", "PASSAGES FROM OTHER SECTIONS (for the questions):", index.numbered_paragraphs(passages)]
    found = [c for c in part.checks if c.get("status") in ("missing", "issue")]
    if found:
        lines += ["", "ALREADY FOUND BY CODE (shown to the author already; do not repeat):"]
        lines += [f"- {c.get('label', '')}" + (f": {c.get('detail')}" if c.get("detail") else "") for c in found]
    lines += ["", "QUESTIONS:"]
    if questions:
        for q in questions:
            about = f" (about {q.claim.id})" if q.claim else ""
            lines.append(f"{q.id}{about}: {q.text}")
    else:
        lines.append("(none for this section: give only findings, summary and strengths)")
    lines += ["", _RULES]
    return "\n".join(lines)


@dataclass
class _Candidate:
    section: str
    finding: dict[str, Any]
    question: Question | None
    claim: Claim | None
    grounding: str


MAX_REMOVED_LOG = 20


def _log_removed(log: list[dict[str, Any]], cand_or_finding: Any, reason: str, detail: str = "", section: str = "") -> None:
    """What was taken out and why: shown with the report so a removed finding can be checked by a person."""
    finding = cand_or_finding.finding if isinstance(cand_or_finding, _Candidate) else cand_or_finding
    log.append({
        "section": cand_or_finding.section if isinstance(cand_or_finding, _Candidate) else section,
        "check": finding.get("check"), "problem": str(finding.get("problem") or "")[:300], "reason": reason,
        "detail": detail[:300],
    })


_CLAIM_IDS = re.compile(r"\s*\((?:C\d+(?:\s*(?:,|and)\s*)?)+\)|\b(claims?)\s+C\d+(?:\s*(?:,|and)\s*C\d+)*")


def without_claim_ids(text: str) -> str:
    """Model text without our claim numbering ("the main claims C1 and C2" -> "the main claims")."""
    return re.sub(r"\s+([.,;:])", r"\1", _CLAIM_IDS.sub(lambda m: m.group(1) or "", text or "")).strip()


def _claims_by_id(ledger: Ledger) -> dict[str, Claim]:
    return {c.id: c for c in ledger.claims}


def _read_answers(
    raw: dict[str, Any], part: SectionPart, questions: list[Question], passages: list[str], index: PaperIndex,
    ledger: Ledger, facts_text: str, counts: dict[str, int], log: list[dict[str, Any]],
    checked: list[dict[str, Any]] | None = None,
) -> tuple[list[_Candidate], dict[str, Any]]:
    checked = checked if checked is not None else []
    allowed = _allowed(index, part.name, passages)
    own = _allowed(index, part.name, [])
    by_q = {q.id: q for q in questions}
    claims = _claims_by_id(ledger)
    candidates: list[_Candidate] = []
    answered: set[str] = set()
    for answer in raw.get("answers") or []:
        if not isinstance(answer, dict):
            continue
        q = by_q.get(str(answer.get("q") or "").strip())
        status = str(answer.get("status") or "").strip().lower()
        if q is None or q.id in answered or status not in STATUSES:
            continue
        answered.add(q.id)
        counts[status] = counts.get(status, 0) + 1
        checked.append({
            "q": q.id, "section": part.name, "check": q.item.id, "source": q.item.source, "question": q.text,
            "claim_id": q.claim.id if q.claim else None, "status": status,
            "evidence_ids": _evidence(answer.get("evidence"), allowed),
        })
        if status in ("supported", "cant_tell"):
            continue
        evidence = _evidence(answer.get("evidence"), allowed)
        if status == "problem" and not evidence:
            counts["no_evidence"] = counts.get("no_evidence", 0) + 1
            _log_removed(log, {"check": q.item.id, "problem": answer.get("problem")}, "no valid evidence", section=part.name)
            continue
        claim = q.claim
        grounding = " ".join([index.text_of(evidence), claim.statement if claim else "", q.text, facts_text])
        finding = {
            "severity": q.severity(status), "problem": without_claim_ids(_text(answer.get("problem"))),
            "impact": without_claim_ids(_text(answer.get("impact"), 300)), "fix": without_claim_ids(_text(answer.get("fix"))), "quote": index.text_of(evidence)[:1200] if evidence else "",
            "evidence_ids": evidence, "claim_id": claim.id if claim else None, "claim": claim.statement if claim else None,
            "check": q.item.id, "source": q.item.source, "origin": "rubric", "status": status, "question": q.text,
            "kind": "add" if status == "not_reported" else "change", "verified_by": "ledger",
        }
        candidates.append(_Candidate(part.name, finding, q, claim, grounding))
    for item in (raw.get("findings") or [])[:MAX_FREE_FINDINGS]:
        if not isinstance(item, dict):
            continue
        evidence = _evidence(item.get("evidence"), allowed)
        if not evidence:
            counts["no_evidence"] = counts.get("no_evidence", 0) + 1
            _log_removed(log, {"problem": item.get("problem")}, "no valid evidence", section=part.name)
            continue
        claim = claims.get(str(item.get("claim") or "").strip())
        severity = "major" if str(item.get("severity") or "").lower() == "major" and claim is not None and claim.main else "minor"
        grounding = " ".join([index.text_of(evidence), claim.statement if claim else "", facts_text])
        finding = {
            "severity": severity, "problem": without_claim_ids(_text(item.get("problem"))),
            "impact": without_claim_ids(_text(item.get("impact"), 300)), "fix": without_claim_ids(_text(item.get("fix"))), "quote": index.text_of(evidence)[:1200], "evidence_ids": evidence,
            "claim_id": claim.id if claim else None, "claim": claim.statement if claim else None, "check": None,
            "source": "Section review", "origin": "review", "status": "problem", "question": None, "kind": "change",
            "verified_by": "ledger",
        }
        candidates.append(_Candidate(part.name, finding, None, claim, grounding))
    strengths = []
    for item in raw.get("strengths") or []:
        if not isinstance(item, dict) or len(strengths) >= MAX_STRENGTHS:
            continue
        evidence = _evidence(item.get("evidence"), own)
        point = _text(item.get("point"), 300)
        if evidence and len(point) >= 10:
            strengths.append({"point": point, "quote": index.text_of(evidence)[:600], "evidence_ids": evidence})
    summary = without_claim_ids(_text(raw.get("summary")))
    section_text = " ".join(index.paragraphs[p].text for p in index.sections.get(part.name, []))
    if summary and not (_stems(summary) & _stems(section_text)):
        summary = ""  # a summary that names nothing in the section is a template
    return candidates, {"summary": summary or None, "strengths": strengths}


# --------------------------------------------------------------------------- absence check and defence

async def _defence(
    candidates: list[_Candidate], index: PaperIndex, search: PaperSearch, provider: Any, counts: dict[str, int],
    log: list[dict[str, Any]],
) -> list[_Candidate]:
    """Every 'problem' put to the authors' advocate with the passages most likely to answer it."""
    problems = [c for c in candidates if c.finding["status"] == "problem"]
    if not problems:
        return candidates
    queries = [f"{c.finding['problem']} {c.claim.statement if c.claim else ''}" for c in problems]
    vectors = await embed_texts(provider, queries)
    items: list[tuple[str, _Candidate, list[str]]] = []
    blocks: list[str] = []
    for number, (cand, query, vector) in enumerate(zip(problems, queries, vectors), start=1):
        own = sorted({i.split(".")[0] for i in cand.finding["evidence_ids"]}, key=sort_key)
        passages = list(dict.fromkeys(own + search.search(query, k=CHECK_PASSAGES, query_vector=vector)))[: CHECK_PASSAGES + 2]
        item_id = f"D{number}"
        items.append((item_id, cand, passages))
        blocks.append(
            f"### {item_id}\nCRITICISM: {cand.finding['problem']}\nCITED: {cand.finding['quote'][:500]}\n"
            f"PASSAGES:\n{index.numbered_paragraphs(passages)}"
        )
    try:
        raw = _parse(provider, await provider.generate(_DEFENCE_SYSTEM, f"{_DEFENCE_RULES}\n\n" + "\n\n".join(blocks), temperature=0.0))
    except Exception:  # noqa: BLE001
        logger.warning("Author-defence check failed; keeping the findings", exc_info=True)
        return candidates
    verdicts = {str(v.get("id")): v for v in raw.get("items") or [] if isinstance(v, dict)}
    dropped: set[int] = set()
    for item_id, cand, passages in items:
        verdict = verdicts.get(item_id) or {}
        allowed = {s for p in passages for s in index.paragraphs[p].sentence_ids}
        found = _evidence(verdict.get("evidence"), allowed)
        decided = str(verdict.get("verdict") or "").lower()
        if decided == "resolved" and found:
            dropped.add(id(cand))
            counts["author_defence"] = counts.get("author_defence", 0) + 1
            _log_removed(log, cand, "answered by the paper", f"{', '.join(found)}: {_text(verdict.get('note'), 200)}")
        elif decided == "partly" and found:
            corrected = without_ids(_text(verdict.get("corrected")))
            if len(corrected) >= MIN_PROBLEM_CHARS and _stems(corrected) & _stems(cand.finding["problem"]):
                _log_removed(log, cand, "narrowed: the paper answers part of it", f"{', '.join(found)}: {corrected[:200]}")
                cand.finding["problem"] = corrected
                counts["narrowed"] = counts.get("narrowed", 0) + 1
    return [c for c in candidates if id(c) not in dropped]


def _gate(candidates: list[_Candidate], counts: dict[str, int], log: list[dict[str, Any]]) -> list[_Candidate]:
    """Only specific findings pass. A check's finding whose own fix is vague gets the check's fix instead."""
    out: list[_Candidate] = []
    for cand in candidates:
        f = cand.finding
        problem_ok, fix_ok = specific(f["problem"], f["fix"], cand.grounding)
        if problem_ok and not fix_ok and cand.question is not None:
            f["fix"] = cand.question.fix()
            fix_ok = True
        if not (problem_ok and fix_ok):
            counts["not_specific"] = counts.get("not_specific", 0) + 1
            _log_removed(log, cand, "not specific to this paper", "fix: " + f["fix"][:150])
            continue
        if f.get("origin") == "review" and not f.get("claim_id") and not _CONCRETE.search(f["problem"]):
            # a reviewer's own point that bears on no claim of the paper and names nothing concrete (a value, a
            # figure or table, a section) is an opinion about emphasis ("connect the descriptors to the claims")
            counts["not_specific"] = counts.get("not_specific", 0) + 1
            _log_removed(log, cand, "not tied to a claim or a concrete detail", f["problem"][:150])
            continue
        out.append(cand)
    return out


def _merge_overlaps(candidates: list[_Candidate], log: list[dict[str, Any]]) -> list[_Candidate]:
    """One point is said once per section. Merged: the same check on several claims (kept for its first claim);
    two checks on the same claim citing the same sentences (a missing significance test and missing P values for
    one comparison); two findings citing the same sentences whose problems share most of their words. A different
    point about the same sentences (a dataset count that does not match) stays."""
    out: list[_Candidate] = []
    for cand in sorted(candidates, key=_order):
        f = cand.finding
        duplicate = None
        for kept in out:
            k = kept.finding
            if kept.section != cand.section:
                continue
            same_check = bool(f.get("check")) and f.get("check") == k.get("check")
            both_checks = f.get("origin") == k.get("origin") == "rubric"
            same_claim = bool(f.get("claim_id")) and f.get("claim_id") == k.get("claim_id")
            shared = set(f["evidence_ids"]) & set(k["evidence_ids"])
            mine, theirs = _stems(f["problem"]), _stems(k["problem"])
            alike = bool(mine and theirs) and len(mine & theirs) / len(mine | theirs) >= SAME_POINT_OVERLAP
            # two statements that the same kind of thing is missing for the same claim ("no statistical test",
            # "no P values or confidence intervals"): one gap, said once
            same_gap = same_claim and asserts_absence(f) and asserts_absence(k) and (
                kind_of(f["problem"]).id == kind_of(k["problem"]).id != "other"
            )
            if same_check or (both_checks and same_claim and (shared or not f["evidence_ids"])) or (shared and alike) or same_gap:
                duplicate = kept
                break
        if duplicate is not None:
            _log_removed(log, cand, "same point as another finding", duplicate.finding["problem"][:200])
            continue
        out.append(cand)
    return out


def paragraph_pages(index: PaperIndex, page_texts: list[str], first_page: int) -> dict[str, int]:
    """The page each paragraph starts on, found by its first words in the file's pages (in order)."""
    if not page_texts:
        return {}
    from services.analysis_guard import alnum

    pages = [alnum(t) for t in page_texts]
    out: dict[str, int] = {}
    cursor = 0
    for pid in sorted(index.paragraphs, key=sort_key):
        key = alnum(index.paragraphs[pid].text)[:60]
        if len(key) < 20:
            continue
        for p in list(range(cursor, len(pages))) + list(range(0, cursor)):
            if key in pages[p]:
                out[pid] = p + first_page
                cursor = p
                break
    return out


# --------------------------------------------------------------------------- the whole review

def _order(cand: _Candidate) -> tuple[int, int, tuple[int, int]]:
    f = cand.finding
    first = sort_key(f["evidence_ids"][0]) if f["evidence_ids"] else (10**9, 0)
    return (0 if f["severity"] == "major" else 1, 0 if f["origin"] == "rubric" else 1, first)


async def review(
    parts: list[SectionPart], texts: dict[str, str], journal: str, provider: Any, ctx: AnalysisContext
) -> Outcome:
    """The evidence-ledger review of the given sections. Raises only on programming errors (the caller falls back)."""
    index = build_index(ctx.abstract, texts)
    if not index.sentences:
        return Outcome(status="failed", note="The section review found no text to read.")
    paper_key = digest(ctx.source_text or "\n".join(texts.values()))
    ledger, search = await asyncio.gather(build_ledger(index, provider, journal), build_search(index, provider, paper_key))
    roles = {p.name: p.role for p in parts}
    order = [p.name for p in parts]
    decided = {str(c.get("key")) for p in parts for c in p.checks if c.get("status") in ("met", "missing", "issue")}
    field_name = ledger.field
    if field_name in ("other", "chemistry_other", "cs_other"):
        field_name = field_from_journal(journal) or field_name
    paper_text = " ".join(texts.values())
    plan, packs = build_plan(
        ledger.claims, field_name, ledger.paper_kind, journal, roles, order, paper_text, decided, policy_for(journal).tier
    )

    vectors = await embed_texts(provider, [q.search_text for q in plan])
    for q, vector in zip(plan, vectors):
        q.passages = search.search(q.search_text, k=PASSAGES_PER_QUESTION, query_vector=vector, exclude_sections={q.section})
    facts_text = " ".join(f.value for f in ledger.facts)
    counts: dict[str, int] = {}
    log: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    gate = asyncio.Semaphore(CONCURRENT_CALLS)

    async def investigate(part: SectionPart) -> tuple[SectionPart, list[_Candidate], dict[str, Any]] | None:
        if part.name not in index.sections:
            return None
        questions = [q for q in plan if q.section == part.name]
        passages = list(dict.fromkeys(p for q in questions for p in q.passages))[:MAX_PASSAGES]
        prompt = _section_prompt(part, questions, passages, index, ledger, journal)
        async with gate:
            raw = _parse(provider, await provider.generate(_SYSTEM.format(journal=journal or "the target journal"), prompt, temperature=0.0))
        candidates, extra = _read_answers(raw, part, questions, passages, index, ledger, facts_text, counts, log, checked)
        return part, candidates, extra

    results = await asyncio.gather(*(investigate(p) for p in parts), return_exceptions=True)
    failed = [r for r in results if isinstance(r, BaseException)]
    for error in failed:
        logger.warning("A section investigation call failed", exc_info=error)
    done = [r for r in results if r is not None and not isinstance(r, BaseException)]
    candidates = [c for _, cands, _ in done for c in cands]
    # A finding that says something is missing is checked later against the whole paper, together with every other
    # such statement in the report (services.absence_audit). The others are put to the authors' advocate here.
    checked_missing = [c for c in candidates if asserts_absence(c.finding)]
    checked_problems = await _defence(
        [c for c in candidates if not asserts_absence(c.finding)], index, search, provider, counts, log
    )
    kept = _merge_overlaps(_gate(checked_missing + checked_problems, counts, log), log)
    pages = paragraph_pages(index, ctx.page_texts, ctx.first_page)
    for cand in kept:
        first = cand.finding["evidence_ids"][0].split(".")[0] if cand.finding["evidence_ids"] else None
        if first and pages.get(first):
            cand.finding["page"] = pages[first]

    sections: dict[str, dict[str, Any]] = {}
    for part, _, extra in done:
        mine = sorted((c for c in kept if c.section == part.name), key=_order)
        for strength in extra.get("strengths") or []:
            first = strength["evidence_ids"][0].split(".")[0] if strength.get("evidence_ids") else None
            if first and pages.get(first):
                strength["page"] = pages[first]
        sections[part.name] = {"name": part.name, **extra, "findings": [c.finding for c in mine]}

    claim_findings: dict[str, int] = {}
    for cand in kept:
        if cand.finding.get("claim_id"):
            claim_findings[cand.finding["claim_id"]] = claim_findings.get(cand.finding["claim_id"], 0) + 1
    summary = {
        "status": "done" if not failed and ledger.status == "done" else "partial",
        "field": field_name,
        "paper_kind": ledger.paper_kind,
        "packs": [{"name": p.name, "title": p.title, "version": p.version} for p in packs],
        "claims": [
            {"id": c.id, "statement": c.statement, "type": c.type, "main": c.main, "quote": index.text_of(c.evidence)[:600],
             "evidence_ids": list(c.evidence), "findings": claim_findings.get(c.id, 0)}
            for c in ledger.claims
        ],
        "questions": len(plan),
        "answers": {s: counts.get(s, 0) for s in STATUSES},
        "findings_kept": len(kept),
        "removed": {
            "without_evidence": counts.get("no_evidence", 0),
            "found_in_paper": counts.get("absence_check", 0),
            "answered_by_paper": counts.get("author_defence", 0),
            "not_specific": counts.get("not_specific", 0),
            "narrowed": counts.get("narrowed", 0),
        },
        # the checks run at the same time and log as they finish: sorted, so the same paper gives the same list
        "removed_findings": sorted(log, key=lambda r: (order.index(r["section"]) if r["section"] in order else len(order),
                                                      r["reason"], r["problem"]))[:MAX_REMOVED_LOG],
        "checks": sorted(checked, key=lambda c: int(c["q"][1:])),
        "ledger_entries_dropped": ledger.dropped,
        "retrieval": "keywords + embeddings" if search.vectors else "keywords",
        "store": get_store().name,
        "version": EVIDENCE_LEDGER_VERSION,
    }
    missing_sections = [p.name for p in parts if p.name not in sections and p.name in index.sections]
    note = ""
    if failed or missing_sections:
        note = "Part of the section-by-section review could not be completed this time" + (
            f" ({', '.join(missing_sections)})." if missing_sections else "."
        )
    if ledger.status != "done":
        note = (note + " " if note else "") + "The paper's claims could not be listed this time; the checks that need them were skipped."
    status = "done" if not note else ("partial" if sections else "failed")
    return Outcome(sections=sections, summary=summary, status=status, note=note)


async def review_with_budget(
    parts: list[SectionPart], texts: dict[str, str], journal: str, provider: Any, ctx: AnalysisContext
) -> Outcome:
    return await asyncio.wait_for(review(parts, texts, journal, provider, ctx), LEDGER_BUDGET_SECONDS)
