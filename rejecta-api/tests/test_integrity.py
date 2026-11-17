"""Publication integrity gate: file marks, the paper's own DOI, the Crossref record, and what the score may say.

No test here touches the network: a fake web (httpx.MockTransport) answers for Crossref and OpenAlex.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from config import settings
from models import FileMark, IntegrityNotice, ParsedDocument, PublicationStatus
from services import citation_http, integrity
from services import paper_profile as pp
from services.parser import _doi_candidates, _file_marks
from tests.test_analysis_guard import make_ctx, payload, run

TITLE = "A novel chalcone derivative S17 induces apoptosis through ROS dependent DR5 up-regulation in gastric cancer cells"
DOI = "10.1038/s41598-017-10400-3"
FRONT = f"Scientific Reports\n{TITLE}\nSaiyang Zhang, Tao Li, Lingling Zhang\nAbstract ..."


@pytest.fixture(autouse=True)
def fast_clean_state(monkeypatch):
    for name, (concurrency, _gap) in list(citation_http._SOURCES.items()):
        monkeypatch.setitem(citation_http._SOURCES, name, (concurrency, 0.0))
    monkeypatch.setattr(citation_http, "DEFAULT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(citation_http, "COOLDOWN_AFTER_FAILURE_SECONDS", 0.0)
    monkeypatch.setattr(settings, "OPENALEX_API_KEY", "")
    citation_http.reset_limiters()
    integrity.clear_cache()
    yield
    citation_http.reset_limiters()
    integrity.clear_cache()


def work(doi: str = DOI, title: str = TITLE, **extra: Any) -> dict[str, Any]:
    message = {
        "DOI": doi, "title": [title], "type": "journal-article", "container-title": ["Scientific Reports"],
        "publisher": "Springer Nature", "issued": {"date-parts": [[2017, 8, 29]]},
        "author": [{"family": "Zhang"}, {"family": "Li"}],
    }
    message.update(extra)
    return message


RETRACTION = [
    {"DOI": "10.1038/s41598-026-35488-4", "type": "retraction", "label": "Retraction", "source": "retraction-watch",
     "updated": {"date-time": "2026-01-19T00:00:00Z"}, "record-id": "1"},
    {"DOI": "10.1038/s41598-026-35488-4", "type": "retraction", "label": "Retraction", "source": "publisher",
     "updated": {"date-time": "2026-01-19T00:00:00Z"}},
]


class FakeWeb:
    def __init__(self, works: dict[str, dict] | None = None, search: list[dict] | None = None,
                 openalex: dict[str, bool] | None = None, crossref_status: int = 200):
        self.works = {k.lower(): v for k, v in (works or {}).items()}
        self.search = search or []
        self.openalex = openalex or {}
        self.crossref_status = crossref_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.crossref.org":
            if self.crossref_status != 200:
                return httpx.Response(self.crossref_status, json={})
            path = unquote(request.url.path)
            if path.startswith("/works/"):
                found = self.works.get(path[len("/works/"):].lower())
                return httpx.Response(200, json={"message": found}) if found else httpx.Response(404, json={})
            return httpx.Response(200, json={"message": {"items": self.search}})
        if request.url.host == "api.openalex.org":
            doi = unquote(request.url.path).split("doi:", 1)[-1].lower()
            if doi in self.openalex:
                return httpx.Response(200, json={"is_retracted": self.openalex[doi]})
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})


def check(parsed: ParsedDocument, web: FakeWeb) -> PublicationStatus:
    async def go() -> PublicationStatus:
        async with httpx.AsyncClient(transport=httpx.MockTransport(web)) as client:
            return await integrity.check_publication(parsed, client)

    return asyncio.run(go())


def paper(dois: list[str] | None = None, title: str = "", front: str = FRONT, marks: list[FileMark] | None = None) -> ParsedDocument:
    return ParsedDocument(
        full_text=front, abstract="", sections={}, references=[], word_count=100,
        doi_candidates=dois or [], title=title, front_text=front, integrity_marks=marks or [],
    )


# ------------------------------------------------------------------ what the file itself says

def test_a_retracted_article_watermark_on_every_page_is_read() -> None:
    pages = [f"RETRACTED ARTICLE\nPage {n} text of the paper." for n in range(6)]
    marks = _file_marks(pages)
    assert [(m.kind, m.text, m.pages) for m in marks] == [("retracted", "RETRACTED ARTICLE", 6)]


def test_a_paper_about_retractions_is_not_marked_as_retracted() -> None:
    pages = [
        "Analysis of Retracted Manuscripts in Chemistry\nWe discuss retracted manuscripts and the Retraction Watch database.",
        "Most manuscripts were retracted because of misconduct.",
        "(39) Xu, L. et al. Retraction Note: The amine-catalysed coupling. Nat. Catal. 2021, 4, 1089.\n"
        "RETRACTED ARTICLE: an example title cited in the references.",
        "More references.",
    ]
    assert _file_marks(pages) == []


def test_the_papers_own_doi_is_the_one_printed_on_every_page_not_a_cited_one() -> None:
    pages = [
        "Title of the paper\nCite This: ACS Omega 2023\nhttps://doi.org/10.1021/acsomega.3c03689",
        "Body text.\nhttps://doi.org/10.1021/acsomega.3c03689",
        "(12) Kleinert, S. Lancet 2011. doi: 10.1016/S0140-6736(11)60075-5\nhttps://doi.org/10.1021/acsomega.3c03689",
    ]
    assert _doi_candidates(pages, []) == ["10.1021/acsomega.3c03689"]


def test_a_doi_in_xml_metadata_stops_at_the_tag() -> None:
    assert _doi_candidates([""], ["<prism:doi>10.1039/D6CC90216B</prism:doi>"]) == ["10.1039/D6CC90216B"]


# ------------------------------------------------------------------ the publication record

def test_a_retraction_in_crossref_makes_the_paper_retracted_and_names_the_notice_once() -> None:
    web = FakeWeb(works={DOI: work(**{"updated-by": RETRACTION})})
    status = check(paper([DOI]), web)
    assert status.status == "retracted" and status.doi == DOI and status.journal == "Scientific Reports"
    assert len(status.notices) == 1  # publisher and Retraction Watch report the same notice
    assert status.notices[0].date == "2026-01-19" and "publisher" in status.notices[0].source
    assert status.doi_source == "file"


def test_a_doi_whose_title_is_not_on_the_first_page_is_not_this_paper() -> None:
    web = FakeWeb(works={DOI: work(title="A completely different study of polymer brushes", **{"updated-by": RETRACTION})})
    status = check(paper([DOI]), web)
    assert status.status == "not_found" and status.doi is None


def test_without_a_doi_an_exact_title_and_an_author_on_the_page_find_the_record() -> None:
    web = FakeWeb(search=[work(**{"updated-by": RETRACTION})])
    assert check(paper(title=TITLE), web).status == "retracted"
    # the same title without any of its authors on the page is someone else's paper
    assert check(paper(title=TITLE, front=f"{TITLE}\nA. Nobody, B. Someone"), FakeWeb(search=[work()])).status == "not_found"


def test_a_preprint_with_the_same_title_does_not_make_a_manuscript_published() -> None:
    web = FakeWeb(search=[work(doi="10.26434/chemrxiv-2024-abc", type="posted-content")])
    status = check(paper(title=TITLE), web)
    assert status.status == "not_found"
    assert any("preprint" in note for note in status.notes)


def test_a_published_paper_without_notices_is_published() -> None:
    status = check(paper([DOI]), FakeWeb(works={DOI: work()}, openalex={DOI.lower(): False}))
    assert status.status == "published" and status.year == 2017 and status.notes == []


def test_openalex_alone_never_makes_a_paper_retracted() -> None:
    status = check(paper([DOI]), FakeWeb(works={DOI: work()}, openalex={DOI.lower(): True}))
    assert status.status == "published"
    assert any("OpenAlex lists this work as retracted" in note for note in status.notes)


def test_an_expression_of_concern_is_its_own_status() -> None:
    concern = [{"DOI": "10.1/eoc", "type": "expression_of_concern", "source": "publisher",
                "updated": {"date-time": "2024-03-01T00:00:00Z"}}]
    assert check(paper([DOI]), FakeWeb(works={DOI: work(**{"updated-by": concern})})).status == "expression_of_concern"


def test_an_uploaded_retraction_notice_is_recognised_as_one() -> None:
    notice = work(doi="10.1038/s41598-026-35488-4", title=f"Retraction Note: {TITLE}",
                  **{"update-to": [dict(RETRACTION[1], DOI=DOI)]})
    status = check(paper(["10.1038/s41598-026-35488-4"], front=f"Retraction Note: {TITLE}\nZhang"), FakeWeb(works={"10.1038/s41598-026-35488-4": notice}))
    assert status.status == "retracted" and status.is_notice


def test_when_crossref_is_down_the_file_marks_still_decide() -> None:
    mark = FileMark(kind="retracted", text="RETRACTED ARTICLE", pages=7)
    status = check(paper([DOI], marks=[mark]), FakeWeb(crossref_status=500))
    assert status.status == "retracted" and status.sources == ["file"]
    assert check(paper([DOI]), FakeWeb(crossref_status=500)).status == "not_checked"


def test_a_manuscript_with_no_doi_and_no_title_is_not_found_without_any_request() -> None:
    web = FakeWeb()
    assert check(paper(front="Some text"), web).status == "not_found"
    assert web.requests == []


# ------------------------------------------------------------------ what the record decides in the report

def reason(severity: str = "desk_reject") -> dict[str, Any]:
    from tests.test_analysis_guard import PAPER

    words = PAPER.split()[:12]
    return {
        "problem": "The claimed mechanism is not supported by the data shown.",
        "evidence": " ".join(words), "impact": "The central conclusion may not hold for these cells.",
        "solution": "Add the mechanism experiment for the claimed data and conclusion.", "severity": severity,
    }


def retracted_status() -> PublicationStatus:
    return PublicationStatus(
        status="retracted", doi=DOI, journal="Scientific Reports", year=2017,
        notices=[IntegrityNotice(type="retraction", doi="10.1038/s41598-026-35488-4", date="2026-01-19", source="publisher")],
    )


def test_a_retracted_paper_gets_no_recommendation_whatever_the_text_reads_like() -> None:
    out = run(payload(criteria={k: {"score": 90, "reason": "r"} for k, _ in pp.PROFILES[pp.COMMUNICATION].criteria}),
              make_ctx(publication=retracted_status()))
    assert out["mood_score"] <= 5 and out["mood"] == "furious"
    first = out["rejection_reasons"][0]
    assert first["severity"] == "desk_reject" and "retracted on 2026-01-19 by the publisher" in first["problem"]
    assert "https://doi.org/10.1038/s41598-026-35488-4" in first["solution"]
    assert "No recommendation is made" in out["verdict"]
    assert out["oracle_letter"].startswith("Before anything else: this article has been retracted")


def test_an_expression_of_concern_caps_the_score_below_a_recommendation() -> None:
    concern = PublicationStatus(status="expression_of_concern", notices=[IntegrityNotice(type="expression_of_concern")])
    out = run(payload(criteria={k: {"score": 90, "reason": "r"} for k, _ in pp.PROFILES[pp.COMMUNICATION].criteria}),
              make_ctx(publication=concern))
    assert out["mood_score"] <= integrity_cap() and out["rejection_reasons"][0]["severity"] == "major"


def integrity_cap() -> int:
    from services.analysis_guard import CONCERN_SCORE_CAP

    return CONCERN_SCORE_CAP


def test_a_paper_published_in_the_target_journal_is_never_desk_rejected_there() -> None:
    published = PublicationStatus(status="published", doi=DOI, journal="Scientific Reports", year=2017, at_target_journal=True)
    out = run(payload(rejection_reasons=[reason("desk_reject")]), make_ctx(publication=published))
    assert [r["severity"] for r in out["rejection_reasons"]] == ["major"]


def test_a_published_paper_sent_to_another_journal_may_still_be_desk_rejected_for_scope() -> None:
    published = PublicationStatus(status="published", doi=DOI, journal="Scientific Reports", year=2017, at_target_journal=False)
    out = run(payload(rejection_reasons=[reason("desk_reject")]), make_ctx(publication=published))
    assert [r["severity"] for r in out["rejection_reasons"]] == ["desk_reject"]


def test_the_journal_it_appeared_in_is_matched_by_full_or_short_name() -> None:
    status = PublicationStatus(status="published", journal="PLoS ONE", journal_abbreviation="PLoS ONE")
    assert integrity.same_journal(status, "PLOS One", None)
    assert integrity.same_journal(PublicationStatus(status="published", journal="Chemical Communications",
                                                    journal_abbreviation="Chem. Commun."), "chem commun", None)
    assert not integrity.same_journal(status, "Nature", "Nature")


def test_a_manuscript_that_is_not_published_keeps_its_desk_rejection() -> None:
    out = run(payload(rejection_reasons=[reason("desk_reject")]), make_ctx(publication=PublicationStatus(status="not_found")))
    assert [r["severity"] for r in out["rejection_reasons"]] == ["desk_reject"]


# ------------------------------------------------------------------ concerns without evidence, citation coverage

def test_a_reason_that_points_to_no_text_is_kept_apart_as_unverified_not_deleted() -> None:
    lost = dict(reason("major"), evidence="words that are nowhere in this manuscript at all")
    out = run(payload(rejection_reasons=[lost]), make_ctx())
    assert out["rejection_reasons"] == []
    assert [c["problem"] for c in out["unverified_concerns"]] == [lost["problem"]]
    assert out["unverified_concerns"][0]["evidence"] == ""


def test_citation_integrity_weighs_only_as_much_as_the_references_that_could_be_settled() -> None:
    out = run(payload(), make_ctx(citations_total=10, citations_verified=5, citations_ghost=0))
    citation = out["criteria"][pp.CITATION_CRITERION]
    assert citation["score"] == 100 and citation["coverage"] == 0.5
    assert citation["weight"] == pytest.approx(pp.CITATION_WEIGHT * 0.5)
    assert "Coverage 50%" in citation["reason"] and "5 with no matching record" in citation["reason"]


# ------------------------------------------------------------------ end to end through the pipeline

def test_the_pipeline_puts_the_record_in_the_prompt_and_the_report(monkeypatch) -> None:
    import json as _json

    from models import AnalysisResult, CitationResult, CitationStatus, JournalMatch
    from services import pipeline
    from services.llm import base

    seen: dict[str, str] = {}

    class Stub(base.AbstractAnalysisProvider):
        async def analyse(self, abstract, sections, target_journal, citation_summary, journal_data, mode="a"):
            seen["prompt"] = self.build_user_message(abstract, sections, target_journal, citation_summary, journal_data)
            raw = payload(criteria={k: {"score": 88, "reason": "r"} for k, _ in pp.PROFILES[pp.RESEARCH_ARTICLE].criteria})
            raw["provider_used"] = "stub"
            return AnalysisResult.model_validate(self.normalize_analysis_payload(_json.loads(_json.dumps(raw))))

        async def generate(self, system_prompt, user_prompt, temperature=0.2):
            return "{}"

    monkeypatch.setattr(pipeline, "get_provider_cached", lambda: Stub())

    async def verify(refs):
        return [CitationResult(raw=r, status=CitationStatus.verified) for r in refs]

    async def match(abstract, journal):
        return JournalMatch(found=False, target=journal)

    async def check(parsed):
        return retracted_status()

    parsed = paper([DOI])
    parsed.references = ["A. Author, Nature, 2001, 1, 1-2."]
    result = asyncio.run(pipeline.run_analysis(parsed, "Scientific Reports", "a", verify=verify, match=match, check=check))
    assert "this article has been retracted" in seen["prompt"]
    assert result.integrity is not None and result.integrity.status == "retracted"
    assert result.analysis.mood_score <= 5
    assert result.analysis.rejection_reasons[0].severity.value == "desk_reject"


def test_a_failing_status_check_never_fails_the_analysis_and_the_file_marks_still_count(monkeypatch) -> None:
    from services import pipeline

    async def broken(parsed):
        raise RuntimeError("network down")

    mark = FileMark(kind="retracted", text="RETRACTED ARTICLE", pages=5)
    captured: dict[str, Any] = {}

    class Done(Exception):
        pass

    def pipeline_context_publication():
        from services.analysis_guard import current_context

        ctx = current_context()
        return ctx.publication if ctx else None

    class Stub:
        async def analyse(self, **kwargs):
            captured["publication"] = pipeline_context_publication()
            raise Done

    monkeypatch.setattr(pipeline, "get_provider_cached", lambda: Stub())

    async def verify(refs):
        return []

    async def match(abstract, journal):
        from models import JournalMatch

        return JournalMatch(found=False, target=journal)

    with pytest.raises(Done):
        asyncio.run(pipeline.run_analysis(paper([DOI], marks=[mark]), "J", "a", verify=verify, match=match, check=broken))
    assert captured["publication"].status == "retracted"
