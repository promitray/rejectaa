"""Stages 3-5: concerns name their criterion, unsupported concerns are set apart, and code computes the score."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from models import AnalysisResult, ConsistencyFinding, ConsistencyQuote, ConsistencyReport, PublicationStatus
from services import analysis_guard as guard
from services import paper_profile as pp
from services.evidence_check import check_support
from services.llm import base
from tests.test_analysis_guard import PAPER, criteria, make_ctx, payload, run


def concern(severity: str = "major", criterion: str = "rigor", evidence: str | None = None) -> dict[str, Any]:
    words = " ".join(PAPER.split()[:12]) if evidence is None else evidence
    return {
        "problem": "The claimed mechanism is not supported by the data shown in this passage.",
        "evidence": words, "impact": "The central conclusion may not hold for these cells.",
        "solution": "Add the mechanism experiment for the claimed data and conclusion.",
        "severity": severity, "criterion": criterion,
    }


def research(**extra: Any):
    return make_ctx(article_type=pp.RESEARCH_ARTICLE, **extra)


# ------------------------------------------------------------------ Stage 3: concerns per criterion

def test_the_model_is_asked_which_criterion_each_concern_bears_on() -> None:
    assert '"criterion": "<the key of the criterion this concern lowers' in base.AbstractAnalysisProvider.SYSTEM_PROMPT_A


def test_a_concern_keeps_a_valid_criterion_and_drops_an_invented_one() -> None:
    out = run(payload(criteria=criteria(90), rejection_reasons=[concern(criterion="Rigor"), concern(criterion="vibes")]),
              research())
    assert [r["criterion"] for r in out["rejection_reasons"]] == ["rigor", ""]
    assert all(r["origin"] == "model" for r in out["rejection_reasons"])


# ------------------------------------------------------------------ Stage 5: limits the scores must respect

def test_a_verified_major_concern_caps_its_criterion() -> None:
    out = run(payload(criteria=criteria(90), rejection_reasons=[concern("major", "rigor")]), research())
    rigor = out["criteria"]["rigor"]
    assert rigor["score"] == guard.CRITERION_CAP_MAJOR and rigor["raw_score"] == 90
    assert rigor["adjustments"] == [f"Capped at {guard.CRITERION_CAP_MAJOR}: a major concern bears on it."]
    assert out["criteria"]["novelty"]["score"] == 90  # only the criterion the concern names


def test_a_desk_reject_concern_caps_its_criterion_lower_and_a_minor_one_does_not_cap() -> None:
    out = run(payload(criteria=criteria(90), rejection_reasons=[concern("desk_reject", "journal_fit"), concern("minor", "clarity")]),
              research())
    assert out["criteria"]["journal_fit"]["score"] == guard.CRITERION_CAP_DESK_REJECT
    assert out["criteria"]["clarity"]["score"] == 90


def test_an_unverified_concern_never_caps_a_score() -> None:
    lost = concern("desk_reject", "rigor", evidence="words that are nowhere in this manuscript at all")
    out = run(payload(criteria=criteria(90), rejection_reasons=[lost]), research())
    assert out["rejection_reasons"] == [] and out["criteria"]["rigor"]["score"] == 90
    assert out["unverified_concerns"][0]["unverified_reason"].startswith("The quoted evidence is not in the manuscript")


def test_three_major_inconsistencies_cap_rigor_lower_than_one() -> None:
    finding = ConsistencyFinding(kind="value_mismatch", severity="major", message="m",
                                 quotes=[ConsistencyQuote(quote=" ".join(PAPER.split()[:10]), section="methods")])
    one = run(payload(criteria=criteria(90)), research(consistency=ConsistencyReport(status="checked", findings=[finding])))
    three = run(payload(criteria=criteria(90)),
                research(consistency=ConsistencyReport(status="checked", findings=[finding.model_copy(update={"message": f"m{i}"}) for i in range(3)])))
    assert one["criteria"]["rigor"]["score"] == guard.RIGOR_CAP_ONE_CONTRADICTION
    assert three["criteria"]["rigor"]["score"] == guard.RIGOR_CAP_SEVERAL_CONTRADICTIONS


def test_the_score_is_the_same_when_it_is_computed_again() -> None:
    ctx = research(publication=PublicationStatus(status="expression_of_concern"))
    out = run(payload(criteria=criteria(90), rejection_reasons=[concern("major", "rigor")]), ctx)
    again = json.loads(json.dumps(out))
    guard.score_report(again, ctx)
    assert again["mood_score"] == out["mood_score"] <= guard.CONCERN_SCORE_CAP
    assert again["criteria"] == out["criteria"]


# ------------------------------------------------------------------ Stage 4: does the quotation show the problem?

class Judge:
    model = "judge"

    def __init__(self, answer: Any = None, fail: bool = False) -> None:
        self.answer, self.fail, self.prompts = answer, fail, []

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(user_prompt)
        if self.fail:
            raise RuntimeError("down")
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def analysed(ctx, reasons: list[dict[str, Any]]) -> AnalysisResult:
    raw = payload(criteria=criteria(90), rejection_reasons=reasons)
    raw["provider_used"] = "test"
    return AnalysisResult.model_validate(run(raw, ctx))


def test_a_concern_whose_quotation_does_not_show_the_problem_is_set_apart_and_the_score_recomputed() -> None:
    ctx = research()
    report = analysed(ctx, [concern("major", "rigor"), concern("minor", "clarity")])
    assert report.criteria["rigor"].score == guard.CRITERION_CAP_MAJOR
    judge = Judge({"verdicts": [{"id": 1, "supports": "no", "why": "The passage is about the microscope, not the synthesis."},
                                {"id": 2, "supports": "yes", "why": ""}]})
    checked = asyncio.run(check_support(report, judge, ctx))
    assert [r.severity.value for r in checked.rejection_reasons] == ["minor"]
    moved = checked.unverified_concerns[0]
    assert moved.unverified_reason == "The quoted passage does not show this problem: The passage is about the microscope, not the synthesis."
    assert checked.criteria["rigor"].score == 90  # the cap went with the unsupported concern
    assert checked.mood_score > report.mood_score


def test_concerns_written_by_code_are_never_sent_to_the_judge() -> None:
    finding = ConsistencyFinding(kind="value_mismatch", severity="major", message="The time differs.",
                                 quotes=[ConsistencyQuote(quote=" ".join(PAPER.split()[:10]), section="methods")])
    ctx = research(consistency=ConsistencyReport(status="checked", findings=[finding]))
    report = analysed(ctx, [])
    judge = Judge({"verdicts": [{"id": 1, "supports": "no", "why": "x"}]})
    checked = asyncio.run(check_support(report, judge, ctx))
    assert judge.prompts == [] and checked.rejection_reasons[0].origin == "consistency"


def test_a_failing_judge_leaves_the_report_as_it_is_and_says_so() -> None:
    ctx = research()
    report = analysed(ctx, [concern("major", "rigor")])
    checked = asyncio.run(check_support(report, Judge(fail=True), ctx))
    assert len(checked.rejection_reasons) == 1 and checked.mood_score == report.mood_score
    assert any("could not be checked" in note for note in checked.grounding_notes)
