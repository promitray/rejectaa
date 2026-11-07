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
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from services import paper_profile as pp

MAX_SECTIONS = 8
MIN_ANCHOR_CHARS = 20  # letters and digits, spaces and punctuation ignored (about four words)
ANCHOR_PREFIX_CHARS = 24
MAX_GROUNDING_NOTES = 8
MISSING_METHODS_PENALTY = 8  # research article with no Experimental/Methods section in the file
SI_PENALTY = 5  # most a missing Supporting Information can cost; it is the upload's limit, not a flaw
UNVERIFIED_QUOTE = "[unverified quote removed]"

# Mood bands, the same ones the prompt gives the model.
_MOOD_BANDS = ((20, "furious"), (40, "skeptical"), (55, "unimpressed"), (70, "neutral"), (80, "interested"), (100, "delighted"))


def mood_for_score(score: int) -> str:
    for upper, mood in _MOOD_BANDS:
        if score <= upper:
            return mood
    return "delighted"


def alnum(text: str) -> str:
    """Lowercase letters and digits only. Makes quotes comparable across ligatures, hyphen breaks and spacing."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", text).lower())


@dataclass
class AnalysisContext:
    """What the pipeline knows about the paper, shared with the prompt builder and the checks."""

    source_text: str = ""  # the whole paper, for verifying quotes and anchors
    article_type: str = pp.UNKNOWN
    type_source: str = "none"  # label | user | none
    structure_mode: str = "detected"  # detected | derived
    section_texts: dict[str, str] = field(default_factory=dict)  # headed sections (detected mode)
    section_roles: dict[str, str] = field(default_factory=dict)
    abstract: str = ""
    body: str = ""  # the whole body, sent to the model in derived mode
    refers_to_si: bool = False
    citations_total: int = 0
    citations_verified: int = 0
    citations_ghost: int = 0
    citations_unchecked: int = 0
    extra_text: str = ""  # citation summary and journal data: numbers there are legitimate
    numbers_text: str = ""  # text whose numbers count as "in the paper" (the body, not the reference list)
    corpus: str = field(init=False, default="")
    norm_text: str = field(init=False, default="")  # the paper, Unicode-normalised
    corpus_index: list[int] = field(init=False, default_factory=list)  # corpus position -> position in norm_text
    numbers: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.norm_text = unicodedata.normalize("NFKC", self.source_text)
        chars: list[str] = []
        for position, char in enumerate(self.norm_text):
            for lower in char.lower():
                if "a" <= lower <= "z" or "0" <= lower <= "9":
                    chars.append(lower)
                    self.corpus_index.append(position)
        self.corpus = "".join(chars)
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


def _clean_reasons(
    reasons: Any, ctx: AnalysisContext, notes: list[str], starts: list[tuple[int, str]]
) -> list[dict[str, Any]]:
    """Keep only rejection reasons with all four parts: problem, evidence, impact, solution.

    The evidence must be words that are really in the manuscript, and its location (section, nearby figure)
    is worked out by code, not taken from the model. A solution that shares nothing specific with the problem
    and the evidence is advice that would fit any paper, so that reason is removed too.
    """
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
            continue
        if min(len(problem), len(impact), len(solution)) < MIN_REASON_PART_CHARS:
            incomplete += 1
            continue
        if not (_stems(solution) & _stems(f"{problem} {evidence}")):
            generic += 1
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
            }
        )
    if no_evidence:
        notes.append(f"{no_evidence} rejection reason(s) were removed because they did not point to any text in the manuscript.")
    if incomplete:
        notes.append(f"{incomplete} rejection reason(s) were removed because they lacked a problem, an impact or a solution.")
    if generic:
        notes.append(f"{generic} rejection reason(s) were removed because the solution was not specific to the cited passage.")
    return kept[:MAX_REASONS]


def _section_starts(ctx: AnalysisContext, sections: dict[str, dict[str, Any]]) -> list[tuple[int, str]]:
    """Where each section begins in the paper, in reading order."""
    if ctx.structure_mode == "derived":
        found = [(_locate(str(f.get("anchor", "")), ctx), name) for name, f in sections.items()]
    else:
        found = [(_locate(text[:80], ctx), name) for name, text in ctx.section_texts.items()]
    return sorted((pos, name) for pos, name in found if pos >= 0)


def _clean_sections(raw: Any, ctx: AnalysisContext | None, notes: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Valid sections keyed by name, and the roles they have."""
    kept: list[tuple[int, str, dict[str, Any]]] = []  # (position in paper, name, fields)
    seen: set[str] = set()
    roles: list[str] = []
    detected_keys = {_key(k): k for k in (ctx.section_texts if ctx else {})}

    for entry in _section_entries(raw):
        name = re.sub(r"\s+", " ", entry["name"]).strip(" .:-")
        score = _to_score(entry.get("score"))
        if not (2 <= len(name) <= 48) or score is None:
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
            key_name = name
        elif ctx is not None:
            match = detected_keys.get(_key(name))
            if match is None:
                notes.append(f'Section "{name}" was removed: it is not a section of this paper.')
                continue
            fields["source"] = "detected"
            key_name = match
            role = ctx.section_roles.get(match, role)
        else:
            key_name = name

        if _key(key_name) in seen:
            continue
        seen.add(_key(key_name))
        roles.append(role)
        kept.append((position, key_name, fields))

    if ctx is not None and ctx.structure_mode == "derived":
        kept.sort(key=lambda item: item[0])  # reading order of the paper
    return {name: fields for _, name, fields in kept[:MAX_SECTIONS]}, roles[:MAX_SECTIONS]


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
    """Reference integrity from the citation check. Skipped when nothing could be checked."""
    checked = ctx.citations_total - ctx.citations_unchecked
    if ctx.citations_total <= 0 or checked <= 0:
        return None
    score = round(100 * ctx.citations_verified / checked) - min(40, 10 * ctx.citations_ghost)
    reason = f"{ctx.citations_verified} of {checked} checked references verified"
    if ctx.citations_ghost:
        reason += f"; {ctx.citations_ghost} could not exist (ghost)"
    return {
        "score": max(0, min(100, score)),
        "reason": reason + ".",
        "weight": pp.CITATION_WEIGHT,
        "computed": True,
    }


def _text_fields(payload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    """Every free-text field of the report as (container, key), so it can be checked in place."""
    for key in ("verdict", "oracle_letter", "novelty_assessment", "journal_fit"):
        if isinstance(payload.get(key), str):
            yield payload, key
    for container in list(payload.get("sections", {}).values()) + list(payload.get("rejection_reasons", [])):
        if isinstance(container, dict):
            for key in ("issue", "fix", "strength", "weakness", "reason"):
                if isinstance(container.get(key), str):
                    yield container, key
    for container in payload.get("criteria", {}).values():
        if isinstance(container, dict) and isinstance(container.get("reason"), str):
            yield container, "reason"


def finalize_payload(parsed: dict[str, Any], ctx: AnalysisContext | None) -> dict[str, Any]:
    """Turn the model's JSON into a checked report. Without a context only the section shape is normalised."""
    out = dict(parsed)
    notes: list[str] = []
    sections, roles = _clean_sections(parsed.get("sections"), ctx, notes)
    out["sections"] = sections
    out.pop("absent_sections", None)
    if ctx is None:
        return out

    # ---- article type: stated in the file or chosen by the author wins over the model's reading
    model_type = pp.coerce_article_type(parsed.get("paper_format"))
    article_type = ctx.article_type if ctx.article_type in pp.ARTICLE_TYPES else (model_type or pp.UNKNOWN)
    profile = pp.get_profile(article_type)

    # ---- a rejection reason must point to text that is in the paper
    out["rejection_reasons"] = _clean_reasons(out.get("rejection_reasons"), ctx, notes, _section_starts(ctx, sections))

    # ---- remove quotations that are not in the paper, flag numbers that are not
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
    total_weight = sum(c["weight"] for c in criteria.values())
    if llm_criteria_present and total_weight > 0:
        overall = sum(c["weight"] * c["score"] for c in criteria.values()) / total_weight
    else:
        overall = _to_score(parsed.get("mood_score")) or 0
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

    final_score = max(0, min(100, int(round(overall)) - penalty))
    out["mood_score"] = final_score
    out["mood"] = mood_for_score(final_score)
    out["paper_format"] = article_type
    out["criteria"] = criteria
    out["not_assessed"] = not_assessed
    out["grounding_notes"] = notes[:MAX_GROUNDING_NOTES]
    return out


def describe_for_log(ctx: AnalysisContext | None) -> str:
    if ctx is None:
        return "no-context"
    return json.dumps(
        {"type": ctx.article_type, "source": ctx.type_source, "mode": ctx.structure_mode, "si": ctx.refers_to_si}
    )
