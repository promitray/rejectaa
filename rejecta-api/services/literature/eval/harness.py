"""Leakage-safe evaluation of literature retrieval and journal ranking on the fixed evaluation set.

Two measures:
- reference recall@k: how many of a published paper's own references the retriever finds among its top k;
- journal top-k accuracy: whether the paper's real journal is among the top k journals ranked for it.

What keeps the numbers honest:
- the retriever is given only what an uploaded manuscript has (title, abstract, date, author names). The paper's
  references are hidden, and candidate route (a), "the paper's references and their related / cited-by works",
  is switched off (`RetrievalOptions.use_paper_references = False`), or those works would come straight back;
- every retrieved work is filtered as at analysis time: the paper itself (same id or DOI) is removed, works from
  the paper's date on are removed (date cut-off), and works that cite the paper are removed (they postdate it);
- the set is the frozen file (eval_set_v1.json), so two runs compare the same papers.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.literature.types import Work

DEFAULT_SET = Path(__file__).resolve().parent / "eval_set_v1.json"


@dataclass(frozen=True)
class EvalPaper:
    work: Work
    field: str
    journal_id: str
    journal_name: str


@dataclass(frozen=True)
class EvalQuery:
    """What the retriever may see: what an uploaded manuscript carries. No references, no OpenAlex id."""

    title: str
    abstract: str
    date: dt.date
    author_names: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalOptions:
    use_paper_references: bool = True  # candidate route (a); always False in the evaluation
    before: dt.date | None = None  # date cut-off


@dataclass
class Report:
    measure: str
    k: int
    n: int
    mean: float
    per_paper: list[dict[str, Any]] = field(default_factory=list)


def load_eval_set(path: Path = DEFAULT_SET) -> tuple[str, list[EvalPaper]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    papers = [
        EvalPaper(Work.from_json(p), p["field"], p["journal"]["source_id"], p["journal"]["name"])
        for p in data.get("papers") or []
    ]
    return str(data.get("version") or ""), papers


def query_for(paper: EvalPaper) -> EvalQuery:
    w = paper.work
    return EvalQuery(w.title, w.abstract, w.publication_date or dt.date(w.year or 1900, 1, 1), tuple(w.author_names))


def leak_filter(candidates: list[Work], paper: Work) -> list[Work]:
    """The candidates an analysis of `paper` could legitimately use: not the paper itself, nothing from its date on,
    nothing that cites it. Undated candidates are dropped too (they cannot be shown to be prior)."""
    cutoff = paper.publication_date or (dt.date(paper.year, 1, 1) if paper.year else None)
    out: list[Work] = []
    for c in candidates:
        if c.id == paper.id or (c.doi and paper.doi and c.doi.lower() == paper.doi.lower()):
            continue
        if paper.id in c.reference_ids:
            continue
        date = c.publication_date or (dt.date(c.year, 12, 31) if c.year else None)
        if date is None or (cutoff is not None and date >= cutoff):
            continue
        out.append(c)
    return out


def recall_at_k(retrieved_ids: list[str], reference_ids: list[str], k: int) -> float:
    """Share of the references found among the first k retrieved (the references that could be found at all:
    those dated before the paper are all of them by construction, so the denominator is the reference list)."""
    refs = set(reference_ids)
    if not refs:
        return 0.0
    return len(refs & set(retrieved_ids[:k])) / len(refs)


def top_k_hit(ranked_journal_ids: list[str], true_id: str, k: int) -> bool:
    return true_id in ranked_journal_ids[:k]


Retriever = Callable[[EvalQuery, RetrievalOptions], Awaitable[list[Work]]]
JournalRanker = Callable[[EvalQuery], Awaitable[list[str]]]


async def reference_recall(retriever: Retriever, papers: list[EvalPaper], k: int = 20) -> Report:
    per_paper: list[dict[str, Any]] = []
    for paper in papers:
        query = query_for(paper)
        options = RetrievalOptions(use_paper_references=False, before=query.date)
        found = leak_filter(await retriever(query, options), paper.work)
        score = recall_at_k([w.id for w in found], paper.work.reference_ids, k)
        per_paper.append({"id": paper.work.id, "field": paper.field, "recall": round(score, 4),
                          "references": len(paper.work.reference_ids), "retrieved": len(found)})
    mean = sum(p["recall"] for p in per_paper) / len(per_paper) if per_paper else 0.0
    return Report("reference_recall", k, len(per_paper), round(mean, 4), per_paper)


async def journal_top_k(ranker: JournalRanker, papers: list[EvalPaper], k: int = 3) -> Report:
    per_paper: list[dict[str, Any]] = []
    for paper in papers:
        ranked = await ranker(query_for(paper))
        hit = top_k_hit(ranked, paper.journal_id, k)
        per_paper.append({"id": paper.work.id, "field": paper.field, "journal": paper.journal_name, "hit": hit,
                          "ranked": ranked[:k]})
    mean = sum(1 for p in per_paper if p["hit"]) / len(per_paper) if per_paper else 0.0
    return Report("journal_top_k", k, len(per_paper), round(mean, 4), per_paper)
