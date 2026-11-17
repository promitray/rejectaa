"""The section review: batching, and the checks every point of the model's answer must pass."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from models import AnalysisResult
from services import paper_profile as pp
from services import section_review as sr
from services.analysis_guard import AnalysisContext

INTRO = (
    "Variational autoencoders are widely used for molecular generation [1-3]. Their latent spaces are poorly "
    "conditioned on molecular properties. In this paper, we condition the latent space on a target property."
)
METHODS = (
    "All networks were implemented using Keras [41] and TensorFlow [42]. We empirically deduce that a target "
    "mutual information of 4.85 leads to optimal and stable training results. We generate 5000 new samples by "
    "randomly appending substructures to existing molecules at random locations."
)
RESULTS = "The constrained optimisation improved the score to 5.67 with a success rate of 98.25 over 800 molecules."


def ctx(**overrides: Any) -> AnalysisContext:
    texts = {"introduction": INTRO, "methods": METHODS, "results": RESULTS}
    values: dict[str, Any] = dict(
        source_text="\n\n".join(texts.values()), body="\n\n".join(texts.values()), structure_mode="detected",
        section_texts=texts, section_roles={k: k for k in texts}, section_titles={"methods": "Methodology"},
        article_type=pp.RESEARCH_ARTICLE, abstract="We condition a VAE.",
    )
    values.update(overrides)
    return AnalysisContext(**values)


def analysis(checks: dict[str, list[dict[str, Any]]] | None = None) -> AnalysisResult:
    sections = {
        name: {"score": 70, "issue": "i", "fix": "f", "role": name, "checks": (checks or {}).get(name, [])}
        for name in ("introduction", "methods", "results")
    }
    return AnalysisResult.model_validate({
        "mood": "neutral", "mood_score": 70, "verdict": "v", "oracle_letter": "l", "criteria": {},
        "sections": sections, "rejection_reasons": [], "novelty_assessment": "n", "journal_fit": "j",
        "provider_used": "test", "processing_time_seconds": 1.0, "word_count": 100, "reference_count": 3,
    })


class Provider:
    def __init__(self, answer: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.answer = answer or {"sections": []}
        self.fail = fail
        self.prompts: list[str] = []

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("down")
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def finding(quote: str, problem: str, fix: str, severity: str = "major") -> dict[str, str]:
    return {"severity": severity, "problem": problem, "quote": quote, "fix": fix}


GOOD = finding(
    "We empirically deduce that a target mutual information of 4.85 leads to optimal",
    "The target mutual information of 4.85 is said to be found empirically, but the search is not described.",
    "Describe the search over target mutual information values: the range tried, runs per value and the criterion.",
)


def review(answer: dict[str, Any], c: AnalysisContext | None = None, checks: dict | None = None) -> AnalysisResult:
    c = c or ctx()
    provider = Provider(answer)
    result = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J Chem Inf Model", provider, c))
    return sr.apply_review(analysis(checks), result, c)


def test_a_finding_whose_quote_is_in_the_section_is_kept() -> None:
    out = review({"sections": [{"name": "methods", "summary": "Describes training.", "findings": [GOOD],
                                "strengths": [{"point": "Names the frameworks used", "quote": "All networks were implemented using Keras [41] and TensorFlow [42]"}]}]})
    methods = out.sections["methods"]
    assert [f.problem for f in methods.findings] == [GOOD["problem"]]
    assert methods.summary == "Describes training."
    assert methods.strengths[0].point == "Names the frameworks used"


def test_a_finding_with_an_invented_quote_is_left_out_and_counted() -> None:
    made_up = finding("The authors trained on 1.2 million molecules from ChEMBL using a GPU cluster",
                      "The training set size of the ChEMBL molecules is not justified anywhere.",
                      "Justify the training set size of the ChEMBL molecules with a learning curve.")
    out = review({"sections": [{"name": "methods", "findings": [made_up, GOOD]}]})
    assert [f.quote for f in out.sections["methods"].findings] == [GOOD["quote"]]
    assert any("left out" in note for note in out.grounding_notes)


def test_a_fix_that_would_fit_any_paper_is_left_out() -> None:
    generic = finding(GOOD["quote"], GOOD["problem"], "Improve the clarity and provide additional details overall.")
    out = review({"sections": [{"name": "methods", "findings": [generic]}]})
    assert out.sections["methods"].findings == []


def test_a_finding_on_a_passage_the_checklist_already_reports_is_left_out() -> None:
    check = {"key": "software_version", "label": "Software versions", "status": "issue", "severity": "minor",
             "detail": "", "quote": "All networks were implemented using Keras [41] and TensorFlow [42].", "fix": ""}
    repeat = finding("All networks were implemented using Keras [41] and TensorFlow [42]",
                     "Keras and TensorFlow are named without the versions that were used.",
                     "Give the Keras and TensorFlow versions used for the networks.", "minor")
    out = review({"sections": [{"name": "methods", "findings": [repeat, GOOD]}]}, checks={"methods": [check]})
    assert [f.quote for f in out.sections["methods"].findings] == [GOOD["quote"]]


def test_findings_that_say_again_what_the_checklist_shows_are_left_out() -> None:
    checks = [
        {"key": "figure_unreferenced", "label": "Fig. 1 is never referred to", "status": "issue", "severity": "minor",
         "detail": "", "quote": "", "fix": ""},
        {"key": "overclaim", "label": "Strong claim: “dramatically improve”", "status": "issue", "severity": "minor",
         "detail": "", "quote": "Though introducing the conditional sampling mechanism helps us dramatically improve on the …", "fix": ""},
    ]
    figure = finding("The constrained optimisation improved the score to 5.67 with a success rate of 98.25",
                     "Figure 1 is never referred to in the text, so the 5.67 improvement has no figure to support it.",
                     "Refer to Figure 1 where the 5.67 improvement over 800 molecules is discussed.")
    claim = finding("The constrained optimisation improved the score to 5.67 with a success rate of 98.25",
                    "The claim to dramatically improve the score of 5.67 is stronger than the evidence shown.",
                    "Rephrase the dramatically improve claim about the 5.67 score as a measured statement.", "minor")
    out = review({"sections": [{"name": "results", "findings": [figure, claim]}]}, checks={"results": checks})
    assert out.sections["results"].findings == []


def test_a_strength_that_quotes_a_figure_caption_is_left_out() -> None:
    out = review({"sections": [{"name": "results", "strengths": [
        {"point": "Gives detailed results for the benchmark", "quote": "Table 1: Generation performance on ZINC250K dataset sampling from a normal distribution"}]}]},
        ctx(section_texts={"introduction": INTRO, "methods": METHODS,
                           "results": RESULTS + " Table 1: Generation performance on ZINC250K dataset sampling from a normal distribution (N(0, I))."}))
    assert out.sections["results"].strengths == []


def test_the_section_score_is_computed_from_findings_and_checklist_and_keeps_the_models_number() -> None:
    check = {"key": "seed", "label": "Random seed", "status": "missing", "severity": "minor", "detail": "", "quote": "", "fix": ""}
    out = review({"sections": [{"name": "methods", "findings": [GOOD],
                                "strengths": [{"point": "Names the frameworks used", "quote": "All networks were implemented using Keras [41] and TensorFlow [42]"}]}]},
                 checks={"methods": [check]})
    methods = out.sections["methods"]
    assert methods.llm_score == 70 and methods.score == 85 - sr.MAJOR_FINDING_COST - sr.MINOR_CHECK_COST + sr.STRENGTH_BONUS
    assert methods.score_basis[0].startswith("Start at 85") and any(f"−{sr.MAJOR_FINDING_COST} major" in s for s in methods.score_basis)


def test_a_section_with_nothing_to_fault_scores_the_start_value_and_the_same_input_the_same_score() -> None:
    answer = {"sections": [{"name": "results", "findings": [], "strengths": []}]}
    assert review(answer).sections["results"].score == sr.START_SCORE
    assert review(answer).sections["results"].score == review(answer).sections["results"].score


def test_the_score_never_leaves_its_bounds() -> None:
    many = [{"severity": "major", "problem": "x" * 30, "quote": "q", "fix": "f"}] * 10
    assert sr.section_score(many, [], [])[0] == sr.START_SCORE - sr.MAX_FINDING_COST
    heavy = [{"status": "issue", "severity": "major", "label": "l"}] * 10
    assert sr.section_score(many, [], heavy)[0] == max(sr.SCORE_FLOOR, sr.START_SCORE - sr.MAX_FINDING_COST - sr.MAX_CHECK_COST)


def test_a_section_the_review_did_not_reach_keeps_the_models_score() -> None:
    out = review({"sections": [{"name": "methods", "findings": []}]})
    assert out.sections["results"].score == 70 and out.sections["results"].llm_score is None


def test_major_findings_come_first_and_unknown_severity_is_minor() -> None:
    minor = dict(GOOD, severity="cosmetic", quote="We generate 5000 new samples by randomly appending substructures",
                 problem="The 5000 new samples are generated randomly but no random seed for the substructures is given.",
                 fix="Give the random seed used to append the substructures, so the 5000 samples can be regenerated.")
    out = review({"sections": [{"name": "methods", "findings": [minor, GOOD]}]})
    assert [f.severity for f in out.sections["methods"].findings] == ["major", "minor"]


def test_a_section_the_model_names_wrongly_is_ignored() -> None:
    out = review({"sections": [{"name": "Methods and materials", "findings": [GOOD]}]})
    assert out.sections["methods"].findings == []


def test_sections_are_sent_in_small_parallel_batches_with_their_checklist_and_heading() -> None:
    texts = {f"part {i}": f"Text of part {i}. " * 20 for i in range(6)}
    c = ctx(section_texts=texts, section_roles={k: "thematic" for k in texts}, section_titles={},
            source_text=" ".join(texts.values()), body=" ".join(texts.values()))
    provider = Provider()
    asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", provider, c))
    first = provider.prompts[:2]  # the batches: 4 sections + 2 (an empty answer is then retried section by section)
    assert [p.count("### name:") for p in first] == [4, 2]
    provider = Provider()
    asyncio.run(sr.review_sections(sr.inputs_from_parsed(ctx()), "J", provider, ctx()))
    assert 'heading in the paper: "Methodology"' in provider.prompts[0]
    assert "Already found by code" in provider.prompts[0] and "Software versions" in provider.prompts[0]


class LazyProvider(Provider):
    """Answers a batch for its first section only, as the model sometimes does; a single section gets its answer."""

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(prompt)
        wanted = prompt.split("REVIEW THESE SECTIONS:", 1)[1]
        names = [n for n in ("introduction", "methods", "results") if f'### name: "{n}"' in wanted]
        names = names[:1] if len(names) > 1 else names
        return json.dumps({"sections": [{"name": n, "summary": f"About {n}.", "findings": [], "strengths": []} for n in names]})


def test_a_section_the_model_skipped_is_asked_for_again_on_its_own() -> None:
    c = ctx()
    provider = LazyProvider()
    result = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", provider, c))
    assert result.status == "done" and set(result.sections) == {"introduction", "methods", "results"}
    assert len(provider.prompts) == 3  # one batch, then the two skipped sections one by one


def test_a_review_that_still_misses_a_section_is_partial_and_names_it() -> None:
    class Never(Provider):
        async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
            return json.dumps({"sections": [{"name": "introduction", "summary": "s", "findings": []}]})

    c = ctx()
    result = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", Never(), c))
    assert result.status == "partial" and "methods" in result.note and "results" in result.note


def test_a_failed_review_keeps_the_report_and_says_so() -> None:
    c = ctx()
    result = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", Provider(fail=True), c))
    out = sr.apply_review(analysis(), result, c)
    assert result.status == "failed" and out.sections["methods"].findings == []
    assert any("could not be completed" in note for note in out.grounding_notes)


def test_a_quote_with_a_greek_letter_spelled_out_is_found() -> None:
    text = METHODS + " The optimal parameter set ϕ∗ maximizes the likelihood of the training molecules."
    c = ctx(section_texts={"introduction": INTRO, "methods": text, "results": RESULTS},
            source_text=f"{INTRO}\n\n{text}\n\n{RESULTS}", body=f"{INTRO}\n\n{text}\n\n{RESULTS}")
    greek = finding("The optimal parameter set phi* maximizes the likelihood of the training molecules",
                    "The optimal parameter set phi is defined without the constraints of the likelihood maximisation.",
                    "State the constraints of the likelihood maximisation for the parameter set phi.")
    out = review({"sections": [{"name": "methods", "findings": [greek]}]}, c)
    assert len(out.sections["methods"].findings) == 1


def test_a_quotation_that_is_right_at_its_start_and_drifts_later_is_not_accepted() -> None:
    drifted = dict(GOOD, quote="We empirically deduce that a target mutual information of 4.85 leads to a different and stable outcome")
    out = review({"sections": [{"name": "methods", "findings": [drifted]}]})
    assert out.sections["methods"].findings == []


def test_a_quotation_with_an_ellipsis_needs_each_piece_in_the_paper() -> None:
    joined = dict(GOOD, quote="We empirically deduce that a target mutual information of 4.85 … leads to optimal and stable training results")
    assert len(review({"sections": [{"name": "methods", "findings": [joined]}]}).sections["methods"].findings) == 1


# ------------------------------------------------------------------ the fix plan

def _check(label, status="issue", severity="minor", fix="Do the thing.", quote="", detail=""):
    return {"key": label, "label": label, "status": status, "severity": severity, "detail": detail, "quote": quote, "fix": fix}


def test_the_fix_plan_lists_major_before_minor_and_review_before_checklist_and_skips_what_is_met() -> None:
    plan = sr.build_fix_plan(
        [{"severity": "minor", "problem": "p-minor " * 4, "quote": "q1", "fix": "fix-minor"},
         {"severity": "major", "problem": "p-major " * 4, "quote": "q2", "fix": "fix-major"}],
        [_check("Random seed", "missing", "minor", "Give the seed."), _check("Ethics", "missing", "major", "Name the committee."),
         _check("Hardware", "met", None, "unused"), _check("Spread", "missing", "minor", "")],  # no fix: not a step
    )
    assert [(s["step"], s["source"], s["action"]) for s in plan] == [
        (1, "review", "fix-major"), (2, "checklist", "Name the committee."), (3, "review", "fix-minor"), (4, "checklist", "Give the seed."),
    ]
    assert plan[0]["quote"] == "q2"


def test_the_fix_plan_has_at_most_six_steps() -> None:
    many = [{"severity": "minor", "problem": f"problem {i} " * 3, "quote": "q", "fix": f"fix {i}"} for i in range(10)]
    assert len(sr.build_fix_plan(many, [])) == sr.MAX_FIX_STEPS


def test_a_reviewed_section_gets_its_plan_and_the_vague_one_line_fix_is_replaced() -> None:
    out = review({"sections": [{"name": "methods", "findings": [GOOD]}]})
    methods = out.sections["methods"]
    assert methods.fix_plan[0].action == GOOD["fix"] and methods.fix == GOOD["fix"] and methods.fix_plan[0].quote == GOOD["quote"]
    assert out.sections["introduction"].fix_plan == []  # reviewed with nothing to fault: nothing to fix


def test_a_reviewed_section_with_nothing_to_fix_no_longer_carries_the_models_generic_fix() -> None:
    out = review({"sections": [{"name": "results", "findings": []}]})
    assert out.sections["results"].fix is None and out.sections["results"].issue is None


def test_without_a_review_the_checklist_still_gives_a_plan() -> None:
    c = ctx()
    check = _check("Random seed", "missing", "minor", "Give the seed.")
    out = sr.apply_review(analysis({"methods": [check]}), sr.SectionReview(), c)
    assert [s.action for s in out.sections["methods"].fix_plan] == ["Give the seed."]
    assert out.sections["methods"].score == 70  # no review: the model's score stands


def test_a_finding_about_the_same_topic_as_a_checklist_item_is_left_out_whatever_it_quotes() -> None:
    check = _check("Data / code availability", "missing", "minor", "Add a statement.", detail="No statement says where the data can be found.")
    same_topic = finding("We generate 5000 new samples by randomly appending substructures to existing molecules",
                         "The paper lacks a statement on data and code availability, which is needed to reproduce the 5000 samples.",
                         "Add a statement saying where the data and the code for the 5000 samples can be accessed.")
    other = dict(GOOD)
    out = review({"sections": [{"name": "methods", "findings": [same_topic, other]}]}, checks={"methods": [check]})
    assert [f.quote for f in out.sections["methods"].findings] == [other["quote"]]


def test_a_step_about_words_outside_the_text_calibrate_receives_is_marked() -> None:
    inside = finding("We empirically deduce that a target mutual information of 4.85 leads to optimal", GOOD["problem"], GOOD["fix"])
    outside = finding("We generate 5000 new samples by randomly appending substructures to existing molecules",
                      "The 5000 samples are generated randomly but the random seed for the substructures is not given.",
                      "Give the random seed used to append the substructures, so the 5000 samples can be regenerated.", "minor")
    c = ctx()
    result = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", Provider({"sections": [{"name": "methods", "findings": [inside, outside]}]}), c))
    base = analysis()
    payload = base.model_dump(mode="json")
    payload["sections"]["methods"]["tool_text"] = "We empirically deduce that a target mutual information of 4.85 leads to optimal and stable training results."
    out = sr.apply_review(AnalysisResult.model_validate(payload), result, c)
    flags = {s.quote[:12]: s.in_calibrate_text for s in out.sections["methods"].fix_plan}
    assert flags == {"We empirical": True, "We generate ": False}


def test_a_step_without_a_quotation_applies_to_the_whole_section() -> None:
    check = _check("Random seed", "missing", "minor", "Give the seed.")
    out = sr.apply_review(analysis({"methods": [check]}), sr.SectionReview(), ctx())
    assert out.sections["methods"].fix_plan[0].in_calibrate_text is True


def test_a_missing_checklist_item_is_an_add_step_and_a_problem_in_the_text_is_a_change_step() -> None:
    plan = sr.build_fix_plan([], [_check("Random seed", "missing", "minor", "Give the seed."),
                                  _check("Software versions", "issue", "minor", "Give versions.", quote="Keras [41]")])
    assert {s["problem"].split(".")[0]: s["kind"] for s in plan} == {"Random seed": "add", "Software versions": "change"}
