"""Literature knowledge base, P1: byline, author resolution, prior-version labels, candidates, ranking, snapshot."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from typing import Any

import pytest

from services.literature import authors as au
from services.literature.candidates import Options, gather, is_self, search_queries
from services.literature.manuscript import Manuscript, extract_byline
from services.literature.ranking import bm25_scores, hybrid, rerank
from services.literature.retrieve import retrieve_prior_work
from services.literature.types import PaperDate, Work

TODAY = dt.date(2026, 11, 16)


# ---------------------------------------------------------------- byline


def test_byline_with_affiliation_marks_glued_to_names() -> None:
    front = ("Why Deep Models Often Cannot Beat Non-deep Counterparts\non Molecular Property Prediction?\n"
             "Jun Xia * 1 Lecheng Zhang * 1 Xiao Zhu * 1 Stan Z. Li 1\nAbstract\nMolecular property prediction ...")
    names, affiliations, anonymised = extract_byline(
        front, "Why Deep Models Often Cannot Beat Non-deep Counterparts on Molecular Property Prediction?",
        "Molecular property prediction ...")
    assert names == ["Jun Xia", "Lecheng Zhang", "Xiao Zhu", "Stan Z. Li"] and not anonymised


def test_byline_after_a_title_whose_words_recur_in_the_abstract() -> None:
    front = ("Article\nhttps://doi.org/10.1038/x\nChiral molecular imprinting-based SERS\n"
             "detection strategy for absolute enantiomeric\ndiscrimination\n"
             "Maryam Arabi1, Abbas Ostovan1, Yunqing Wang\n1,2\n, Rongchao Mei1,\n"
             "Department of Chemistry, Yantai University, Yantai, China\n"
             "Chiral discrimination is critical ... absolute enantiomeric discrimination ...")
    names, affiliations, _ = extract_byline(
        front, "Chiral molecular imprinting-based SERS detection strategy for absolute enantiomeric discrimination",
        "Chiral discrimination is critical")
    assert names == ["Maryam Arabi", "Abbas Ostovan", "Yunqing Wang", "Rongchao Mei"]
    assert affiliations and affiliations[0].startswith("Department of Chemistry")


def test_an_anonymised_manuscript_has_no_authors() -> None:
    names, _, anonymised = extract_byline("A Title Here\nAnonymous Authors\nAbstract\nx", "A Title Here", "x")
    assert names == [] and anonymised


# ---------------------------------------------------------------- author resolution and labels


def test_names_match_in_either_order_and_by_initial() -> None:
    assert au.same_person_name("Jun Xia", "J. Xia") and au.same_person_name("Jun Xia", "Xia Jun")
    assert not au.same_person_name("Jun Xia", "Jun Li")


class FakeOpenAlex:
    """Answers /authors and /works?filter=author.id from a script; search/DOI/related/citing from lists."""

    def __init__(self, authors: dict[str, list[dict]] | None = None, coauthors: dict[str, list[str]] | None = None,
                 search: dict[str, list[Work]] | None = None, by_doi: list[Work] | None = None,
                 by_id: list[Work] | None = None, citing: list[Work] | None = None) -> None:
        self.authors, self.coauthors = authors or {}, coauthors or {}
        self.search_results, self.by_doi, self.by_id, self.citing_works = search or {}, by_doi or [], by_id or [], citing or []
        self.calls: list[str] = []

    async def get(self, path: str, params: dict[str, Any]) -> dict | None:
        if path == "/authors":
            return {"results": self.authors.get(params["search"], [])}
        author = params["filter"].split(":", 1)[1]
        return {"results": [{"authorships": [{"author": {"display_name": n}} for n in self.coauthors.get(author, [])]}]}

    async def search(self, query: str, filters: str = "", per_page: int = 50, sort: str = "") -> list[Work]:
        self.calls.append(f"search:{query}:{sort}")
        return [] if sort else self.search_results.get(query, [])

    async def works_by_dois(self, dois: list[str]) -> list[Work]:
        self.calls.append("dois")
        return self.by_doi

    async def works_by_ids(self, ids: list[str]) -> list[Work]:
        self.calls.append("ids")
        return [w for w in self.by_id if w.id in ids]

    async def citing(self, work_id: str, before: str, per_page: int = 25) -> list[Work]:
        self.calls.append("citing")
        return self.citing_works

    async def close(self) -> None:
        pass


def ms(**extra: Any) -> Manuscript:
    values: dict[str, Any] = dict(paper_hash="h", title="Deep models fail on molecular property prediction",
                                  abstract="We benchmark deep and tree models on molecular datasets.",
                                  author_names=["Jun Xia", "Stan Z. Li"], affiliations=["Westlake University, Hangzhou"])
    values.update(extra)
    return Manuscript(**values)


def test_an_author_is_resolved_only_with_evidence_and_without_doubt() -> None:
    client = FakeOpenAlex(
        authors={
            "Jun Xia": [
                {"id": "https://openalex.org/A1", "display_name": "Jun Xia", "last_known_institutions": [{"display_name": "Westlake University"}]},
                {"id": "https://openalex.org/A2", "display_name": "Jun Xia", "last_known_institutions": [{"display_name": "Tsinghua University"}]},
            ],
            "Stan Z. Li": [
                {"id": "https://openalex.org/A3", "display_name": "Stan Z. Li"},
                {"id": "https://openalex.org/A4", "display_name": "Stan Li"},
            ],
        },
        coauthors={"A1": ["Stan Z. Li", "Someone Else"]},
    )
    resolved = asyncio.run(au.resolve_authors(ms(), client))
    assert resolved[0].openalex_id == "A1" and resolved[0].score == 2  # affiliation + co-author
    assert resolved[1].openalex_id is None  # two candidates, no evidence: never guessed


def work(wid: str, title: str = "t", date: dt.date | None = dt.date(2021, 1, 1), abstract: str = "a", **extra: Any) -> Work:
    return Work(id=wid, title=title, abstract=abstract, abstract_missing=not abstract, publication_date=date,
                year=date.year if date else None, **extra)


def test_prior_version_labels() -> None:
    resolved = [au.ResolvedAuthor("Jun Xia", "A1", 2)]
    similar = work("W1", "Deep models fail on molecular property prediction (workshop)", author_ids=["A1"])
    other = work("W2", "A different topic about catalysts", author_ids=["A1"])
    assert au.prior_version_label(similar, ms(), resolved, 0.95) == au.PRIOR_VERSION
    assert au.prior_version_label(other, ms(), resolved, 0.3) == au.SAME_AUTHORS
    stranger = work("W3", "Deep models fail on molecular property prediction", author_ids=["A9"])
    assert au.prior_version_label(stranger, ms(), resolved, 0.97) == ""  # authors resolved and not shared
    assert au.prior_version_label(stranger, ms(anonymised=True, author_names=[]), [], 0.97) == au.POSSIBLE_PRIOR


# ---------------------------------------------------------------- candidates


PAPER_DATE = PaperDate(dt.date(2023, 6, 30), "arxiv_stamp", "day")


def test_candidates_from_all_routes_with_exclusions_and_date_classes() -> None:
    ref = work("R1", "reference", related_ids=["X1"], cited_by_count=50)
    client = FakeOpenAlex(
        by_doi=[ref], by_id=[work("X1", "related")], citing=[work("C1", "citing", dt.date(2022, 1, 1))],
        search={"Deep models fail on molecular property prediction": [
            work("S1", "found"), work("SELF", "the paper", dt.date(2023, 7, 1), doi="10.1/me"),
            work("L1", "later", dt.date(2024, 1, 1)), work("N1", "concurrent", dt.date(2023, 3, 1)),
            work("U1", "undated", None), work("P1", "published version", dt.date(2023, 5, 1), doi="10.1/ME"),
        ]},
    )

    async def local(before: dt.date) -> list[Work]:
        return [work("S1", "found"), work("V1", "vector neighbour")]

    pool = asyncio.run(gather(ms(dois=["10.1/me"], reference_dois=["10.1/ref"]), PAPER_DATE, client, Options(), local))
    c = pool.candidates
    assert set(c) == {"R1", "X1", "C1", "S1", "N1", "V1"}
    assert c["S1"].routes == {"search", "local"} and c["R1"].in_references and c["N1"].date_class == "concurrent"
    assert sorted(pool.excluded_self) == ["P1", "SELF"] and pool.dropped_later == 1 and pool.dropped_undated == 1


def test_route_a_can_be_switched_off() -> None:
    client = FakeOpenAlex(by_doi=[work("R1")])
    pool = asyncio.run(gather(ms(reference_dois=["10.1/ref"]), PAPER_DATE, client, Options(use_paper_references=False)))
    assert "dois" not in client.calls and not pool.candidates


def test_only_an_exact_identifier_makes_a_work_the_paper_itself() -> None:
    m = ms(dois=["10.1/me"], arxiv_ids=["2306.17702"])
    assert is_self(work("A", doi="10.1/ME"), m) and is_self(work("B", arxiv_id="2306.17702"), m)
    assert not is_self(work("C", "Deep models fail on molecular property prediction"), m)


def test_search_queries_use_title_keywords_and_claims() -> None:
    queries = search_queries(ms(claims=["Tree models beat deep models"]))
    assert queries[0].startswith("Deep models fail") and "Tree models beat deep models" in queries


# ---------------------------------------------------------------- ranking


def test_bm25_prefers_matching_words() -> None:
    scores = bm25_scores("tree models molecules", {"a": "tree models for molecules", "b": "catalyst synthesis"})
    assert scores["a"] > scores["b"] == 0


def test_hybrid_order_is_deterministic_and_fuses_both() -> None:
    from services.literature.candidates import Candidate

    cands = [Candidate(work("W2", "catalysis")), Candidate(work("W1", "molecular property prediction deep models"))]
    vectors = {"W1": [0.0, 1.0], "W2": [1.0, 0.0]}
    order = hybrid(cands, ms(), [0.1, 0.9], vectors)
    assert [c.work.id for c in order] == ["W1", "W2"] and order[0].similarity > order[1].similarity
    assert [c.work.id for c in hybrid(cands, ms(), [0.1, 0.9], vectors)] == ["W1", "W2"]


class Reranker:
    def __init__(self, answer: dict) -> None:
        self.answer = answer

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict:
        return json.loads(raw)


def test_rerank_uses_only_shown_ids_and_caps_title_only_works() -> None:
    from services.literature.candidates import Candidate

    cands = [Candidate(work("A", "a")), Candidate(work("B", "b", abstract="")), Candidate(work("C", "c"))]
    answer = {"items": [{"id": "W3", "relevance": 3, "shares": ["task", "method", "nonsense"]},
                        {"id": "W2", "relevance": 3}, {"id": "W99", "relevance": 3}]}
    out = asyncio.run(rerank(ms(), cands, Reranker(answer)))
    assert [c.work.id for c in out] == ["C", "B", "A"]
    assert out[0].relations == ["task", "method"] and out[1].relevance == 2 and out[2].relevance is None


# ---------------------------------------------------------------- the whole retrieval with a fake store


class FakeStore:
    def __init__(self) -> None:
        self.works: dict[str, Work] = {}
        self.emb: dict[str, list[float]] = {}
        self.snapshots: dict[tuple, dict] = {}

    async def get_snapshot(self, kind: str, paper_hash: str, model: str, journal: str = "") -> dict | None:
        return self.snapshots.get((kind, paper_hash, model))

    async def put_snapshot(self, kind: str, paper_hash: str, model: str, payload: dict, journal: str = "") -> None:
        self.snapshots[(kind, paper_hash, model)] = json.loads(json.dumps(payload))

    async def upsert_works(self, works: list[Work]) -> None:
        self.works.update({w.id: w for w in works})

    async def embedded_hashes(self, model: str, ids: list[str]) -> dict[str, str]:
        return {}

    async def upsert_embeddings(self, model: str, rows: list) -> None:
        self.emb.update({r[0]: r[3] for r in rows})

    async def nearest(self, model: str, vector: list[float], k: int = 50, before: Any = None, **_: Any) -> list:
        return []

    async def get_works(self, ids: list[str]) -> dict[str, Work]:
        return {i: self.works[i] for i in ids if i in self.works}


class Provider(Reranker):
    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        out = []
        for t in texts:
            v = [float((hash(w) % 97) + 1) for w in t.lower().split()[:8]] + [0.0] * 8
            norm = math.sqrt(sum(x * x for x in v[:8])) or 1.0
            out.append([x / norm for x in v[:8]] + [0.0] * (1536 - 8))
        return out


@pytest.fixture
def no_store_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import settings
    from services import local_store

    monkeypatch.setattr(settings, "RAG_STORE", "off")
    local_store.reset_store()


def test_retrieve_builds_the_snapshot_and_reuses_it(no_store_writes: None) -> None:
    store = FakeStore()
    client = FakeOpenAlex(search={"Deep models fail on molecular property prediction": [
        work("S1", "Tree models versus deep models on molecules"), work("S2", "Catalyst synthesis"),
        work("PV", "Deep models fail on molecular property prediction", dt.date(2022, 12, 1)),
    ]})
    m = ms(anonymised=True, author_names=[], venue_statements=["arXiv:2306.17702v1 [cs.LG] 30 Jun 2023"])
    provider = Provider({"items": [{"id": "W1", "relevance": 2, "shares": ["task"]}]})
    first = asyncio.run(retrieve_prior_work(m, provider, store=store, client=client, today=TODAY))
    assert first["paper_date"]["date"] == "2023-06-30" and first["paper_date"]["source"] == "arxiv_stamp"
    ids = [w["id"] for w in first["works"]]
    assert set(ids) == {"S1", "S2", "PV"}
    pv = next(w for w in first["works"] if w["id"] == "PV")
    assert pv["label"] == au.POSSIBLE_PRIOR  # anonymised and the same title
    assert first["coverage"]["low"] is True and first["coverage"]["candidates"] == 3
    again = asyncio.run(retrieve_prior_work(m, provider, store=store, client=FakeOpenAlex(), today=TODAY))
    assert again == first  # the stored W snapshot, not a new search
