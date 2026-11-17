"""Ranking candidate prior work: hybrid (keywords + embeddings, fused by rank) and an LLM rerank of the top.

Hybrid: BM25 over each candidate's title and abstract against the manuscript's title, abstract and claims, cosine
similarity of embeddings, and bibliographic coupling (how many of the closest search hits cite it); fused by
reciprocal rank (k=60). Scores are rounded and ties go to the smaller
id, so the same pool always gives the same order.

Rerank (one model call, temperature 0, cached): the top RERANK_POOL are shown as [W1]..[Wn] with title, year,
venue and abstract; for each the model gives a relevance (0 unrelated .. 3 the same problem and approach) and the
dimensions it shares with the manuscript (task, method, data_material, finding). Ids it was not shown are ignored;
an item it skipped keeps no relevance. Final order: relevance, then the hybrid order.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from typing import Any

from services.literature.candidates import Candidate
from services.literature.manuscript import Manuscript
from services.retrieval import tokens

logger = logging.getLogger(__name__)

RERANK_PROMPT_VERSION = "lit-rerank-2026-11-16.1"
RRF_K = 60
# weight of each ranking in the fusion (set from the evaluation, see README "P1 results")
WEIGHTS = {"keyword": 1.0, "meaning": 1.0, "coupling": 1.0}
BM25_K1 = 1.4
BM25_B = 0.75
DIGITS = 6
RERANK_POOL = 30
ABSTRACT_CHARS = 700
RELATIONS = ("task", "method", "data_material", "finding")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def bm25_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
    terms = list(dict.fromkeys(tokens(query)))
    counted = {k: Counter(tokens(v)) for k, v in docs.items()}
    lengths = {k: sum(c.values()) for k, c in counted.items()}
    avg = (sum(lengths.values()) / len(lengths)) if lengths else 1.0
    df: Counter[str] = Counter()
    for c in counted.values():
        df.update(set(c))
    n = max(1, len(docs))
    out: dict[str, float] = {}
    for key, counts in counted.items():
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * lengths[key] / (avg or 1.0)))
        out[key] = round(score, DIGITS)
    return out


def hybrid(cands: list[Candidate], ms: Manuscript, query_vector: list[float] | None, vectors: dict[str, list[float]]) -> list[Candidate]:
    """The candidates in fused order, with their keyword, similarity and fused scores set."""
    docs = {c.work.id: f"{c.work.title} {c.work.abstract}" for c in cands}
    keyword = bm25_scores(ms.query_text(), docs)
    for c in cands:
        c.keyword = keyword.get(c.work.id, 0.0)
        vec = vectors.get(c.work.id)
        c.similarity = round(cosine(query_vector, vec), DIGITS) if query_vector and vec else 0.0
    by_keyword = sorted(cands, key=lambda c: (-c.keyword, c.work.id))
    by_meaning = sorted(cands, key=lambda c: (-c.similarity, c.work.id)) if query_vector else []
    # bibliographic coupling: cited by several of the closest earlier papers (only those that are)
    by_coupling = sorted((c for c in cands if c.co_cited > 0), key=lambda c: (-c.co_cited, c.work.id))
    fused: dict[str, float] = {}
    for ranking, weight in ((by_keyword, WEIGHTS["keyword"]), (by_meaning, WEIGHTS["meaning"]), (by_coupling, WEIGHTS["coupling"])):
        for rank, c in enumerate(ranking, start=1):
            fused[c.work.id] = fused.get(c.work.id, 0.0) + weight / (RRF_K + rank)
    for c in cands:
        c.fused = round(fused.get(c.work.id, 0.0), DIGITS)
    return sorted(cands, key=lambda c: (-c.fused, c.work.id))


_SYSTEM = (
    "You compare a new manuscript with earlier papers and judge how closely each earlier paper relates to it. You are "
    "literal and conservative. Return ONLY valid JSON."
)
_RULES = """For EACH earlier paper [W..] give:
- "relevance": 3 = the same problem AND the same kind of approach or material; 2 = clearly the same problem or the
  same method/material applied to a close problem; 1 = the same broad area only; 0 = unrelated.
- "shares": which of these it shares with the manuscript: "task" (the problem or question), "method" (the
  approach or technique), "data_material" (the datasets, compounds, materials or systems), "finding" (the same
  result or conclusion). Only what the title and abstract show.
A paper with no abstract is judged on its title alone: never give it relevance 3.
Return {"items": [{"id": "W1", "relevance": 2, "shares": ["task", "method"]}]}"""


def _rerank_prompt(ms: Manuscript, top: list[Candidate]) -> tuple[str, dict[str, str]]:
    labels: dict[str, str] = {}
    blocks = []
    for n, c in enumerate(top, start=1):
        label = f"W{n}"
        labels[label] = c.work.id
        abstract = "(no abstract)" if c.work.abstract_missing else c.work.abstract[:ABSTRACT_CHARS]
        blocks.append(f"[{label}] {c.work.title} ({c.work.year or 'n.d.'}; {c.work.venue_name or 'unknown venue'})\n{abstract}")
    claims = "\n".join(f"- {c}" for c in ms.claims[:6])
    manuscript = f"TITLE: {ms.title}\nABSTRACT: {ms.abstract[:2500]}" + (f"\nCLAIMS:\n{claims}" if claims else "")
    return f"{_RULES}\n\nTHE MANUSCRIPT:\n{manuscript}\n\nEARLIER PAPERS:\n\n" + "\n\n".join(blocks), labels


async def rerank(ms: Manuscript, ordered: list[Candidate], provider: Any) -> list[Candidate]:
    """The candidates with the top RERANK_POOL rated by the model, best first. Never raises: on failure the
    hybrid order stays."""
    top = ordered[:RERANK_POOL]
    if not top or not hasattr(provider, "generate"):
        return ordered
    prompt, labels = _rerank_prompt(ms, top)
    try:
        raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0)
        raw = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
    except Exception:  # noqa: BLE001
        logger.warning("Literature rerank failed; keeping the hybrid order", exc_info=True)
        return ordered
    by_id = {c.work.id: c for c in top}
    for item in (raw or {}).get("items") or [] if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        work_id = labels.get(str(item.get("id") or "").strip())
        cand = by_id.get(work_id or "")
        if cand is None or cand.relevance is not None:
            continue
        try:
            relevance = max(0, min(3, int(item.get("relevance"))))
        except (TypeError, ValueError):
            continue
        if cand.work.abstract_missing:
            relevance = min(relevance, 2)
        cand.relevance = relevance
        shares = item.get("shares") if isinstance(item.get("shares"), list) else []
        cand.relations = [r for r in RELATIONS if r in {str(s).strip().lower() for s in shares}]
    rated = sorted(top, key=lambda c: (-(c.relevance if c.relevance is not None else -1), -c.fused, c.work.id))
    return rated + ordered[RERANK_POOL:]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
