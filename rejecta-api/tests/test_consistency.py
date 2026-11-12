"""Stage 2: the paper's own values compared by code, cell lines in Cellosaurus, and the disclosure statements.

The model is replaced by a fake that returns a fixed extraction, and Cellosaurus by a fake web, so these tests
check the code's comparisons, not a model. No network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from models import ConsistencyFinding, ConsistencyQuote, ConsistencyReport, ParsedDocument
from services import citation_http, consistency
from services import paper_profile as pp
from services.consistency import check_consistency, unit_family
from services.disclosures import read_disclosures
from tests.test_analysis_guard import PAPER, make_ctx, payload, run


@pytest.fixture(autouse=True)
def fast_clean_state(monkeypatch):
    for name, (concurrency, _gap) in list(citation_http._SOURCES.items()):
        monkeypatch.setitem(citation_http._SOURCES, name, (concurrency, 0.0))
    monkeypatch.setattr(citation_http, "DEFAULT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(citation_http, "COOLDOWN_AFTER_FAILURE_SECONDS", 0.0)
    citation_http.reset_limiters()
    consistency.clear_cache()
    yield
    citation_http.reset_limiters()
    consistency.clear_cache()


class FakeModel:
    """Answers the extraction prompt with a fixed JSON, and records that it was asked."""

    model = "fake"

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls = 0

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        self.calls += 1
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def cellosaurus(entries: dict[str, dict[str, Any]]):
    """Search answers with the entries for the name (a derived line first, as the real service ranks them);
    /cell-line/<accession> answers with that entry's comments."""
    derived = entry("SOMETHING-CD81", "CVCL_DERIVED", synonyms=("x",))
    by_accession = {e["accession-list"][0]["value"]: e for e in entries.values()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/cell-line/"):
            found = by_accession.get(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"Cellosaurus": {"cell-line-list": [found]}}) if found else httpx.Response(404)
        query = request.url.params.get("q", "")
        for name, item in entries.items():
            if name in query:
                return httpx.Response(200, json={"Cellosaurus": {"cell-line-list": [derived, item]}})
        return httpx.Response(200, json={"Cellosaurus": {"cell-line-list": []}})

    return handler


def entry(identifier: str, accession: str, problem: str | None = None, synonyms: tuple[str, ...] = ()) -> dict[str, Any]:
    comments = [{"category": "Population", "value": "Caucasian"}]
    if problem:
        comments += [{"category": "Problematic cell line", "value": problem}] * 2  # Cellosaurus repeats it
    return {
        "accession-list": [{"type": "primary", "value": accession}],
        "name-list": [{"type": "identifier", "value": identifier}] + [{"type": "synonym", "value": s} for s in synonyms],
        "comment-list": comments,
    }


def check(parsed: ParsedDocument, answer: dict[str, Any], cells: dict[str, dict[str, Any]] | None = None) -> ConsistencyReport:
    async def go() -> ConsistencyReport:
        transport = httpx.MockTransport(cellosaurus(cells or {}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_consistency(parsed, FakeModel(answer), client)

    return asyncio.run(go())


def doc(abstract: str, sections: dict[str, tuple[str, str]], article_type: str | None = pp.RESEARCH_ARTICLE, title: str = "") -> ParsedDocument:
    texts = {name: text for name, (_, text) in sections.items()}
    roles = {name: role for name, (role, _) in sections.items()}
    full = "\n".join([title, abstract, *texts.values()])
    return ParsedDocument(
        full_text=full, abstract=abstract, sections=texts, references=[], word_count=len(full.split()),
        section_roles=roles, article_type=article_type, title=title, body=full,
    )


# ------------------------------------------------------------------ a paper like test paper 2 (oleanolic acid)

OA_ABSTRACT = "Oleanolic acid induced cell cycle arrest at the sub-G1 (apoptotic) phase in HepG2 human hepatocellular carcinoma cells."
OA_METHODS = (
    "HepG2 cells were cultured at 37°C in a humidified atmosphere with 50 µg/ml CO2. "
    "Cells were seeded in 96-well plates at a density of 3×10^6 cells per well for the MTT assay. "
    "For cell cycle analysis the cells were treated with oleanolic acid for 48 h before staining. "
    "Cells treated or untreated with andrographolide and its analogues were collected for apoptosis quantification. "
    "DNA fragmentation was measured after treatment with 5, 25 and 25 µM oleanolic acid."
)
OA_RESULTS = (
    "Flow cytometry after 24 h of treatment showed that oleanolic acid caused G0/G1 cell cycle arrest. "
    "DNA fragmentation increased in cells treated with 5, 25 and 50 µM oleanolic acid."
)
OA = doc(OA_ABSTRACT, {"methods": ("methods", OA_METHODS), "results": ("results", OA_RESULTS)},
         title="Anticancer activity of oleanolic acid in HepG2 cells")
OA_EXTRACTION = {
    "studied_compounds": ["oleanolic acid"],
    "facts": [
        {"experiment": "cell cycle analysis", "parameter": "cell_cycle_phase", "values": [], "value_text": "sub-G1",
         "unit": "", "quote": "induced cell cycle arrest at the sub-G1 (apoptotic) phase"},
        {"experiment": "cell cycle analysis", "parameter": "cell_cycle_phase", "values": [], "value_text": "G0/G1",
         "unit": "", "quote": "oleanolic acid caused G0/G1 cell cycle arrest"},
        {"experiment": "cell cycle analysis", "parameter": "duration", "values": [48], "unit": "h",
         "quote": "the cells were treated with oleanolic acid for 48 h before staining"},
        {"experiment": "cell cycle analysis", "parameter": "duration", "values": [24], "unit": "h",
         "quote": "Flow cytometry after 24 h of treatment showed"},
        {"experiment": "dna fragmentation", "parameter": "concentration", "values": [5, 25, 25], "unit": "µM",
         "quote": "DNA fragmentation was measured after treatment with 5, 25 and 25 µM oleanolic acid"},
        {"experiment": "dna fragmentation", "parameter": "concentration", "values": [5, 25, 50], "unit": "µM",
         "quote": "DNA fragmentation increased in cells treated with 5, 25 and 50 µM oleanolic acid"},
        {"experiment": "MTT assay", "parameter": "cell_density", "values": [3000000], "unit": "cells per well",
         "quote": "seeded in 96-well plates at a density of 3×10^6 cells per well"},
        {"experiment": "cell culture", "parameter": "co2", "values": [50], "unit": "µg/ml",
         "quote": "in a humidified atmosphere with 50 µg/ml CO2"},
        {"experiment": "cell culture", "parameter": "temperature", "values": [37], "unit": "°C",
         "quote": "HepG2 cells were cultured at 37°C in a humidified atmosphere"},
        # made up by the model: not in the paper, so it must be dropped
        {"experiment": "MTT assay", "parameter": "duration", "values": [72], "unit": "h",
         "quote": "cells were incubated with the compound for 72 h in the dark"},
    ],
    "percent_claims": [],
    "treatments": [
        {"compound": "oleanolic acid", "role": "studied", "quote": "the cells were treated with oleanolic acid for 48 h"},
        {"compound": "andrographolide", "role": "unexplained",
         "quote": "Cells treated or untreated with andrographolide and its analogues were collected"},
    ],
    "cell_lines": [{"name": "HepG2", "quote": "HepG2 cells were cultured at 37°C"}],
}


def kinds(report: ConsistencyReport) -> list[str]:
    return [f.kind for f in report.findings]


def test_the_abstract_and_the_results_naming_different_cell_cycle_phases_is_found() -> None:
    report = check(OA, OA_EXTRACTION, {"HepG2": entry("Hep-G2", "CVCL_0027", synonyms=("HepG2",))})
    phase = next(f for f in report.findings if f.kind == "category_mismatch")
    assert "sub-G1" in phase.message and "G0/G1" in phase.message
    assert {q.section for q in phase.quotes} == {"abstract", "results"}


def test_a_treatment_time_or_a_concentration_that_differs_from_the_methods_is_found() -> None:
    report = check(OA, OA_EXTRACTION)
    messages = [f.message for f in report.findings if f.kind == "value_mismatch"]
    assert any("24 h" in m and "48" in m for m in messages)
    assert any("50 µM" in m and "5, 25" in m for m in messages)


def test_implausible_seeding_a_wrong_co2_unit_and_a_foreign_compound_are_found() -> None:
    report = check(OA, OA_EXTRACTION)
    assert "implausible_value" in kinds(report)
    assert any("3 × 10^6 cells per well" in f.message for f in report.findings)
    assert any(f.kind == "unit_mismatch" and "CO2" in f.message for f in report.findings)
    foreign = next(f for f in report.findings if f.kind == "foreign_compound")
    assert "andrographolide" in foreign.message and foreign.quotes[0].section == "methods"


def test_a_treatment_time_the_model_missed_in_the_results_is_read_by_code() -> None:
    answer = dict(OA_EXTRACTION, facts=[f for f in OA_EXTRACTION["facts"] if "after 24 h" not in f["quote"]])
    report = check(OA, answer)
    time = next(f for f in report.findings if f.kind == "value_mismatch" and "24 h" in f.message)
    assert time.quotes[1].quote.startswith("Flow cytometry after 24 h of treatment")
    assert time.quotes[1].section == "results"


def test_the_phase_of_an_arrest_is_read_by_code_even_when_the_model_lists_no_phase() -> None:
    answer = dict(OA_EXTRACTION, facts=[f for f in OA_EXTRACTION["facts"] if f["parameter"] != "cell_cycle_phase"])
    report = check(OA, answer)
    phases = [f for f in report.findings if f.kind == "category_mismatch"]
    assert len(phases) == 1 and "sub-G1" in phases[0].message and "G0/G1" in phases[0].message


def test_the_same_phase_mismatch_read_by_the_model_and_by_code_is_one_finding() -> None:
    assert [f.kind for f in check(OA, OA_EXTRACTION).findings].count("category_mismatch") == 1


def test_a_model_concern_repeating_a_code_finding_is_shown_once() -> None:
    from tests.test_scoring_policy import concern

    words = " ".join(PAPER.split()[:12])
    report = ConsistencyReport(status="checked", findings=[ConsistencyFinding(
        kind="value_mismatch", severity="major", message="The time differs.",
        quotes=[ConsistencyQuote(quote=words, section="methods")])])
    out = run(payload(criteria=criteria_for_research(), rejection_reasons=[concern("major", "rigor", evidence=words)]),
              make_ctx(article_type=pp.RESEARCH_ARTICLE, consistency=report))
    assert [r["origin"] for r in out["rejection_reasons"]] == ["consistency"]
    assert any("shown once" in note for note in out["grounding_notes"])


def criteria_for_research():
    from tests.test_analysis_guard import criteria

    return criteria(80)


def test_one_percent_claim_about_two_doses_is_one_finding() -> None:
    claim = A1_EXTRACTION["percent_claims"][0]
    answer = dict(A1_EXTRACTION, percent_claims=[claim, dict(claim, value=100)])
    assert [f.kind for f in check(A1, answer).findings].count("percent_claim") == 1


def test_a_value_the_model_made_up_is_dropped_and_every_finding_quotes_the_paper() -> None:
    report = check(OA, OA_EXTRACTION)
    # 9 of the model's 10 facts (the 72 h one is not in the paper) and the 2 arrest phases code reads itself
    assert report.status == "checked" and report.facts_checked == 11
    assert not any("72" in q.values for f in report.findings for q in f.quotes)
    for finding in report.findings:
        for quote in finding.quotes:
            assert consistency._norm(quote.quote) in consistency._norm(OA.full_text)


def test_a_cell_line_with_no_known_problem_is_not_a_finding() -> None:
    report = check(OA, OA_EXTRACTION, {"HepG2": entry("Hep-G2", "CVCL_0027", synonyms=("HepG2",))})
    assert [(c.name, c.status, c.accession) for c in report.cell_lines] == [("HepG2", "no_known_problem", "CVCL_0027")]
    assert "problematic_cell_line" not in kinds(report)


# ------------------------------------------------------------------ a paper like test paper 1 (AECHL-1)

A1_METHODS = (
    "The maximum tolerated dose (MTD) of AECHL-1 was 0.5 mg/kg in BALB/c mice. "
    "Mice received AECHL-1 at 50 and 100 mg/kg/day, which is 10-20% of the MTD, for 21 days. "
    "In the MCF-7 model AECHL-1 was given at 0.02 mg/mouse every other day."
)
A1_ABSTRACT = "AECHL-1 (molecular weight 543.8) is a triterpenoid from Ailanthus excelsa with anticancer activity."
A1_DISCUSSION = "AECHL-1, with a molecular weight of 453.8, disrupted microtubules in treated cells."
A1 = doc(A1_ABSTRACT, {"methods": ("methods", A1_METHODS), "discussion": ("discussion", A1_DISCUSSION)})
A1_EXTRACTION = {
    "studied_compounds": ["AECHL-1"],
    "facts": [
        {"experiment": "aechl-1 characterisation", "parameter": "molecular_weight", "values": [543.8], "unit": "",
         "quote": "AECHL-1 (molecular weight 543.8) is a triterpenoid"},
        {"experiment": "aechl-1 characterisation", "parameter": "molecular_weight", "values": [453.8], "unit": "",
         "quote": "with a molecular weight of 453.8, disrupted microtubules"},
        {"experiment": "xenograft dosing", "parameter": "dose", "values": [50, 100], "unit": "mg/kg/day",
         "quote": "Mice received AECHL-1 at 50 and 100 mg/kg/day"},
        {"experiment": "xenograft dosing", "parameter": "dose", "values": [0.02], "unit": "mg/mouse",
         "quote": "AECHL-1 was given at 0.02 mg/mouse every other day"},
    ],
    "percent_claims": [
        {"quote": "50 and 100 mg/kg/day, which is 10-20% of the MTD", "value": 50, "unit": "mg/kg",
         "percent_low": 10, "percent_high": 20, "of_value": 0.5, "of_unit": "mg/kg",
         "of_quote": "The maximum tolerated dose (MTD) of AECHL-1 was 0.5 mg/kg"},
    ],
    "treatments": [],
    "cell_lines": [],
}


def test_a_dose_said_to_be_a_percent_of_the_mtd_is_checked_against_the_mtd() -> None:
    report = check(A1, A1_EXTRACTION)
    claim = next(f for f in report.findings if f.kind == "percent_claim")
    assert "10-20%" in claim.message and "10,000%" in claim.message


def test_two_molecular_weights_for_one_compound_and_mixed_dose_units_are_found() -> None:
    report = check(A1, A1_EXTRACTION)
    weight = next(f for f in report.findings if f.kind == "value_mismatch")
    assert "543.8" in weight.message and "453.8" in weight.message
    assert any(f.kind == "unit_mismatch" and "per animal" in f.message for f in report.findings)


# ------------------------------------------------------------------ a paper like test paper 4 (S17)

def test_a_cell_line_cellosaurus_lists_as_contaminated_is_a_major_finding() -> None:
    methods = "Human gastric cancer cell lines SGC-7901 and MGC-803 were purchased from the cell bank and cultured in RPMI-1640."
    paper = doc("S17 inhibits gastric cancer cells through ROS.", {"methods": ("methods", methods)})
    answer = {"studied_compounds": ["S17"], "facts": [], "percent_claims": [], "treatments": [],
              "cell_lines": [{"name": "SGC-7901", "quote": "Human gastric cancer cell lines SGC-7901 and MGC-803"},
                             {"name": "AGS", "quote": "Human gastric cancer cell lines SGC-7901 and MGC-803"}]}
    hela = "Contaminated. Shown to be a HeLa derivative (PubMed=26116706)."
    report = check(paper, answer, {"SGC-7901": entry("SGC-7901", "CVCL_0520", hela)})
    line = report.cell_lines[0]
    assert line.status == "problematic" and line.accession == "CVCL_0520" and line.note.count("HeLa") == 1
    finding = next(f for f in report.findings if f.kind == "problematic_cell_line")
    assert "HeLa derivative" in finding.message and finding.severity == "major"
    # a name the quotation does not contain is not read as a cell line of this paper
    assert [c.name for c in report.cell_lines] == ["SGC-7901"]


def test_a_misclassified_cell_line_is_a_minor_finding_that_quotes_cellosaurus() -> None:
    note = "Misclassified. Originally thought to be a hepatocellular carcinoma cell line but shown to be from an hepatoblastoma."
    report = check(OA, OA_EXTRACTION, {"HepG2": entry("Hep-G2", "CVCL_0027", note, synonyms=("HepG2",))})
    finding = next(f for f in report.findings if f.kind == "problematic_cell_line")
    assert finding.severity == "minor" and "hepatoblastoma" in finding.message


def test_a_cell_line_without_an_exact_cellosaurus_record_is_not_found_never_contaminated() -> None:
    methods = "The XYZ-99 cells were grown in DMEM with 10% serum for all assays reported here."
    paper = doc("Abstract.", {"methods": ("methods", methods)})
    answer = {"facts": [], "cell_lines": [{"name": "XYZ-99", "quote": "The XYZ-99 cells were grown in DMEM"}]}
    near = {"XYZ-99": entry("XYZ-99/B1", "CVCL_X", "Contaminated. Parent is HeLa.")}  # a near name, not this line
    report = check(paper, answer, near)
    assert report.cell_lines[0].status == "not_found" and "problematic_cell_line" not in kinds(report)


# ------------------------------------------------------------------ what must not be flagged

def test_a_consistent_paper_with_a_positive_control_has_no_findings() -> None:
    methods = "Cells were treated with 10, 20 and 40 µM of compound K for 24 h. Cisplatin at 10 µM served as positive control."
    results = "After 24 h, 40 µM compound K reduced viability more than 10 µM cisplatin did."
    paper = doc("Compound K kills cancer cells.", {"methods": ("methods", methods), "results": ("results", results)})
    answer = {
        "studied_compounds": ["compound K"],
        "facts": [
            {"experiment": "viability", "parameter": "duration", "values": [24], "unit": "h", "quote": "of compound K for 24 h"},
            {"experiment": "viability", "parameter": "duration", "values": [24], "unit": "h", "quote": "After 24 h, 40 µM compound K"},
            {"experiment": "viability", "parameter": "concentration", "values": [10, 20, 40], "unit": "µM",
             "quote": "treated with 10, 20 and 40 µM of compound K"},
            {"experiment": "viability", "parameter": "concentration", "values": [40], "unit": "μM",
             "quote": "After 24 h, 40 µM compound K reduced viability"},
        ],
        "treatments": [{"compound": "cisplatin", "role": "positive_control", "quote": "Cisplatin at 10 µM served as positive control"}],
    }
    assert check(paper, answer).findings == []


def test_reviews_and_editorials_are_not_checked_and_the_model_is_not_asked() -> None:
    model = FakeModel({})
    paper = doc("A review.", {}, article_type=pp.REVIEW)
    report = asyncio.run(check_consistency(paper, model))
    assert report.status == "not_applicable" and model.calls == 0


def test_a_failing_model_never_fails_the_analysis() -> None:
    class Broken(FakeModel):
        async def generate(self, *args, **kwargs):
            raise RuntimeError("model down")

    report = asyncio.run(check_consistency(OA, Broken({})))
    assert report.status == "not_checked"


def test_units_are_read_in_their_common_spellings() -> None:
    assert unit_family("µM") == ("molar", 1e-6) == unit_family("uM") == unit_family("μM")
    assert unit_family("mg kg⁻¹")[0] == "dose_per_kg"
    assert unit_family("mg/mouse")[0] == "dose_per_animal"
    assert unit_family("°C")[0] == "temperature" and unit_family("hours")[1] == 1


# ------------------------------------------------------------------ in the report

def finding_on_paper(kind: str = "value_mismatch", severity: str = "major") -> ConsistencyFinding:
    words = " ".join(PAPER.split()[:10])
    return ConsistencyFinding(kind=kind, severity=severity, message="The treatment time is 48 h in the Methods but 24 h in the Results.",
                              quotes=[ConsistencyQuote(quote=words, section="methods", values="48 h")])


def test_findings_become_quoted_reasons_and_lower_rigor() -> None:
    from tests.test_analysis_guard import criteria

    report = ConsistencyReport(status="checked", findings=[finding_on_paper(), finding_on_paper("percent_claim")])
    base = run(payload(criteria=criteria(80)), make_ctx(article_type=pp.RESEARCH_ARTICLE))
    out = run(payload(criteria=criteria(80)), make_ctx(article_type=pp.RESEARCH_ARTICLE, consistency=report))
    rigor = out["criteria"]["rigor"]
    assert rigor["score"] == 60 and rigor["raw_score"] == 80
    assert rigor["adjustments"] == ["Capped at 60: 2 major inconsistencies in the paper's own values."]
    assert out["mood_score"] < base["mood_score"]
    first = out["rejection_reasons"][0]
    assert first["severity"] == "major" and first["evidence"] and first["solution"]


def test_the_model_is_told_the_findings_and_the_disclosed_interests() -> None:
    from models import Disclosures
    from services.llm import base

    ctx = make_ctx(consistency=ConsistencyReport(status="checked", findings=[finding_on_paper()]),
                   disclosures=Disclosures(interests=["patent or inventorship"]))
    block = base._profile_block(ctx)
    assert "48 h in the Methods but 24 h" in block and "Do NOT list them in rejection_reasons" in block
    assert "patent or inventorship" in block and "not misconduct" in block


# ------------------------------------------------------------------ disclosures

def parsed_text(text: str) -> ParsedDocument:
    return ParsedDocument(full_text=text, abstract="", sections={}, references=[], word_count=len(text.split()))


def test_an_inline_plos_style_competing_interest_statement_names_its_interests() -> None:
    text = (
        "Funding: This work was supported by the Terry Fox Foundation and by Coastside Bio Resources Inc.\n"
        "Competing Interests: Peter Collin is a director, employee and stockholder of Coastside Bio Resources. "
        "PC and TA are co-inventors of a US patent on frondoside A. This does not alter the authors' adherence to "
        "all the PLOS ONE policies.\n"
        "Introduction\nLung cancer is the leading cause of cancer death."
    )
    found = read_disclosures(parsed_text(text))
    assert found.competing_interests and found.competing_interests.startswith("Peter Collin is a director")
    assert "Lung cancer" not in found.competing_interests
    assert {"patent or inventorship", "stock or equity", "employment by a company",
            "company director, founder or officer", "company funding"} <= set(found.interests)
    assert found.declares_none is False and found.funding.startswith("This work was supported")


def test_a_one_sentence_declaration_of_no_interest_is_read() -> None:
    text = "Author Contributions\nY.S. wrote the article.\nNotes\nThe authors declare no competing financial interest.\n"
    found = read_disclosures(parsed_text(text))
    assert found.declares_none is True and found.interests == []
    assert "no competing financial interest" in (found.competing_interests or "")


def test_a_headed_conflict_statement_ends_at_the_next_heading() -> None:
    text = "Conflicts of interest\nThere are no conflicts to declare.\nAcknowledgements\nWe thank the facility staff."
    found = read_disclosures(parsed_text(text))
    assert found.competing_interests == "There are no conflicts to declare." and found.declares_none
