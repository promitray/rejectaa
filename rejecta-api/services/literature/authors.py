"""The manuscript's authors resolved to OpenAlex author ids, and the prior-version labels that follow.

An uploaded manuscript is not in OpenAlex, so its authors are resolved one by one:
1. OpenAlex author search by the name (up to CANDIDATES per name);
2. a candidate must match the name: same surname and the same first initial (either name order, for names
   written family-name first);
3. evidence, each worth one point: an affiliation of the candidate shares the distinctive words of an affiliation
   in the manuscript; a co-author of the candidate's works is another author of the manuscript;
4. resolved when exactly one candidate has the best score and at least one point of evidence. Otherwise the name
   stays unresolved: a common name is never guessed.

Labels for a candidate work (decided here, used in P2):
- `prior_version_same_authors`: it shares a resolved author id and is very similar (title or embedding);
- `same_authors`: it shares a resolved author id only (the authors' own earlier work);
- `possible_prior_version`: no author could be resolved (or the manuscript is anonymised) and it is very similar.
None of these counts against novelty; the first is shown as a prior-presentation fact, the last as a question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.literature.manuscript import Manuscript, ascii_fold
from services.literature.openalex import OpenAlexClient, short_id
from services.literature.types import Work

CANDIDATES = 8
CO_AUTHOR_CHECKS = 3  # candidates per name whose works are read for co-authors
MAX_NAMES = 8
PRIOR_VERSION_SIMILARITY = 0.85  # embedding cosine for "the same work" with a shared author
POSSIBLE_PRIOR_SIMILARITY = 0.92  # higher bar when authorship cannot be checked
PRIOR_VERSION_TITLE = 0.6  # title word overlap (Jaccard) for the same
POSSIBLE_PRIOR_TITLE = 0.8
_STOP = frozenset("of the and for in at on de des la le di du von van university institute department school".split())

PRIOR_VERSION = "prior_version_same_authors"
SAME_AUTHORS = "same_authors"
POSSIBLE_PRIOR = "possible_prior_version"


def name_key(name: str) -> tuple[str, str]:
    """(surname, first initial), lower case, accents removed."""
    parts = [p for p in re.split(r"[\s\-]+", ascii_fold(name).lower().replace(".", " ")) if p]
    if not parts:
        return "", ""
    return parts[-1], parts[0][:1]


def same_person_name(a: str, b: str) -> bool:
    """Same surname and first initial, in either order ("Xia Jun" = "Jun Xia")."""
    pa = [p for p in re.split(r"[\s\-]+", ascii_fold(a).lower().replace(".", " ")) if p]
    pb = [p for p in re.split(r"[\s\-]+", ascii_fold(b).lower().replace(".", " ")) if p]
    if len(pa) < 2 or len(pb) < 2:
        return False
    if pa[-1] == pb[-1] and pa[0][:1] == pb[0][:1]:
        return True
    return pa[0] == pb[-1] and pa[-1][:1] == pb[0][:1]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", ascii_fold(text).lower()) if w not in _STOP}


@dataclass
class ResolvedAuthor:
    name: str
    openalex_id: str | None
    score: int = 0
    evidence: list[str] = field(default_factory=list)


async def resolve_authors(ms: Manuscript, client: OpenAlexClient) -> list[ResolvedAuthor]:
    """Each manuscript author with an OpenAlex id when resolved without doubt (see the module docstring)."""
    if ms.anonymised:
        return []
    affiliation_words = [_words(a) for a in ms.affiliations if _words(a)]
    out: list[ResolvedAuthor] = []
    for name in ms.author_names[:MAX_NAMES]:
        data = await client.get("/authors", {
            "search": name, "per_page": CANDIDATES,
            "select": "id,display_name,display_name_alternatives,last_known_institutions,affiliations,works_count",
        })
        scored: list[tuple[int, str, list[str]]] = []
        checked = 0
        for cand in (data or {}).get("results") or []:
            names = [cand.get("display_name") or "", *(cand.get("display_name_alternatives") or [])]
            if not any(same_person_name(name, n) for n in names if n):
                continue
            score, evidence = 0, []
            insts = [i.get("display_name") or "" for i in cand.get("last_known_institutions") or []]
            insts += [(a.get("institution") or {}).get("display_name") or "" for a in cand.get("affiliations") or []]
            for inst in insts:
                iw = _words(inst)
                if iw and any(len(iw & aw) >= max(1, min(2, len(iw))) for aw in affiliation_words):
                    score += 1
                    evidence.append(f"affiliation: {inst}")
                    break
            others = [n for n in ms.author_names if n != name]
            if others and checked < CO_AUTHOR_CHECKS:
                checked += 1
                works = await client.get("/works", {
                    "filter": f"author.id:{short_id(cand.get('id'))}", "per_page": 50, "select": "authorships",
                    "sort": "publication_date:desc",
                })
                coauthors = {
                    (a.get("author") or {}).get("display_name") or ""
                    for w in (works or {}).get("results") or [] for a in w.get("authorships") or []
                }
                shared = [o for o in others if any(same_person_name(o, c) for c in coauthors if c)]
                if shared:
                    score += 1
                    evidence.append("co-authors: " + ", ".join(shared[:3]))
            scored.append((score, short_id(cand.get("id")), evidence))
        scored.sort(key=lambda s: (-s[0], s[1]))
        unique_best = scored and scored[0][0] >= 1 and (len(scored) == 1 or scored[1][0] < scored[0][0])
        if unique_best:
            out.append(ResolvedAuthor(name, scored[0][1], scored[0][0], scored[0][2]))
        else:
            out.append(ResolvedAuthor(name, None, scored[0][0] if scored else 0, ["not resolved without doubt"]))
    return out


def title_similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def prior_version_label(work: Work, ms: Manuscript, resolved: list[ResolvedAuthor], similarity: float) -> str:
    """The label for a candidate work (see the module docstring); "" for none."""
    ids = {r.openalex_id for r in resolved if r.openalex_id}
    title_sim = title_similarity(work.title, ms.title)
    if ids and ids & set(work.author_ids):
        if similarity >= PRIOR_VERSION_SIMILARITY or title_sim >= PRIOR_VERSION_TITLE:
            return PRIOR_VERSION
        return SAME_AUTHORS
    if (ms.anonymised or not ids) and (similarity >= POSSIBLE_PRIOR_SIMILARITY or title_sim >= POSSIBLE_PRIOR_TITLE):
        return POSSIBLE_PRIOR
    return ""
