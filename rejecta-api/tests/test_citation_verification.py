"""Citation verification: chemistry-style references, fingerprint matching, and "could not check".

No test here touches the network. A fake web (httpx.MockTransport) answers for CrossRef,
Europe PMC, OpenAlex, DataCite and arXiv, so behaviour under rate limits is deterministic.
"""
import asyncio

import httpx
import pytest

from config import settings
from models import CitationStatus
from services import citation_http, citations
from services.citation_refs import (
    candidate_from_crossref,
    compare,
    extract_first_author,
    parse_reference,
)

GRUBBS = "Grubbs, R. H. Angew. Chem. Int. Ed. 2006, 45, 3760-3765."
GRUBBS_ITEM = {
    "DOI": "10.1002/anie.200600680",
    "title": ["Olefin-Metathesis Catalysts for the Preparation of Molecules and Materials (Nobel Lecture)"],
    "author": [{"family": "Grubbs", "given": "Robert H."}],
    "issued": {"date-parts": [[2006, 6, 12]]},
    "volume": "45",
    "page": "3760-3765",
    "container-title": ["Angewandte Chemie International Edition"],
    "short-container-title": ["Angew. Chem. Int. Ed."],
    "type": "journal-article",
    "score": 60.6,
}
KOLB = "Kolb, H. C.; Finn, M. G.; Sharpless, K. B. Angew. Chem. Int. Ed. 2001, 40, 2004-2021."
KOLB_ITEM = {
    "DOI": "10.1002/1521-3773(20010601)40:11<2004::aid-anie2004>3.0.co;2-5",
    "title": ["Click Chemistry: Diverse Chemical Function from a Few Good Reactions"],
    "author": [{"family": "Kolb", "given": "Hartmuth C."}, {"family": "Finn", "given": "M. G."}],
    "issued": {"date-parts": [[2001, 6, 1]]},
    "volume": "40",
    "page": "2004-2021",
    "container-title": ["Angewandte Chemie International Edition"],
    "type": "journal-article",
    "score": 116.7,
}
FAKE = "Smith, J.; Doe, A. J. Am. Chem. Soc. 2019, 141, 99999-99999."
# What CrossRef does for a made-up reference: a plausible-looking, unrelated nearest neighbour.
NEIGHBOUR_ITEM = {
    "DOI": "10.1/neighbour",
    "title": ["A Soap Study"],
    "author": [{"family": "Xxxxxxxxxx"}],
    "issued": {"date-parts": [[2001]]},
    "volume": "99999",
    "page": "99999999-99999999",
    "container-title": ["J. Soc. Cosmet. Chem. Japan"],
    "score": 51.9,
}
# Europe PMC finds a REAL paper by some "Smith" in JACS vol. 141 (2019): same author, year, volume.
EPMC_SMITH = {
    "id": "31000001", "source": "MED", "doi": "10.1021/jacs.9b00001", "title": "A different JACS paper.",
    "authorString": "Smith J, Lee K.", "journalTitle": "J Am Chem Soc", "pubYear": "2019",
    "journalVolume": "141", "pageInfo": "1234-1240",
}


@pytest.fixture(autouse=True)
def fast_clean_state(monkeypatch):
    """No pacing delays, empty cache and limiters, no OpenAlex key unless a test sets one."""
    for name, (concurrency, _gap) in list(citation_http._SOURCES.items()):
        monkeypatch.setitem(citation_http._SOURCES, name, (concurrency, 0.0))
    monkeypatch.setattr(citation_http, "_ANONYMOUS_OPENALEX", (1, 0.0))
    monkeypatch.setattr(citation_http, "DEFAULT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(citation_http, "COOLDOWN_AFTER_FAILURE_SECONDS", 0.0)
    monkeypatch.setattr(settings, "OPENALEX_API_KEY", "")
    citation_http.reset_limiters()
    citations.clear_citation_cache()
    yield
    citation_http.reset_limiters()
    citations.clear_citation_cache()


class FakeWeb:
    """Answers requests per service and records them."""

    def __init__(self, crossref=None, europepmc=None, openalex=None, datacite=None, arxiv=None):
        self.handlers = {
            "api.crossref.org": crossref,
            "www.ebi.ac.uk": europepmc,
            "api.openalex.org": openalex,
            "api.datacite.org": datacite,
            "export.arxiv.org": arxiv,
        }
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handlers.get(request.url.host)
        return handler(request) if handler else httpx.Response(404, json={})

    def calls(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def crossref_items(*items):
    return lambda request: httpx.Response(200, json={"message": {"items": list(items)}})


def run(web: FakeWeb, refs: list[str]):
    async def go():
        async with web.client() as client:
            return await citations.verify_citations(refs, client=client)

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Reading references
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ref, author, year, volume, page", [
    ("Grubbs, R. H. Angew. Chem. Int. Ed. 2006, 45, 3760-3765.", "grubbs", 2006, "45", "3760"),
    ("R. H. Grubbs, Angew. Chem., Int. Ed., 2006, 45, 3760.", "grubbs", 2006, "45", "3760"),
    ("[3] Grubbs RH. Angew Chem Int Ed. 2006;45(23):3760-5.", "grubbs", 2006, "45", "3760"),
    ("Grubbs, R. H. (2006). Olefin-metathesis catalysts. Angew. Chem., 45(23), 3760-3765.", "grubbs", 2006, "45", "3760"),
    ("R. H. Grubbs, Angew. Chem. Int. Ed. 45 (2006) 3760-3765.", "grubbs", 2006, "45", "3760"),
    ("N. Wedler-Jasinski, T. Lueckerath and C. Barner-Kowollik, Chem. Commun., 2017, 53, 157-160.", "wedlerjasinski", 2017, "53", "157"),
    ("Kolb HC, Finn MG, Sharpless KB. Angew Chem Int Ed. 2001;40:2004-2021.", "kolb", 2001, "40", "2004"),
])
def test_reference_styles_give_the_same_fingerprint(ref, author, year, volume, page):
    parsed = parse_reference(ref)
    assert (parsed.first_author, parsed.year, parsed.volume, parsed.first_page) == (author, year, volume, page)


def test_year_volume_and_pages_are_never_mistaken_for_a_title():
    # The old guesser returned "2006, 45, 3760-3765" as the title, which sank every chemistry reference.
    assert parse_reference(GRUBBS).title is None
    assert parse_reference("R. H. Grubbs, Angew. Chem., Int. Ed., 2006, 45, 3760.").title is None


def test_a_real_title_is_still_found():
    kolb = "Kolb, H. C., Finn, M. G., Sharpless, K. B. Click Chemistry: Diverse Chemical Function from a Few Good Reactions. Angew. Chem. Int. Ed. 2001, 40, 2004-2021."
    assert parse_reference(kolb).title == "Click Chemistry: Diverse Chemical Function from a Few Good Reactions"
    vaswani = "Vaswani, A.; Shazeer, N. Attention is all you need. In Advances in Neural Information Processing Systems, 2017, 30, 5998-6008."
    assert parse_reference(vaswani).title == "Attention is all you need"


@pytest.mark.parametrize("text, expected", [
    ("Wedler-Jasinski, N.; Lueckerath, T.", "wedlerjasinski"),
    ("van der Waals, J. D.", "waals"),
    ("Kolb HC, Finn MG", "kolb"),
    ("H. C. Kolb and M. G. Finn", "kolb"),
    ("Müller, K.; Lee, J.", "muller"),
])
def test_first_author_layouts(text, expected):
    assert extract_first_author(text) == expected


# ---------------------------------------------------------------------------
# Fingerprint rules
# ---------------------------------------------------------------------------
def test_matching_fingerprint_is_accepted():
    report = compare(parse_reference(GRUBBS), candidate_from_crossref(GRUBBS_ITEM))
    assert report.verified and report.fingerprint and report.confidence >= 0.93


def test_online_first_year_off_by_one_is_accepted():
    item = {**GRUBBS_ITEM, "issued": {"date-parts": [[2005]]}}
    assert compare(parse_reference(GRUBBS), candidate_from_crossref(item)).verified


def test_same_author_year_and_volume_but_a_different_page_is_not_accepted():
    # Hundreds of papers share "Smith, 2019, JACS 141". Only the page tells them apart.
    smith_other = {**GRUBBS_ITEM, "author": [{"family": "Smith"}], "issued": {"date-parts": [[2019]]},
                   "volume": "141", "page": "1234-1240"}
    report = compare(parse_reference(FAKE), candidate_from_crossref(smith_other))
    assert report.author and report.year and report.volume
    assert report.page_conflict and not report.verified


def test_a_conflicting_year_rules_the_record_out_even_if_the_page_matches():
    item = {**GRUBBS_ITEM, "issued": {"date-parts": [[1999]]}}
    assert not compare(parse_reference(GRUBBS), candidate_from_crossref(item)).verified


def test_without_a_page_on_the_record_the_journal_name_must_agree():
    no_page = {k: v for k, v in GRUBBS_ITEM.items() if k != "page"}
    assert compare(parse_reference(GRUBBS), candidate_from_crossref(no_page)).verified  # journal is in the cited line
    other_journal = {**no_page, "container-title": ["Nature"], "short-container-title": ["Nature"]}
    assert not compare(parse_reference(GRUBBS), candidate_from_crossref(other_journal)).verified


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------
def test_a_title_less_chemistry_reference_is_verified_by_its_fingerprint():
    web = FakeWeb(crossref=crossref_items(GRUBBS_ITEM))
    [result] = run(web, [GRUBBS])
    assert result.status == CitationStatus.verified
    assert result.doi == "10.1002/anie.200600680"
    assert not result.check_incomplete
    assert "first author" in result.diagnostic_reason and "first page" in result.diagnostic_reason


def test_crossref_gets_the_whole_reference_a_year_window_and_a_polite_mailto():
    web = FakeWeb(crossref=crossref_items(GRUBBS_ITEM))
    run(web, [GRUBBS])
    params = web.calls("api.crossref.org")[0].url.params
    assert params["query.bibliographic"] == GRUBBS.rstrip()
    assert params["filter"] == "from-pub-date:2005,until-pub-date:2007"
    assert "mailto" in params


def test_the_correct_paper_is_found_even_when_it_is_not_the_first_hit():
    web = FakeWeb(crossref=crossref_items(NEIGHBOUR_ITEM, KOLB_ITEM, GRUBBS_ITEM))
    [result] = run(web, [GRUBBS])
    assert result.status == CitationStatus.verified and result.doi == GRUBBS_ITEM["DOI"]


def test_a_fabricated_reference_stays_unverified_even_when_catalogs_return_look_alikes():
    web = FakeWeb(
        crossref=crossref_items(NEIGHBOUR_ITEM),
        europepmc=lambda r: httpx.Response(200, json={"resultList": {"result": [EPMC_SMITH]}}),
        openalex=lambda r: httpx.Response(200, json={"results": []}),
    )
    [result] = run(web, [FAKE])
    assert result.status == CitationStatus.unverified
    assert not result.check_incomplete  # it WAS checked: nothing matches
    assert result.doi is None and result.title is None  # the look-alike was not adopted as the reference
    # The author is told what was close and why it was rejected: only the page differs.
    assert "differs from the cited reference in first page" in result.diagnostic_reason


def test_europe_pmc_verifies_a_journal_article_and_a_chemrxiv_preprint():
    record = {
        "id": "PPR1", "source": "PPR", "doi": "10.26434/chemrxiv-2023-abcde",
        "title": "A photoreduction study", "authorString": "Mutlu H, Blinco JP.", "pubYear": "2023",
        "bookOrReportDetails": {"publisher": "ChemRxiv"},
    }
    web = FakeWeb(
        crossref=crossref_items(),
        europepmc=lambda r: httpx.Response(200, json={"resultList": {"result": [record]}}),
    )
    ref = "Mutlu, H.; Blinco, J. P. A photoreduction study. ChemRxiv 2023."
    [result] = run(web, [ref])
    assert result.status == CitationStatus.verified
    assert result.is_preprint and result.venue == "ChemRxiv"
    assert result.doi == "10.26434/chemrxiv-2023-abcde"


def test_a_journal_style_reference_is_not_sent_to_arxiv():
    web = FakeWeb(crossref=crossref_items())
    run(web, [FAKE])
    assert not web.calls("export.arxiv.org")


# ---------------------------------------------------------------------------
# "Could not check" is not "not found"
# ---------------------------------------------------------------------------
def test_a_rate_limited_lookup_is_reported_as_not_checked_not_as_not_found():
    busy = lambda r: httpx.Response(429, headers={"Retry-After": "30"}, json={})
    web = FakeWeb(crossref=busy, europepmc=busy, openalex=busy)
    [result] = run(web, [GRUBBS])
    assert result.status == CitationStatus.unverified
    assert result.check_incomplete is True
    assert "could not complete the check" in result.diagnostic_reason.lower()
    assert "not mean the reference is wrong" in result.diagnostic_reason.lower()


def test_a_long_retry_after_pauses_the_service_for_every_reference_instead_of_hammering_it():
    web = FakeWeb(crossref=lambda r: httpx.Response(429, headers={"Retry-After": "30"}, json={}))
    refs = [f"Author{n}, A. J. Chem. Soc. 2010, {n}, {100 + n}-{110 + n}." for n in range(1, 13)]
    results = run(web, refs)
    assert all(r.check_incomplete for r in results)
    assert len(web.calls("api.crossref.org")) <= 3  # a handful of first attempts, not 12 x retries


def test_a_short_retry_after_is_waited_out_and_the_lookup_succeeds():
    state = {"calls": 0}

    def flaky(request):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"message": {"items": [GRUBBS_ITEM]}})

    [result] = run(FakeWeb(crossref=flaky), [GRUBBS])
    assert result.status == CitationStatus.verified
    assert state["calls"] == 2


def test_a_dead_doi_is_a_ghost_only_when_the_services_really_say_no_such_doi():
    ref = "Nobody, A. Imaginary results. 10.1234/does.not.exist"
    web = FakeWeb(crossref=lambda r: httpx.Response(404, json={}), datacite=lambda r: httpx.Response(404, json={}))
    assert run(web, [ref])[0].status == CitationStatus.ghost


def test_a_doi_that_could_not_be_looked_up_is_never_called_a_ghost():
    ref = "Nobody, A. Imaginary results. 10.1234/does.not.exist"
    web = FakeWeb(crossref=lambda r: httpx.Response(429, headers={"Retry-After": "30"}, json={}))
    [result] = run(web, [ref])
    assert result.status == CitationStatus.unverified and result.check_incomplete


def test_datacite_being_down_never_turns_a_missing_crossref_doi_into_a_ghost():
    ref = "Nobody, A. Dataset. 10.5281/zenodo.123456"
    web = FakeWeb(
        crossref=lambda r: httpx.Response(404, json={}),
        datacite=lambda r: httpx.Response(503, json={}),
    )
    [result] = run(web, [ref])
    assert result.status == CitationStatus.unverified and result.check_incomplete


def test_a_service_that_times_out_is_reported_as_not_checked():
    def hang(request):
        raise httpx.ReadTimeout("slow", request=request)

    [result] = run(FakeWeb(crossref=hang, europepmc=hang, openalex=hang), [GRUBBS])
    assert result.status == CitationStatus.unverified and result.check_incomplete


def test_running_out_of_time_marks_the_rest_as_not_checked(monkeypatch):
    monkeypatch.setattr(citations, "TOTAL_BUDGET_SECONDS", 0.2)

    async def go():
        async def slow(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json={"message": {"items": []}})

        web = FakeWeb(crossref=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await citations.verify_citations([GRUBBS], client=client)

    [result] = asyncio.run(go())
    assert result.check_incomplete and "took too long" in result.diagnostic_reason


# ---------------------------------------------------------------------------
# Load: dedupe and cache
# ---------------------------------------------------------------------------
def test_identical_references_in_one_paper_are_looked_up_once():
    web = FakeWeb(crossref=crossref_items(GRUBBS_ITEM))
    results = run(web, [GRUBBS, GRUBBS, f"[7] {GRUBBS}"])
    assert [r.status for r in results] == [CitationStatus.verified] * 3
    assert len(web.calls("api.crossref.org")) == 1
    assert results[2].raw.startswith("[7]")  # each keeps its own text


def test_answers_are_cached_across_papers():
    web = FakeWeb(crossref=crossref_items(GRUBBS_ITEM))
    run(web, [GRUBBS])
    run(web, [GRUBBS])
    assert len(web.calls("api.crossref.org")) == 1


def test_a_reference_that_could_not_be_checked_is_not_cached():
    busy_then_ok = {"phase": "busy"}

    def crossref(request):
        if busy_then_ok["phase"] == "busy":
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"message": {"items": [GRUBBS_ITEM]}})

    web = FakeWeb(crossref=crossref, europepmc=lambda r: httpx.Response(503, json={}), openalex=lambda r: httpx.Response(503, json={}))
    assert run(web, [GRUBBS])[0].check_incomplete
    busy_then_ok["phase"] = "ok"
    citation_http.reset_limiters()
    assert run(web, [GRUBBS])[0].status == CitationStatus.verified


# ---------------------------------------------------------------------------
# OpenAlex key
# ---------------------------------------------------------------------------
OPENALEX_WORK = {
    "title": "Olefin-Metathesis Catalysts for the Preparation of Molecules and Materials (Nobel Lecture)",
    "doi": "https://doi.org/10.1002/anie.200600680", "publication_year": 2006, "type": "article",
    "authorships": [{"author": {"display_name": "Robert H. Grubbs"}}],
    "biblio": {"volume": "45", "first_page": "3760", "last_page": "3765"},
    "primary_location": {"source": {"display_name": "Angewandte Chemie International Edition", "type": "journal"}},
}


def test_the_openalex_key_is_sent_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "OPENALEX_API_KEY", "test-key-123")
    web = FakeWeb(crossref=crossref_items(), openalex=lambda r: httpx.Response(200, json={"results": [OPENALEX_WORK]}))
    [result] = run(web, [GRUBBS])
    assert result.status == CitationStatus.verified and "OpenAlex" in result.diagnostic_reason
    assert web.calls("api.openalex.org")[0].url.params["api_key"] == "test-key-123"


def test_without_a_key_openalex_is_called_anonymously():
    web = FakeWeb(crossref=crossref_items(), openalex=lambda r: httpx.Response(200, json={"results": []}))
    run(web, [GRUBBS])
    assert "api_key" not in web.calls("api.openalex.org")[0].url.params


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_the_summary_keeps_not_found_and_not_checked_apart():
    ok = citations._unverified("a", diagnostic="x")
    busy = citations._unverified("b", diagnostic="y", incomplete=True)
    summary = citations.build_citation_summary([ok, busy, busy])
    assert (summary.unverified, summary.check_incomplete) == (3, 2)
    text = " ".join(summary.editorial_warnings)
    assert "1 reference(s) were checked but no matching record" in text
    assert "NOT CHECKED: 2 reference(s)" in text
    assert "Run the analysis again" in text


def test_a_summary_with_nothing_unchecked_has_no_not_checked_warning():
    summary = citations.build_citation_summary([citations._unverified("a", diagnostic="x")])
    assert summary.check_incomplete == 0
    assert "NOT CHECKED" not in " ".join(summary.editorial_warnings)
