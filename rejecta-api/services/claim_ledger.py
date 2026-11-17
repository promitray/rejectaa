"""The claim ledger: what this paper claims and what it is made of, each entry tied to the sentences that say it.

One model call reads the whole paper (numbered by services.paper_index) and lists:
- the paper's field and kind (from fixed lists), which choose the rubric packs;
- its claims (at most MAX_CLAIMS): a faithful one-line statement, a type (novelty, comparative, causal, ...),
  whether it is a main claim, and the ids of the sentences where the paper states it;
- its key facts (datasets, models, materials, sample sizes, metrics...), each with its exact words and ids.

Code then keeps only what the paper supports: an id that does not exist is dropped; a claim whose statement shares
no content word with its sentences, or has a number they do not contain, is dropped; a fact whose words are not in
its sentences is dropped. The questions of the review are built from these claims (services.rubrics), so a
finding always concerns something this paper actually says.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from services.analysis_guard import _stems, alnum
from services.paper_index import PaperIndex, ids_in
from services.rubrics import CLAIM_TYPES, FIELDS, KINDS, Claim

logger = logging.getLogger(__name__)

LEDGER_PROMPT_VERSION = "2026-11-13.1"
MAX_CLAIMS = 8
MAX_FACTS = 24
MAX_PAPER_CHARS = 110_000  # the numbered paper; longer papers keep their opening and closing parts whole
FACT_KINDS = (
    "dataset", "model", "method", "material", "compound", "sample_size", "metric", "instrument", "software",
    "condition", "baseline", "other",
)

_SYSTEM = (
    "You read a scientific manuscript and record, without judging it, what it claims and what it is made of. "
    "Every entry cites the ids of the sentences that state it. You never add anything the paper does not say. "
    "Return ONLY valid JSON."
)

_RULES = f"""The paper is given below, one sentence per line, each with its id in brackets, e.g. [P12.3].

Return:
{{"field": one of {list(FIELDS)},
 "paper_kind": one of {list(KINDS)},
 "claims": [{{"id": "C1", "statement": "...", "type": one of {list(CLAIM_TYPES)}, "main": true|false, "ids": ["P1.2", "P14.1"]}}],
 "facts": [{{"kind": one of {list(FACT_KINDS)}, "value": "...", "ids": ["P8.2"]}}]}}

Claims (at most {MAX_CLAIMS}, the most important first):
- A claim is something the paper asserts as its finding or contribution: what it shows, is better than, causes,
  explains, is the first to do, holds generally, or is useful for. Background facts about earlier work are not claims.
- "statement": the claim in at most 30 words, faithful to the paper's own words and strength (do not soften or
  strengthen it; keep its numbers exactly).
- "type": novelty (first / new / most comprehensive), comparative (better or worse than something), causal (X
  causes or explains Y), mechanism (how something happens), generalization (holds broadly / for all),
  quantitative (a specific measured value), practical (useful for an application).
- "main": true for the paper's central claims (in the abstract or the conclusion), at most 4.
- "ids": the sentences where the paper states the claim (the abstract and the conclusion first). Only ids shown.

Facts (at most {MAX_FACTS}): the datasets, models or baselines, materials or compounds, sample sizes, metrics,
instruments, software and key conditions the work uses. "value": the exact words of the paper (a name or a
number with its unit), "ids": where it is stated.
"""


@dataclass
class Fact:
    kind: str
    value: str
    ids: tuple[str, ...]


@dataclass
class Ledger:
    field: str = "other"
    paper_kind: str = "other"
    claims: list[Claim] = dc_field(default_factory=list)
    facts: list[Fact] = dc_field(default_factory=list)
    dropped: int = 0  # entries the paper did not support
    status: str = "not_run"  # done | failed | not_run


def numbered_paper(index: PaperIndex, budget: int = MAX_PAPER_CHARS) -> str:
    """The whole paper with ids. Too long: every section keeps its start and end, the middle of long sections
    is left out (claims sit in the abstract, the introduction's end, the results and the conclusion)."""
    blocks = [(name, index.numbered(name)) for name in index.sections]
    total = sum(len(text) for _, text in blocks)
    if total > budget:
        share = max(2000, budget // max(1, len(blocks)))
        trimmed = []
        for name, text in blocks:
            if len(text) > share:
                head = int(share * 0.6)
                text = text[:head].rsplit("\n", 1)[0] + "\n[… part of this section not shown …]\n" + text[-(share - head):].split("\n", 1)[-1]
            trimmed.append((name, text))
        blocks = trimmed
    return "\n\n".join(f"## {name.upper()}\n{text}" for name, text in blocks)


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _valid_ids(value: Any, index: PaperIndex) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in ids_in(value) if i in index.sentences))


def check_ledger(raw: dict[str, Any], index: PaperIndex) -> Ledger:
    """The model's ledger with everything the paper does not support removed."""
    ledger = Ledger(status="done")
    field_value = str(raw.get("field") or "").strip().lower()
    kind_value = str(raw.get("paper_kind") or "").strip().lower()
    ledger.field = field_value if field_value in FIELDS else "other"
    ledger.paper_kind = kind_value if kind_value in KINDS else "other"
    seen: set[str] = set()
    main_count = 0
    for entry in raw.get("claims") or []:
        if not isinstance(entry, dict) or len(ledger.claims) >= MAX_CLAIMS:
            continue
        statement = re.sub(r"\s+", " ", str(entry.get("statement") or "")).strip()
        claim_type = str(entry.get("type") or "").strip().lower()
        ids = _valid_ids(entry.get("ids"), index)
        source = index.text_of(ids)
        if (
            len(statement) < 15 or claim_type not in CLAIM_TYPES or not ids
            or not (_stems(statement) & _stems(source))
            or not _numbers(statement) <= _numbers(source)
            or alnum(statement) in seen
        ):
            ledger.dropped += 1
            continue
        seen.add(alnum(statement))
        main = bool(entry.get("main")) and main_count < 4
        main_count += int(main)
        ledger.claims.append(Claim(f"C{len(ledger.claims) + 1}", statement[:300], claim_type, main, ids))
    for entry in raw.get("facts") or []:
        if not isinstance(entry, dict) or len(ledger.facts) >= MAX_FACTS:
            continue
        value = re.sub(r"\s+", " ", str(entry.get("value") or "")).strip()
        kind = str(entry.get("kind") or "other").strip().lower()
        ids = _valid_ids(entry.get("ids"), index)
        if not value or not ids or len(alnum(value)) < 2 or alnum(value) not in alnum(index.text_of(ids)):
            ledger.dropped += 1
            continue
        ledger.facts.append(Fact(kind if kind in FACT_KINDS else "other", value[:160], ids))
    if not any(c.main for c in ledger.claims) and ledger.claims:
        first = ledger.claims[0]
        ledger.claims[0] = Claim(first.id, first.statement, first.type, True, first.evidence)
    return ledger


async def build_ledger(index: PaperIndex, provider: Any, journal: str) -> Ledger:
    """The checked ledger. Never raises: on failure the ledger is empty and marked failed."""
    if not index.sentences or not hasattr(provider, "generate"):
        return Ledger()
    prompt = f"{_RULES}\nTARGET JOURNAL: {journal or 'not given'}\n\nTHE PAPER:\n\n{numbered_paper(index)}"
    try:
        raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0)
        raw = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
    except Exception:  # noqa: BLE001
        logger.warning("Claim ledger call failed", exc_info=True)
        return Ledger(status="failed")
    if not isinstance(raw, dict):
        return Ledger(status="failed")
    return check_ledger(raw, index)


def ledger_lines(ledger: Ledger) -> str:
    """The ledger as the investigation prompt shows it."""
    lines = [f"FIELD: {ledger.field}   KIND OF PAPER: {ledger.paper_kind}", "CLAIMS OF THE PAPER:"]
    lines += [f"- {c.id}{' (main)' if c.main else ''} [{c.type}] {c.statement}  — stated in {', '.join(c.evidence[:3])}" for c in ledger.claims]
    if ledger.facts:
        lines.append("WHAT THE WORK USES:")
        lines += [f"- {f.kind}: {f.value}" for f in ledger.facts]
    return "\n".join(lines)
