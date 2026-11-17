"""Literature knowledge base, P0: dates, OpenAlex parsing, schema, store guard, eval set and harness."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from config import settings
from models import PublicationStatus
from services.literature import EMBEDDING_DIMENSIONS, KB_SCHEMA_VERSION
from services.literature import schema
from services.literature.dates import classify_by_date, concurrent_since, resolve_paper_date
from services.literature.eval import harness
from services.literature.openalex import abstract_from_index, short_id, work_from_json
from services.literature.store import LiteratureStoreUnavailable, database_url, journal_key
from services.literature.types import PaperDate, Work

TODAY = dt.date(2026, 11, 14)


# ---------------------------------------------------------------- dates


def test_arxiv_stamp_gives_the_day() -> None:
    d = resolve_paper_date(["arXiv:2306.17702v1 [cs.LG] 30 Jun 2023"], today=TODAY)
    assert (d.date, d.source, d.precision) == (dt.date(2023, 6, 30), "arxiv_stamp", "day")


def test_arxiv_id_alone_gives_the_first_of_the_month() -> None:
    d = resolve_paper_date([], front_text="see arXiv:2306.17702 for details", today=TODAY)
    assert (d.date, d.source, d.precision) == (dt.date(2023, 6, 1), "arxiv_id", "month")


def test_a_year_alone_counts_from_its_start() -> None:
    venue = resolve_paper_date(["The ICML 2023 Workshop on Interpretable Machine Learning in Healthcare (IMLH)."], today=TODAY)
    assert (venue.date, venue.source, venue.precision) == (dt.date(2023, 1, 1), "venue_statement", "year")
    record = resolve_paper_date([], publication=PublicationStatus(status="published", year=2021, journal="J"), today=TODAY)
    assert (record.date, record.source) == (dt.date(2021, 1, 1), "publication_record")


def test_without_any_date_today_is_used() -> None:
    assert resolve_paper_date([], today=TODAY) == PaperDate(TODAY, "today", "day", "")


def test_prior_concurrent_and_later_work() -> None:
    paper = PaperDate(dt.date(2023, 6, 30), "arxiv_stamp", "day")
    assert concurrent_since(paper) == dt.date(2022, 12, 28)
    assert classify_by_date(dt.date(2022, 6, 1), None, paper) == "prior"
    assert classify_by_date(dt.date(2023, 3, 1), None, paper) == "concurrent"
    assert classify_by_date(dt.date(2023, 7, 1), None, paper) == "later"
    # known only by its year: counts from 31 Dec, so a 2023 work is never prior to a mid-2023 paper
    assert classify_by_date(None, 2023, paper) == "later"
    assert classify_by_date(None, 2021, paper) == "prior"
    assert classify_by_date(None, None, paper) == "undated"


# ---------------------------------------------------------------- OpenAlex records


RECORD = {
    "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/ABC", "title": "Graph models for molecules",
    "publication_date": "2022-03-04", "publication_year": 2022, "type": "article",
    "primary_location": {"source": {"id": "https://openalex.org/S9", "display_name": "J Chem Inf Model"},
                         "landing_page_url": "https://arxiv.org/abs/2203.01234"},
    "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "Jane Doe"},
                     "institutions": [{"id": "https://openalex.org/I5"}]}],
    "abstract_inverted_index": {"Graphs": [0], "model": [1], "molecules": [2]},
    "referenced_works": ["https://openalex.org/W7"], "related_works": ["https://openalex.org/W8"],
    "concepts": [{"display_name": "Chemistry"}], "cited_by_count": 12,
}


def test_an_openalex_record_becomes_a_work() -> None:
    w = work_from_json(RECORD)
    assert (w.id, w.doi, w.arxiv_id, w.venue_id, w.year) == ("W1", "10.1000/abc", "2203.01234", "S9", 2022)
    assert w.abstract == "Graphs model molecules" and not w.abstract_missing
    assert w.author_ids == ["A1"] and w.institution_ids == ["I5"] and w.reference_ids == ["W7"]
    assert Work.from_json(w.to_json()) == w


def test_a_work_without_an_abstract_is_flagged_and_embedded_by_title() -> None:
    w = work_from_json({**RECORD, "abstract_inverted_index": None})
    assert w.abstract_missing and w.embedding_text() == "Graph models for molecules"
    assert abstract_from_index(None) == "" and short_id("https://openalex.org/W42") == "W42"


# ---------------------------------------------------------------- schema and store guard


def test_schema_has_one_vector_column_per_size_and_an_index_per_model() -> None:
    ddl = "\n".join(schema.statements())
    for model, dims in EMBEDDING_DIMENSIONS.items():
        assert f"embedding_{dims} vector({dims})" in ddl and f"WHERE model = '{model}'" in ddl
    assert "CHECK (kind IN ('W', 'J'))" in ddl and "PRIMARY KEY (kind, paper_hash, journal_key, model, schema_version)" in ddl
    assert KB_SCHEMA_VERSION.startswith("lit-")


def test_the_store_refuses_supabase_and_the_production_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "")
    with pytest.raises(LiteratureStoreUnavailable):
        database_url()
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@db.x.supabase.co:5432/postgres")
    with pytest.raises(LiteratureStoreUnavailable):
        database_url()
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@localhost:5433/app")
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@localhost:5433/app")
    with pytest.raises(LiteratureStoreUnavailable):
        database_url()
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@localhost:5433/rejecta_local")
    assert database_url().endswith("rejecta_local")


def test_journal_keys_ignore_case_and_punctuation() -> None:
    assert journal_key("Journal of the American  Chemical Society") == journal_key("journal of the american chemical society")
    assert journal_key("Angewandte Chemie (Int. Ed.)") == "angewandte chemie int ed"


def test_the_flag_is_off() -> None:
    assert settings.LITERATURE_KB_ENABLED is False


# ---------------------------------------------------------------- eval set and harness


def test_the_frozen_eval_set_is_balanced_and_usable() -> None:
    version, papers = harness.load_eval_set()
    assert version and len(papers) == 50
    assert sum(p.field == "cs_ml" for p in papers) == 25 and sum(p.field == "chemistry" for p in papers) == 25
    assert len({p.journal_id for p in papers}) == 10
    assert all(len(p.work.reference_ids) >= 20 and not p.work.abstract_missing and p.work.publication_date for p in papers)
    assert len({p.work.id for p in papers}) == 50


def work(wid: str, date: dt.date | None, refs: list[str] | None = None, doi: str | None = None) -> Work:
    return Work(id=wid, title=wid, abstract="a", abstract_missing=False, publication_date=date,
                year=date.year if date else None, reference_ids=refs or [], doi=doi)


def test_the_leak_filter_removes_the_paper_later_work_citers_and_undated_work() -> None:
    paper = work("P", dt.date(2022, 6, 1), refs=["R1", "R2"], doi="10.1/p")
    candidates = [
        work("R1", dt.date(2020, 1, 1)), work("P", dt.date(2022, 6, 1)), work("X", dt.date(2021, 1, 1), doi="10.1/P"),
        work("L", dt.date(2023, 1, 1)), work("C", dt.date(2021, 5, 1), refs=["P"]), work("U", None),
    ]
    assert [c.id for c in harness.leak_filter(candidates, paper)] == ["R1"]


def test_recall_and_top_k() -> None:
    assert harness.recall_at_k(["R1", "Z", "R2"], ["R1", "R2", "R3", "R4"], 2) == 0.25
    assert harness.top_k_hit(["S1", "S2", "S3"], "S3", 3) and not harness.top_k_hit(["S1", "S2", "S3"], "S3", 2)


def test_the_retriever_never_sees_references_and_route_a_is_off() -> None:
    _, papers = harness.load_eval_set()
    seen: list[tuple[harness.EvalQuery, harness.RetrievalOptions]] = []

    async def retriever(query: harness.EvalQuery, options: harness.RetrievalOptions) -> list[Work]:
        seen.append((query, options))
        return []

    report = asyncio.run(harness.reference_recall(retriever, papers[:3]))
    assert report.n == 3 and report.mean == 0.0
    for query, options in seen:
        assert options.use_paper_references is False and options.before == query.date
        assert not hasattr(query, "reference_ids")


def test_a_retriever_that_returns_the_hidden_references_scores_and_leaks_are_dropped() -> None:
    _, papers = harness.load_eval_set()
    paper = papers[0]

    async def oracle(query: harness.EvalQuery, options: harness.RetrievalOptions) -> list[Work]:
        refs = [work(r, dt.date(2000, 1, 1)) for r in paper.work.reference_ids[:10]]
        return [work(paper.work.id, paper.work.publication_date)] + refs  # the paper itself first: removed

    report = asyncio.run(harness.reference_recall(oracle, [paper], k=20))
    assert report.per_paper[0]["recall"] == round(10 / len(paper.work.reference_ids), 4)
