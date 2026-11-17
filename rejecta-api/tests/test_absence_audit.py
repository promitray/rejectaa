"""Statements that the paper lacks something, checked against the whole paper (services.absence_audit)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from models import AnalysisResult
from services import absence_audit as aa
from services import paper_profile as pp
from services.analysis_guard import AnalysisContext

ABSTRACT = "We benchmark 12 models on 14 datasets. Deep models are generally unable to outperform non-deep ones."
RESULTS = (
    "Deep models underperform non-deep counterparts on 10 of 14 datasets in Table 1. "
    "The failure cannot be solely attributed to the small size of the datasets."
)
WHY = (
    "We design two experiments to verify the explanation, increasing or decreasing the level of smoothing. "
    "With 10-smooth and 20-smooth labels the deep models improve steadily, as shown in Figure 2.\n\n"
    "Secondly, we decrease smoothness using activity cliffs. On cliff molecules the deep models degrade more than "
    "tree models, as Table 2 shows."
)
SETUP = "Results are averaged over 50 independent runs with different seeds. The standard deviations are in the appendix."
SECTIONS = {"results": RESULTS, "Why above phenomena would occur?": WHY, "methods": SETUP}
ROLES = {"results": "results", "Why above phenomena would occur?": "thematic", "methods": "methods"}


def ctx() -> AnalysisContext:
    return AnalysisContext(
        source_text="\n\n".join([ABSTRACT, *SECTIONS.values()]), body="\n\n".join(SECTIONS.values()),
        structure_mode="detected", section_texts=SECTIONS, section_roles=ROLES, section_titles={"results": "Benchmarking Results"},
        article_type=pp.RESEARCH_ARTICLE, abstract=ABSTRACT, numbers_text="\n".join(SECTIONS.values()),
    )


CAUSAL = ("The paper lacks a controlled experiment or mechanistic measurement to isolate why deep models fail on "
          "the 14 datasets.")
STATS = "The section does not report n, statistical tests or confidence intervals for the comparison in Table 1."
FIX = "Add an experiment that varies one factor."


def finding(problem: str, check: str = "universal.causal_evidence") -> dict[str, Any]:
    return {"severity": "major", "problem": problem, "quote": "Deep models underperform non-deep counterparts",
            "fix": FIX, "check": check, "origin": "rubric", "status": "problem", "evidence_ids": ["P2.1"],
            "claim_id": "C1", "claim": "Deep models are generally unable to outperform non-deep ones"}


def report() -> AnalysisResult:
    return AnalysisResult.model_validate({
        "mood": "neutral", "mood_score": 70, "verdict": "I would return this for major revision.",
        "oracle_letter": "The benchmark is broad. However, the paper lacks a controlled experiment to isolate the cause.",
        "sections": {
            "results": {
                "score": 60, "role": "results", "title": "Benchmarking Results",
                "summary": "The section compares models on 14 datasets. It lacks controlled experiments to isolate causes.",
                "findings": [finding(CAUSAL), finding(STATS, "nature.statistics_reporting")],
                "score_basis": ["Start at 85"], "strengths": [], "checks": [],
            },
        },
        "rejection_reasons": [{"severity": "major", "problem": "No ablation is provided for the tree models.", "reason": "x",
                               "evidence": "Deep models underperform non-deep counterparts", "origin": "model"}],
        "criteria": {"rigor": {"score": 75, "weight": 25.0, "reason": "Methods are clear. There is no significance test."}},
        "novelty_assessment": "n", "journal_fit": "j", "provider_used": "t",
        "evidence_ledger": {"claims": [{"id": "C1", "statement": "s", "type": "comparative", "main": True}]},
    })


class Checker:
    """Answers the audit call from `answers`, and the confirmation call with `confirm` (default: every quotation
    states its thing)."""

    def __init__(self, answers: dict[str, dict[str, Any]], confirm: str = "states") -> None:
        self.answers = answers
        self.confirm = confirm
        self.prompts: list[str] = []

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(prompt)
        if "states a specific thing" in system:
            return json.dumps({"items": [{"id": f"E{n}", "answer": self.confirm} for n in range(1, 40)]})
        return json.dumps({"items": [{"id": k, **v} for k, v in self.answers.items()]})

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def test_every_statement_of_absence_in_the_report_is_collected() -> None:
    items = aa.collect(report().model_dump(mode="json"))
    where = [(i.where, i.kind.id) for i in items]
    assert where == [
        ("finding in Benchmarking Results", "controlled_experiment"),
        ("finding in Benchmarking Results", "statistics"),
        ("concern", "controlled_experiment"),
        ("summary of Benchmarking Results", "controlled_experiment"),
        ("reason for the rigor score", "statistics"),
        ("oracle letter", "controlled_experiment"),
    ]


def test_the_checker_reads_the_whole_paper_with_guidance_for_each_kind() -> None:
    checker = Checker({})
    asyncio.run(aa.audit(report(), ctx(), checker))
    prompt = checker.prompts[0]
    assert "[P3.1] We design two experiments to verify the explanation" in prompt  # Section 3, numbered
    assert "even when the paper calls it an \"explanation\"" in prompt


def test_present_removes_partly_rewrites_absent_marks_searched_and_scores_are_recomputed() -> None:
    corrected = ("Results are averaged over 50 independent runs with standard deviations in the appendix, but no "
                 "significance test is reported for the comparison in Table 1.")
    checker = Checker({
        "A1": {"verdict": "present", "evidence": ["P3.1", "P3.2", "P4.1"], "quote": "We design two experiments to verify the explanation, increasing or decreasing the level of smoothing"},  # the smoothing and cliff experiments
        "A2": {"verdict": "partly", "evidence": ["P5.1", "P5.2"], "corrected": corrected, "quote": "averaged over 50 independent runs"},
        "A3": {"verdict": "absent", "evidence": []},
        "A4": {"verdict": "present", "evidence": ["P3.1"], "quote": "We design two experiments to verify the explanation, increasing or decreasing the level of smoothing"},
        "A5": {"verdict": "partly", "evidence": ["P5.1"], "corrected": "Spread over 50 runs is given (P5.1), but no significance test is."},
        "A6": {"verdict": "present", "evidence": ["P3.1"], "quote": "We design two experiments to verify the explanation, increasing or decreasing the level of smoothing"},
    })
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    results = out.sections["results"]
    assert [f.problem for f in results.findings] == [corrected]
    assert results.findings[0].addressed.startswith("Results are averaged over 50 independent runs")
    # the summary's "lacks controlled experiments" is about THIS section: Section 3's experiment does not remove it
    assert results.summary == "The section compares models on 14 datasets. It lacks controlled experiments to isolate causes."
    assert out.criteria["rigor"].reason == "Methods are clear. Spread over 50 runs is given, but no significance test is."
    assert "lacks a controlled experiment" not in out.oracle_letter
    assert out.rejection_reasons[0].problem == "No ablation is provided for the tree models."  # absent: stays
    assert results.score != 60 and results.score_basis[0].startswith("Start at")
    assert out.evidence_ledger.removed["found_in_paper"] == 1
    assert out.absence_audit["present"] == 2 and out.absence_audit["partly"] == 2 and out.absence_audit["mode"] == "full paper"


def test_a_concern_the_paper_answers_is_set_aside_as_unverified() -> None:
    checker = Checker({"A3": {"verdict": "present", "evidence": ["P4.1"], "quote": "Secondly, we decrease smoothness using activity cliffs"}})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert out.rejection_reasons == [] or all(r.origin != "model" for r in out.rejection_reasons)
    assert out.unverified_concerns[0].unverified_reason.startswith("The paper does contain what this concern says is missing")


def test_citations_that_do_not_exist_keep_the_statement() -> None:
    checker = Checker({"A1": {"verdict": "present", "evidence": ["P99.1"]}})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert out.sections["results"].findings[0].problem == CAUSAL and out.sections["results"].findings[0].searched


def test_a_failed_check_leaves_the_report_and_says_so() -> None:
    class Broken(Checker):
        async def generate(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("down")

    out = asyncio.run(aa.audit(report(), ctx(), Broken({})))
    assert len(out.sections["results"].findings) == 2
    assert any("could not be checked against the whole paper" in n for n in out.grounding_notes)


def test_a_long_paper_is_searched_per_statement_in_every_section(monkeypatch: Any) -> None:
    monkeypatch.setattr(aa, "FULL_PAPER_CHARS", 100)
    checker = Checker({})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert out.absence_audit["mode"] == "retrieval"
    assert "PASSAGES (from every section of the paper)" in checker.prompts[0]
    assert "(Why above phenomena would occur?)" in checker.prompts[0]


def test_kinds_are_recognised_by_what_the_statement_is_about() -> None:
    assert aa.kind_of("lacks a controlled experiment to isolate the cause").id == "controlled_experiment"
    assert aa.kind_of("does not report the statistical test").id == "statistics"
    assert aa.kind_of("does not state the hyperparameter search").id == "methods_detail"
    assert aa.kind_of("no comparison with the strongest baseline").id == "baseline"
    assert aa.kind_of("something else entirely").id == "other"



def test_present_without_the_words_that_show_it_keeps_the_statement() -> None:
    no_quote = Checker({"A1": {"verdict": "present", "evidence": ["P5.1"]}})
    out = asyncio.run(aa.audit(report(), ctx(), no_quote))
    assert out.sections["results"].findings[0].problem == CAUSAL
    wrong_quote = Checker({"A1": {"verdict": "present", "evidence": ["P5.1"], "quote": "an ablation that removes each component in turn"}})  # not in the paper
    out = asyncio.run(aa.audit(report(), ctx(), wrong_quote))
    assert out.sections["results"].findings[0].problem == CAUSAL


def test_claim_numbers_never_reach_the_reader() -> None:
    from services.evidence_ledger import without_claim_ids

    assert without_claim_ids("It lacks explicit statements of the main claims C1 and C2, which matter.") == (
        "It lacks explicit statements of the main claims, which matter."
    )
    assert without_claim_ids("The comparison (C1) is unfair.") == "The comparison is unfair."


def test_a_quote_found_elsewhere_in_the_paper_counts_and_its_sentence_is_cited() -> None:
    # the model points at the wrong sentence (P5.1) but quotes words that are in P3.2
    checker = Checker({"A1": {"verdict": "present", "evidence": ["P5.1"], "quote": "the deep models improve steadily, as shown in Figure 2"}})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert all(f.problem != CAUSAL for f in out.sections["results"].findings)
    assert out.absence_audit["items"][0]["evidence_ids"] == ["P3.2", "P5.1"]


# --------------------------------------------------------------------------- parts: absent vs present but incomplete


def test_parts_decide_partly_even_when_the_checker_says_absent() -> None:
    checker = Checker({"A2": {
        "verdict": "absent", "evidence": [], "corrected": "",
        "parts": [
            {"part": "n", "found": True, "where": "Experimental Setups", "quote": "averaged over 50 independent runs"},
            {"part": "spread", "found": True, "where": "appendix only", "quote": "The standard deviations are in the appendix"},
            {"part": "significance test", "found": False, "where": "", "quote": ""},
        ],
    }})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    stats = next(f for f in out.sections["results"].findings if f.check == "nature.statistics_reporting")
    # each element with its own location; "are in the appendix" only points to the spread, it is not the spread
    assert stats.problem == (
        "This section does not give n, spread, significance test. In the paper: n is given in methods; spread is only "
        "referred to (“The standard deviations are in the appendix.”), but the appendices in the file do not show it; "
        "significance test is not given anywhere in the paper."
    )
    assert stats.addressed_ids == ["P5.1"]
    item = out.absence_audit["items"][1]
    assert item["verdict"] == "partly" and [p["found"] for p in item["parts"]] == [True, False, False]
    assert item["parts"][1]["pointer"].startswith("The standard deviations are in the appendix")


def test_a_part_found_only_by_a_quote_not_in_the_paper_does_not_count() -> None:
    checker = Checker({"A1": {
        "verdict": "present", "evidence": ["P3.1"],
        "parts": [{"part": "controlled experiment", "found": True, "quote": "an ablation of every component in turn"}],
    }})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert out.absence_audit["items"][0]["verdict"] == "absent"


def test_all_parts_found_is_present_and_the_checkers_restatement_is_used_when_sound() -> None:
    corrected = "Standard deviations over 50 independent runs are given in the appendix, but no significance test is reported."
    checker = Checker({
        "A1": {"parts": [{"part": "controlled experiment", "found": True,
                          "quote": "increasing or decreasing the level of smoothing"}]},
        "A2": {"corrected": corrected, "parts": [
            {"part": "spread", "found": True, "quote": "The standard deviations are in the appendix"},
            {"part": "test", "found": False}]},
    })
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert [i["verdict"] for i in out.absence_audit["items"][:2]] == ["present", "partly"]
    # the spread is only pointed to: said so, element by element
    assert [f.problem for f in out.sections["results"].findings] == [
        "This section: spread is only referred to (“The standard deviations are in the appendix.”), but the appendices "
        "in the file do not show it; test is not given anywhere in the paper."
    ]



# --------------------------------------------------------------------------- scope: this section, or the whole paper


def test_scope_follows_the_wording() -> None:
    assert aa.scope_of("The section does not discuss smoothing.", "discussion") == "discussion"
    assert aa.scope_of("It does not address the dataset-size claim.", "discussion") == "discussion"
    assert aa.scope_of("The paper never reports a significance test.", "results") is None
    assert aa.scope_of("This section of the paper lacks a test.", "results") == "results"
    assert aa.scope_of("The paper lacks a test.", None) is None


SECTION_GAP = "The section does not discuss why deep models improve on smoothed datasets."


def report_with(problem: str, section: str = "Why above phenomena would occur?") -> AnalysisResult:
    data = report().model_dump(mode="json")
    data["sections"] = {
        "results": {**data["sections"]["results"], "findings": [{**finding(problem), "check": None}], "summary": None},
        section: {"score": 80, "role": "thematic", "findings": [], "score_basis": ["Start at 85"], "strengths": [], "checks": []},
    }
    data["rejection_reasons"], data["oracle_letter"] = [], "x"
    data["criteria"]["rigor"]["reason"] = "Clear."
    return AnalysisResult.model_validate(data)


SMOOTH_PART = {"part": "why deep models improve on smoothed data", "found": True,
               "quote": "With 10-smooth and 20-smooth labels the deep models improve steadily"}


def test_a_section_gap_covered_only_elsewhere_is_kept_as_a_minor_cross_reference() -> None:
    out = asyncio.run(aa.audit(report_with(SECTION_GAP), ctx(), Checker({"A1": {"verdict": "present", "parts": [SMOOTH_PART]}})))
    f = out.sections["results"].findings[0]
    assert f.problem == (
        "This section does not give why deep models improve on smoothed data; it is given in Why above phenomena would occur?."
    )
    assert f.severity == "minor" and f.cross_reference
    # the fix and the consequence follow the correction: point to where the paper has it
    assert f.fix.startswith("Point to Why above phenomena would occur? from this section") and f.kind == "add"
    assert "it is in Why above phenomena would occur?" in (f.impact or "")
    item = out.absence_audit["items"][0]
    assert item["verdict"] == "partly" and item["scope"] == "Benchmarking Results" and item["parts"][0]["in_scope"] is False


def test_a_section_gap_answered_in_that_section_is_removed() -> None:
    in_results = {"part": "the comparison", "found": True, "quote": "Deep models underperform non-deep counterparts on 10 of 14 datasets"}
    out = asyncio.run(aa.audit(report_with("The section does not compare deep and non-deep models."), ctx(),
                               Checker({"A1": {"verdict": "present", "parts": [in_results]}})))
    assert out.sections["results"].findings == []


def test_a_statement_about_the_whole_paper_is_removed_by_evidence_anywhere() -> None:
    out = asyncio.run(aa.audit(report_with("The paper does not explain why deep models improve on smoothed datasets."), ctx(),
                               Checker({"A1": {"verdict": "present", "parts": [SMOOTH_PART]}})))
    assert out.sections["results"].findings == []



def test_a_quotation_that_is_only_on_the_topic_does_not_remove_the_statement() -> None:
    # Appendix D: "These phenomena can be attributed to ..." is about causes, not about dataset size
    checker = Checker({"A1": {"verdict": "present", "parts": [SMOOTH_PART]}}, confirm="related")
    out = asyncio.run(aa.audit(report_with("The paper does not explain why deep models improve on smoothed datasets."), ctx(), checker))
    assert len(out.sections["results"].findings) == 1
    assert out.absence_audit["not_specific"] == 1 and out.absence_audit["items"][0]["verdict"] == "absent"



def test_a_part_whose_sentences_only_relate_does_not_narrow_the_statement() -> None:
    # "partly" claims the paper has part of it: that part is confirmed too, and a merely related one does not count
    checker = Checker({"A2": {"parts": [
        {"part": "spread", "found": True, "quotes": ["Results are averaged over 50 independent runs"]},
        {"part": "significance test", "found": False}]}}, confirm="related")
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    stats = next(f for f in out.sections["results"].findings if f.check == "nature.statistics_reporting")
    assert stats.problem == STATS and stats.searched


def test_any_of_up_to_three_quotes_can_show_a_part() -> None:
    checker = Checker({"A1": {"parts": [{"part": "controlled experiment", "found": True, "quotes": [
        "words that are not in the paper at all", "increasing or decreasing the level of smoothing"]}]}})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    assert all(f.problem != CAUSAL for f in out.sections["results"].findings)


def test_two_statements_about_the_same_thing_in_one_section_do_not_get_opposite_outcomes() -> None:
    data = report_with(SECTION_GAP).model_dump(mode="json")
    data["sections"]["results"]["summary"] = "It does not discuss why deep models improve on smoothed datasets."
    report2 = AnalysisResult.model_validate(data)
    in_section = {"part": "why", "found": True, "quotes": ["Deep models underperform non-deep counterparts on 10 of 14 datasets"]}
    checker = Checker({
        "A1": {"parts": [SMOOTH_PART]},       # the finding: covered only in Section 3 -> cross-reference, kept
        "A2": {"parts": [in_section]},        # the summary sentence: "found in this section" -> would be removed
    })
    out = asyncio.run(aa.audit(report2, ctx(), checker))
    assert out.sections["results"].summary == "It does not discuss why deep models improve on smoothed datasets."
    assert out.absence_audit["kept_for_consistency"] == 1


def test_the_paper_named_in_passing_does_not_make_a_statement_paper_wide() -> None:
    assert aa.scope_of("It lacks discussion of dataset size, which matters for the paper's claims.", "discussion") == "discussion"
    assert aa.scope_of("The authors never report the significance test.", "results") is None



def test_one_elements_location_is_never_given_to_another() -> None:
    # n is in the setup; the spread has only a pointer; neither borrows the other's place
    checker = Checker({"A2": {"parts": [
        {"part": "n", "found": True, "quotes": ["averaged over 50 independent runs"]},
        {"part": "a measure of spread", "found": True, "quotes": ["The standard deviations are in the appendix"]},
    ]}})
    out = asyncio.run(aa.audit(report(), ctx(), checker))
    stats = next(f for f in out.sections["results"].findings if f.check == "nature.statistics_reporting")
    assert "n is given in methods" in stats.problem
    assert "a measure of spread is only referred to" in stats.problem and "spread is given" not in stats.problem


def test_pointers_are_recognised() -> None:
    assert aa._POINTER.search("The standard deviations can be seen in the appendix for the limited space.")
    assert aa._POINTER.search("Full spectra are provided in the Supporting Information.")
    assert not aa._POINTER.search("Table 3 lists the standard deviations of all models.")
