"""The rubric packs (knowledge/rubrics/*.yaml) and the audit plan they give for one paper.

A pack is a list of checks a careful reviewer of a field (or of a journal) makes. The plan is built by code, not
by the model: for each check that applies to the paper's field, kind and target journal, one question per
matching claim of the paper (or one about the whole paper), asked in the section where the answer belongs.

So the model is never asked "review this section" (which gives advice that fits any paper); it is asked, for
example, "For 'deep models are generally unable to outperform non-deep ones': did each compared model get the
same hyperparameter search?". The plan is a pure function of the paper's claims and the packs: the same paper
always gets the same questions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


RUBRIC_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "rubrics"
UNIVERSAL = "universal"

FIELDS = (
    "chemistry_synthesis", "chemistry_catalysis", "chemistry_computational", "chemistry_materials",
    "chemistry_analytical", "chemistry_other", "cs_ml", "cs_systems", "cs_theory", "cs_other",
    "biology", "medicine", "physics", "other",
)
KINDS = ("method", "benchmark", "application", "synthesis", "characterization", "theory", "dataset", "review", "other")
CLAIM_TYPES = ("novelty", "comparative", "causal", "mechanism", "generalization", "quantitative", "practical")
SEVERITIES = ("major", "minor")

MAX_CLAIMS_PER_ITEM = 2  # the same check on more than two claims repeats itself
MAX_QUESTIONS_PER_SECTION = 6
MAX_QUESTIONS = 24


@dataclass(frozen=True)
class RubricItem:
    id: str
    pack: str
    triggers_on: tuple[str, ...]
    ask_in: tuple[str, ...]
    question: str
    search: str
    severity_missing: str
    severity_problem: str
    fix: str
    source: str
    main_only: bool = False
    kinds: tuple[str, ...] = ()
    requires_terms: tuple[str, ...] = ()
    covered_by: tuple[str, ...] = ()
    not_for: tuple[str, ...] = ()  # journal tiers (services.journal_policy) the check does not apply to


@dataclass(frozen=True)
class Pack:
    name: str
    version: str
    title: str
    fields: tuple[str, ...]
    journals: tuple[str, ...]
    items: tuple[RubricItem, ...]


@dataclass(frozen=True)
class Claim:
    id: str
    statement: str
    type: str
    main: bool
    evidence: tuple[str, ...]  # sentence ids where the paper states it


@dataclass
class Question:
    id: str  # "Q1"...
    item: RubricItem
    section: str
    text: str
    claim: Claim | None = None
    passages: list[str] = field(default_factory=list)  # paragraph ids from other sections, filled by retrieval

    @property
    def search_text(self) -> str:
        return f"{self.claim.statement if self.claim else ''} {self.item.search}".strip()

    def severity(self, status: str) -> str:
        """The check's severity for this outcome. A major check on a claim that is not a main claim is minor."""
        value = self.item.severity_missing if status == "not_reported" else self.item.severity_problem
        if value == "major" and self.claim is not None and not self.claim.main:
            return "minor"
        return value if value in SEVERITIES else "minor"

    def fix(self) -> str:
        return _fill(self.item.fix, self.claim)


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _item(raw: dict[str, Any], pack: str) -> RubricItem:
    severity = raw.get("severity") or {}
    return RubricItem(
        id=str(raw["id"]), pack=pack, triggers_on=_tuple(raw.get("triggers_on")), ask_in=_tuple(raw.get("ask_in")),
        question=str(raw["question"]).strip(), search=str(raw.get("search") or ""),
        severity_missing=str(severity.get("missing") or "minor"), severity_problem=str(severity.get("problem") or "minor"),
        fix=str(raw.get("fix") or "").strip(), source=str(raw.get("source") or "Rejecta editorial rubric"),
        main_only=bool(raw.get("main_only")), kinds=_tuple(raw.get("kinds")),
        requires_terms=tuple(t.lower() for t in _tuple(raw.get("requires_terms"))), covered_by=_tuple(raw.get("covered_by")),
        not_for=_tuple(raw.get("not_for")),
    )


@lru_cache(maxsize=1)
def load_packs(directory: str | None = None) -> tuple[Pack, ...]:
    """Every pack in the rubric folder, the universal one first, the rest by name."""
    root = Path(directory) if directory else RUBRIC_DIR
    packs: list[Pack] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        applies = raw.get("applies_to") or {}
        name = str(raw.get("pack") or path.stem)
        packs.append(Pack(
            name=name, version=str(raw.get("version") or ""), title=str(raw.get("title") or name),
            fields=_tuple(applies.get("fields")), journals=_tuple(applies.get("journals")),
            items=tuple(_item(item, name) for item in raw.get("items") or []),
        ))
    packs.sort(key=lambda p: (p.name != UNIVERSAL, p.name))
    return tuple(packs)


def rubric_version() -> str:
    """Part of the report version: a changed pack must not serve a report made with the older one."""
    return ",".join(f"{p.name}@{p.version}" for p in load_packs())


def select_packs(field_name: str, journal: str) -> list[Pack]:
    """The packs for a paper of this field submitted to this journal."""
    journal_key = re.sub(r"\s+", " ", (journal or "").strip().lower())
    out: list[Pack] = []
    for pack in load_packs():
        if "any" in pack.fields or field_name in pack.fields:
            out.append(pack)
        elif pack.journals and any(re.search(pattern, journal_key) for pattern in pack.journals):
            out.append(pack)
    return out


def _fill(template: str, claim: Claim | None) -> str:
    statement = claim.statement if claim else "the main claim"
    return template.replace("{claim}", statement)


def _section_for(item: RubricItem, roles: dict[str, str], order: list[str]) -> str | None:
    """The first section (in reading order) whose role is the item's first preference that the paper has."""
    for role in item.ask_in:
        for name in order:
            if roles.get(name) == role:
                return name
    return None


def build_plan(
    claims: list[Claim],
    field_name: str,
    paper_kind: str,
    journal: str,
    roles: dict[str, str],
    order: list[str],
    paper_text: str,
    decided_checks: set[str],
    journal_tier: str = "standard",
) -> tuple[list[Question], list[Pack]]:
    """The questions for this paper, and the packs they come from.

    `decided_checks`: checklist keys code already decided (met, missing or issue) somewhere in the paper; an item
    covered by one of them is not asked again.
    """
    packs = select_packs(field_name, journal)
    text = paper_text.lower()
    ordered_claims = sorted(claims, key=lambda c: (not c.main, _claim_number(c.id)))
    candidates: list[tuple[tuple[int, int, int, int], Question]] = []
    for pack_rank, pack in enumerate(packs):
        for item_rank, item in enumerate(pack.items):
            if item.kinds and paper_kind not in item.kinds:
                continue
            if item.requires_terms and not any(term in text for term in item.requires_terms):
                continue
            if item.covered_by and decided_checks & set(item.covered_by):
                continue
            if journal_tier in item.not_for:
                continue  # e.g. "significance beyond the field" at a journal that reviews for soundness only
            section = _section_for(item, roles, order)
            if section is None:
                continue
            severity_rank = 0 if "major" in (item.severity_missing, item.severity_problem) else 1
            if "paper" in item.triggers_on:
                q = Question("", item, section, _fill(item.question, None))
                candidates.append(((severity_rank, 0, pack_rank, item_rank), q))
                continue
            matching = [c for c in ordered_claims if c.type in item.triggers_on and (c.main or not item.main_only)]
            for claim_rank, claim in enumerate(matching[:MAX_CLAIMS_PER_ITEM]):
                q = Question("", item, section, _fill(item.question, claim), claim)
                candidates.append(((severity_rank, claim_rank + (0 if claim.main else 5), pack_rank, item_rank), q))
    candidates.sort(key=lambda pair: pair[0])
    per_section: dict[str, int] = {}
    plan: list[Question] = []
    for _, question in candidates:
        if per_section.get(question.section, 0) >= MAX_QUESTIONS_PER_SECTION or len(plan) >= MAX_QUESTIONS:
            continue
        per_section[question.section] = per_section.get(question.section, 0) + 1
        plan.append(question)
    # ids in reading order of the sections, then plan order: stable for the same paper
    plan.sort(key=lambda q: order.index(q.section) if q.section in order else len(order))
    for number, question in enumerate(plan, start=1):
        question.id = f"Q{number}"
    return plan, packs


def _claim_number(claim_id: str) -> int:
    match = re.search(r"\d+", claim_id)
    return int(match.group(0)) if match else 10**6


def field_from_journal(journal: str) -> str | None:
    """A hint from the journal's name when the paper's own field is unclear."""
    name = (journal or "").lower()
    if re.search(r"machine intelligence|machine learning|neural|artificial intelligence|computational science|information and modeling|cheminformatics", name):
        return "cs_ml"
    if re.search(r"catalys", name):
        return "chemistry_catalysis"
    if re.search(r"organic|synthesis|synlett|org\. lett", name):
        return "chemistry_synthesis"
    if re.search(r"computational chemistry|theory and computation|chemical theory|quantum chem", name):
        return "chemistry_computational"
    if re.search(r"materials", name):
        return "chemistry_materials"
    return None

