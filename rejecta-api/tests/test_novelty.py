"""The detailed novelty assessment from the claim ledger, checked against the paper (services.novelty).

The paper below is made up for the tests; each test names the rule it guards."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from models import AnalysisResult
from services import novelty as nv
from services import paper_profile as pp
from services.analysis_guard import AnalysisContext
from services.journal_policy import policy_for

ABSTRACT = "We present the most comprehensive benchmark of molecular property models to date across many datasets."
INTRO = (
    "Earlier studies reported that deep models underperform non-deep models on small molecular datasets (Jiang et al., 2021). "
    "Label smoothness explains generalisation in kernel methods (Rahaman et al., 2019). "
    "Contrary to Smith et al. (2020), we question whether dataset size alone explains the gap. "
    "Unlike earlier benchmarks (Mayr et al., 2018), we add Transformers and 3D descriptors.\n\n"
    "Lee et al. (2019) attribute the gap to the small size of molecular datasets. "
    "We show that the gap cannot be solely attributed to dataset size."
)
RESULTS = (
    "Deep models underperform tree models on 10 of 14 datasets, as Table 1 shows. "
    "The standard deviations of Table 1 can be seen in the appendix.\n\n"
    "We add an activity-cliff experiment that decreases label smoothness, shown in Figure 2. "
    "Removing cliff molecules as a counter-test restores the deep models, see Table 2. "
    "Smoothing uses the Tanimoto similarity of ECFP fingerprints between molecules."
)
METHODS = "Fingerprints are the inputs of SVM, XGB, RF and MLP in our benchmark. All experiments are repeated over five random seeds."
APPENDIX = ("Figure 4 repeats the cliff experiment on two more datasets. "
            "The orthogonal transform maps each input fingerprint to a dense real-valued vector.")
ROLES = {"introduction": "introduction", "results": "results", "methods": "methods", "appendix a": "other"}
JOURNAL = "Nature"


def ctx(appendix: str = APPENDIX, methods: str = METHODS, page_texts: list[str] | None = None) -> AnalysisContext:
    sections = {"introduction": INTRO, "results": RESULTS, "methods": methods, "appendix a": appendix}
    return AnalysisContext(
        source_text="\n\n".join([ABSTRACT, *sections.values()]), body="\n\n".join(sections.values()),
        structure_mode="detected", section_texts=sections, section_roles=ROLES,
        section_titles={"introduction": "Introduction", "results": "Results", "methods": "Methods",
                        "appendix a": "More cliffs"},
        article_type=pp.RESEARCH_ARTICLE, abstract=ABSTRACT, numbers_text=RESULTS,
        figures=[{"label": "Table 1", "caption": "AUC on 14 datasets. The standard deviations can be found in the appendix.", "page": 5},
                 {"label": "Figure 2", "caption": "Smoothed datasets. Left: ESOL (Regression); Middle: Lipop (Regression); Right: QM7 (Regression).", "page": 6},
                 {"label": "Table 2", "caption": "RMSE on cliff molecules.", "page": 6},
                 {"label": "Figure 4", "caption": "Cliffs on more datasets; error bars show SD over 5 runs.", "page": 9}],
        section_labels={"introduction": "1", "results": "3", "methods": "4", "appendix a": "A"},
        section_parts={"introduction": "main", "results": "main", "methods": "main", "appendix a": "appendix"},
        section_pages={"introduction": 1, "results": 3, "methods": 8, "appendix a": 9},
        page_texts=page_texts or [], journal=JOURNAL,
    )


CLAIMS = [
    {"id": "C1", "statement": "Deep models underperform non-deep models", "type": "comparative", "main": True,
     "evidence_ids": ["P4.1"]},
    {"id": "C2", "statement": "Low label smoothness causes the gap", "type": "causal", "main": True,
     "evidence_ids": ["P5.1"]},
    {"id": "C3", "statement": "Tree models with fingerprints beat deep models", "type": "comparative", "main": False,
     "evidence_ids": ["P4.1"]},
    {"id": "C4", "statement": "The gap cannot be solely attributed to dataset size", "type": "causal", "main": False,
     "evidence_ids": ["P3.2"]},
]


def report(claims: list[dict[str, Any]] | None = None) -> AnalysisResult:
    return AnalysisResult.model_validate({
        "mood": "neutral", "mood_score": 70, "verdict": "v", "oracle_letter": "l", "sections": {},
        "rejection_reasons": [], "criteria": {}, "novelty_assessment": "The paper provides new insights.",
        "journal_fit": "j", "provider_used": "t", "evidence_ledger": {"claims": claims or CLAIMS},
    })


class Model:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def generate(self, system: str, prompt: str, temperature: float = 0.2, max_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


ANSWER: dict[str, Any] = {
    "prior_work": [
        {"ids": ["P2.1"], "citations": ["Jiang et al., 2021"], "covers": "non-deep models",
         "applies_to": [{"claim": "C1", "relation": "same_finding"}, {"claim": "C2", "relation": "same_finding"}]},
        {"ids": ["P2.3"], "citations": ["Smith et al. (2020)"], "covers": "dataset size",
         "applies_to": [{"claim": "C2", "relation": "builds_on"}]},
        {"ids": ["P3.1"], "citations": ["Lee et al. (2019)"], "covers": "small size of molecular datasets",
         "applies_to": [{"claim": "C4", "relation": "builds_on"}]},
    ],
    "claims": [
        {"claim": "C1", "scope": {"text": "models on 14 datasets", "ids": ["P4.1"]}, "contribution_type": "benchmark",
         "delta": {"text": "", "ids": [], "shown_in": []}, "evidence_kind": "none", "support": "descriptive",
         "strength": {}},
        {"claim": "C2", "scope": {"text": "regression datasets", "ids": ["P5.1"]}, "contribution_type": "explanation",
         "delta": {"text": "an activity-cliff experiment that lowers label smoothness", "ids": ["P5.1"],
                   "shown_in": ["Fig. 2"]},
         "evidence_kind": "new_experiment", "support": "causal",
         "intervention": {"target": "labels",
                          "representation": {"text": "Tanimoto similarity of ECFP fingerprints", "ids": ["P5.3"]},
                          "models": {"names": ["SVM", "XGB", "RF"], "ids": ["P6.1"]}},
         "strength": {"counts": [], "control": {"text": "removing cliff molecules", "ids": ["P5.2"]}},
         "limits": {"text": "only the three regression datasets of Figure 2 are smoothed", "ids": ["P5.1"]},
         "skeptic": {"text": "the smoothing uses the Tanimoto similarity of ECFP fingerprints, the inputs of the winning models", "ids": ["P5.3"]},
         "strengthen": {"text": "vary label smoothness with a similarity no compared model uses as input, keeping the models and datasets fixed", "ids": ["P5.3"]}},
        {"claim": "C3", "scope": {"text": "tree models", "ids": ["P4.1"]}, "contribution_type": "empirical_finding",
         "delta": {"text": "tree models with fingerprints win on 10 of 14 datasets", "ids": ["P4.1"],
                   "shown_in": ["Table 1"]},
         "evidence_kind": "new_experiment", "support": "descriptive",
         "strength": {"counts": [{"n": "14", "what": "datasets", "ids": ["P4.1"]}]}},
        {"claim": "C4", "scope": {"text": "dataset size", "ids": ["P3.2"]},
         "delta": {"text": "", "ids": [], "shown_in": []}, "evidence_kind": "none", "support": "descriptive"},
    ],
    "superlatives": [],
    "overall": {"strongest_new": {"claim": "C2", "text": "The cliff experiment of Figure 2 is the new part.", "ids": ["P5.1"]},
                "strongest_overlap": {"claim": "C1", "text": "It provides new insights.", "ids": ["P2.1"]},
                "why_verdict": {"text": "The benchmark repeats Jiang et al.; only the cliff test is new.", "ids": ["P2.1", "P5.1"]}},
    "verdict": "incremental", "reason": "The main finding was reported by Jiang et al.; the cliff test is new.",
    "reason_ids": ["P2.1", "P5.1"],
}


def run(answer: dict[str, Any] = ANSWER, claims: list[dict[str, Any]] | None = None,
        context: AnalysisContext | None = None) -> AnalysisResult:
    return asyncio.run(nv.assess(report(claims), context or ctx(), Model(answer)))


def by_claim(out: AnalysisResult) -> dict[str, dict[str, Any]]:
    return {c["claim"]: c for c in out.novelty["contributions"]}


# 1. Variance: repeats, error bars and spread separately; never "stated in the methods" without spread values.

def test_repeats_from_the_methods_are_not_spread_values() -> None:
    out = run()
    c2 = by_claim(out)["C2"]["strength"]
    assert c2["repeats"]["ids"] == ["P6.2"] and c2["repeats"]["covers"] == ["as stated, “All experiments”"]
    assert c2["spread"]["status"] == "not_given"
    assert "Repeats: All experiments are repeated over five random seeds; it covers as stated, “All experiments”. [P6.2]" in out.novelty_assessment
    assert "stated in the methods" not in out.novelty_assessment


def test_error_bars_per_figure_and_whether_the_caption_defines_them() -> None:
    out = run()
    assert by_claim(out)["C2"]["strength"]["error_bars"] == [
        {"label": "Figure 2", "caption_defines": False, "definition": ""}]
    assert "the caption of Figure 2 does not define any; whether they are drawn cannot be read" in out.novelty_assessment


def test_spread_pointer_on_a_benchmark_claim_is_referred_to_not_shown() -> None:
    out = run()
    spread = by_claim(out)["C3"]["strength"]["spread"]
    assert spread["status"] == "referred_not_shown" and spread["ids"] == ["P4.2"]
    assert by_claim(out)["C1"]["strength"]["spread"]["status"] == "referred_not_shown"  # benchmark claims too
    shown = by_claim(run(context=ctx(appendix="AUC on BACE is 0.81 ± 0.02 over five runs.")))["C3"]["strength"]["spread"]
    assert shown["status"] == "shown" and shown["text"].startswith("shown in Appendix A")


# 2. Float location.

def test_main_text_float_shows_page_and_citing_section_appendix_float_its_appendix() -> None:
    out = run()
    places = {p["label"]: p for p in by_claim(out)["C3"]["places"]}
    assert nv.place_text(places["Table 1"]) == "Table 1, p. 5, cited in Section 3: Results"
    claims = copy.deepcopy(CLAIMS)
    claims[1]["evidence_ids"] = ["P5.1", "P7.1"]
    fig4 = {p["label"]: p for p in by_claim(run(claims=claims))["C2"]["places"]}["Figure 4"]
    assert nv.place_text(fig4) == "Figure 4, Appendix A: More cliffs, p. 9"


# 3. Prior-work scope: same subject and same assertion.

def test_outcome_sentence_does_not_cover_a_causal_claim() -> None:
    out = run()
    found = by_claim(out)
    assert [p["citations"] for p in found["C1"]["prior"]] == [["Jiang et al., 2021"]]
    assert all("Jiang et al., 2021" not in p["citations"] for p in found["C2"]["prior"])
    assert any("about an outcome, not the cause" in d for d in out.novelty["dropped"])


def test_category_sentence_covers_member_claims() -> None:
    claims = copy.deepcopy(CLAIMS)
    claims[2]["statement"] = "Tree models, which are non-deep models, beat deep models on small molecular datasets"
    c3 = by_claim(run(claims=claims))["C3"]
    assert [(p["covers"], p["by_scope"]) for p in c3["prior"]] == [("non-deep models", True)]


# 4. Questioning or limiting an earlier belief is "challenges".

def test_questioning_an_earlier_belief_is_challenges() -> None:
    found = by_claim(run())
    assert [p["relation"] for p in found["C2"]["prior"]] == ["challenges"]  # "Contrary to Smith et al."
    assert [p["relation"] for p in found["C4"]["prior"]] == ["challenges"]  # the next sentence limits Lee et al.


# 5. Every figure and table the claim's sentences or the experiment's paragraph cite.

def test_every_table_of_the_experiment_is_attached() -> None:
    c2 = by_claim(run())["C2"]
    assert [p["label"] for p in c2["places"]] == ["Figure 2", "Table 2"]  # the model named only Figure 2
    assert [p["label"] for p in c2["delta"]["shown_in"]] == ["Figure 2"]


# 6. Confound in plain words; an intervention on the models' own inputs is not a confound.

def test_confound_names_representation_models_and_whether_it_is_the_same() -> None:
    out = run()
    i = by_claim(out)["C2"]["intervention"]
    assert i["kind"] == "confound" and i["family"] == "fingerprints" and not i["same"]
    assert i["models"]["names"] == ["SVM", "XGB", "RF"]
    assert ("SVM, XGB, RF take fingerprints as input [P6.1] — a related representation of the same family "
            "(fingerprints), not the same one") in out.novelty_assessment
    same = by_claim(run(context=ctx(methods="ECFP fingerprints are the inputs of SVM, XGB, RF and MLP. "
                                            "All experiments are repeated over five random seeds.")))["C2"]["intervention"]
    assert same["same"] and same["shared"] == ["ecfp"]


def test_models_named_from_a_sentence_that_does_not_state_their_input_are_replaced() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["intervention"]["models"] = {"names": ["MLP"], "ids": ["P5.2"]}  # P5.2 says nothing of inputs
    i = by_claim(run(answer))["C2"]["intervention"]
    assert i["models"]["ids"] == ["P6.1"] and i["models"]["names"] == ["SVM", "XGB", "RF", "MLP"]
    assert i["terms"] == ["TANIMOTO", "ECFP"] or i["terms"] == ["Tanimoto", "ECFP"]


def test_claim_on_the_same_figure_shares_the_intervention() -> None:
    claims = copy.deepcopy(CLAIMS)
    claims[3]["evidence_ids"] = ["P3.2", "P5.1"]  # C4 also rests on the cliff experiment of Figure 2
    out = run(claims=claims)
    i = by_claim(out)["C4"]["intervention"]
    assert i["kind"] == "same_as" and i["claim"] == "C2" and i["figure"] == "Figure 2"
    assert "Same intervention as C2 (both rest on Figure 2)" in out.novelty_assessment


def test_confound_found_by_code_when_the_model_gives_none() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1].pop("intervention")
    i = by_claim(run(answer))["C2"]["intervention"]
    assert i["representation"]["ids"] == ["P5.3"] and i["models"]["names"] == ["SVM", "XGB", "RF", "MLP"]


def test_intervention_on_inputs_reports_side_effects_not_a_confound() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["intervention"] = {
        "target": "inputs", "representation": {"text": "an orthogonal transform of the input fingerprints", "ids": ["P7.2"]},
        "side_effects": {"text": "binary sparse fingerprints become dense real-valued vectors", "ids": ["P7.2"]}}
    out = run(answer)
    i = by_claim(out)["C2"]["intervention"]
    assert i["kind"] == "inputs" and i["side_effects"]["ids"] == ["P7.2"]
    assert "Possible confound" not in out.novelty_assessment and "not itself a confound" in out.novelty_assessment
    answer["claims"][1]["intervention"].pop("side_effects")
    assert "Whether it also changes the sparsity, scale, type or dimensionality" in run(answer).novelty_assessment


def test_a_description_of_inputs_is_not_an_intervention_and_labels_are_read_from_the_text() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][2]["intervention"] = {"target": "inputs",
                                           "representation": {"text": "fingerprints", "ids": ["P6.1"]}}
    assert by_claim(run(answer))["C3"]["intervention"] is None  # P6.1 describes the inputs; nothing is manipulated
    answer["claims"][1]["intervention"]["target"] = "inputs"  # the model says inputs; P5.1 changes label smoothness
    answer["claims"][1]["intervention"]["representation"] = {"text": "label smoothing", "ids": ["P5.1"]}
    i = by_claim(run(answer))["C2"]["intervention"]
    assert i["kind"] == "confound" and i["representation"]["ids"] == ["P5.1", "P5.3"]


def test_generic_depth_notes_are_dropped() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["limits"] = {"text": "Limited to the datasets and models tested", "ids": ["P5.1"]}
    answer["claims"][1]["strengthen"] = {"text": "Testing additional datasets to confirm generalizability", "ids": ["P5.1"]}
    answer["claims"][1]["skeptic"] = {"text": "The improvement might not generalize to a broader range of data", "ids": ["P5.1"]}
    c2 = by_claim(run(answer))["C2"]
    assert c2["limits"]["ids"] == [] and c2["skeptic"]["ids"] == []
    assert c2["strengthen"].get("from_code")  # a causal claim gets the isolating control written by code


def test_overview_figure_cited_in_the_introduction_is_not_evidence() -> None:
    claims = copy.deepcopy(CLAIMS)
    claims[0]["evidence_ids"] = ["P4.1", "P2.4"]
    labels = [p["label"] for p in by_claim(run(claims=claims))["C1"]["places"]]
    assert labels == ["Table 1"]


# 7. A caption that names its datasets is counted.

def test_caption_that_enumerates_datasets_is_counted() -> None:
    out = run()
    counts = by_claim(out)["C2"]["strength"]["counts"]
    assert counts == [{"n": "3", "what": "datasets", "ids": [], "from_caption": True, "source": "Figure 2",
                       "names": ["ESOL", "Lipop", "QM7"]}]
    assert "3 datasets (from the caption of Figure 2: ESOL, Lipop, QM7)" in out.novelty_assessment


def test_full_caption_is_read_from_the_page_text() -> None:
    page = ("Results text.\nTable 2. RMSE on cliff molecules for the targets. Left: CB1; Middle: DRD3;\n"
            "Right: PPARy.\nthe body text goes on here.")
    context = ctx(page_texts=["", "", "", "", "", page])
    caption = nv.full_caption("Table 2", context)
    assert caption.endswith("Right: PPARy.") and "body text" not in caption
    assert nv.caption_count(caption)["n"] == "3"


# 8. Depth fields.

def test_depth_fields_are_kept_with_ids_and_shown() -> None:
    out = run()
    c2 = by_claim(out)["C2"]
    assert c2["contribution_type"] == "explanation"
    assert c2["limits"]["ids"] == ["P5.1"] and c2["skeptic"]["ids"] == ["P5.3"] and c2["strengthen"]["ids"] == ["P5.3"]
    text = out.novelty_assessment
    assert "Limits: only the three regression datasets of Figure 2 are smoothed. [P5.1]" in text
    assert "A skeptic would object: the smoothing uses the Tanimoto similarity" in text
    assert "Would be stronger with: vary label smoothness" in text
    overall = out.novelty["overall"]
    assert [o["role"] for o in overall] == ["strongest_new", "strongest_overlap", "why_verdict"]
    assert overall[0]["text"] == "The cliff experiment of Figure 2 is the new part."
    assert overall[1]["text"].startswith("Strongest overlap with cited work: C1")  # the vague sentence is replaced by code
    tier = policy_for(JOURNAL).tier
    assert out.novelty["journal"]["tier"] == tier and "Against the journal:" in text


def test_depth_note_without_ids_is_dropped() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["skeptic"] = {"text": "it may not generalise", "ids": []}
    out = run(answer)
    assert by_claim(out)["C2"]["skeptic"] == {"text": "", "ids": []}
    assert any("skeptic" in d for d in out.novelty["dropped"])


# 9. "new" is labelled against cited work.

def test_new_verdict_is_labelled_vs_cited_work() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["prior_work"] = []
    out = run(answer)
    assert out.novelty["verdict"] == "new" and out.novelty["verdict_label"] == "new (vs cited work)"
    assert "Verdict: new (vs cited work) —" in out.novelty_assessment


# Earlier rules, kept.

def test_reused_table_is_reanalysis_and_new_requires_a_new_experiment() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][0]["delta"] = {"text": "benchmarks the models", "ids": ["P4.1"], "shown_in": ["Table 1"]}
    answer["claims"][0]["evidence_kind"] = "new_experiment"
    found = by_claim(run(answer))
    assert found["C1"]["evidence_kind"] == "new_experiment"
    assert found["C2"]["evidence_kind"] == "new_experiment"
    assert found["C3"]["evidence_kind"] == "reanalysis_of_existing_results" and found["C3"]["status"] == "incremental"


def test_counts_must_be_in_their_sentences_or_captions() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][2]["strength"]["counts"] = [{"n": "12", "what": "datasets", "ids": ["P4.1"]}]
    out = run(answer)
    assert by_claim(out)["C3"]["strength"]["counts"] == []
    assert any("12 datasets" in d for d in out.novelty["dropped"])


def test_theory_is_named_by_its_citation() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["theory"] = {"ids": ["P2.2"], "note": "a learning theory"}
    out = run(answer)
    assert by_claim(out)["C2"]["theory"]["citations"] == ["Rahaman et al., 2019"]
    assert "rests on Rahaman et al., 2019" in out.novelty_assessment


def test_superlatives_backed_only_on_the_axes_the_comparison_names() -> None:
    sup = run().novelty["superlatives"]
    assert [s["ids"] for s in sup] == [["P1.1"]] and not sup[0]["supported"]
    answer = copy.deepcopy(ANSWER)
    answer["superlatives"] = [{"ids": ["P1.1"], "text": "most comprehensive", "evidence_ids": ["P2.4"],
                               "axes_backed": ["models", "descriptors", "datasets"]}]
    s = run(answer).novelty["superlatives"][0]
    assert s["supported"] and s["axes_backed"] == ["descriptors", "models"] and s["axes_not_covered"] == ["datasets", "metrics"]


def test_vague_delta_and_reason_are_dropped() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["delta"]["text"] = "provides new insights into smoothness"
    answer["reason"] = "A valuable contribution."
    out = run(answer)
    assert by_claim(out)["C2"]["delta"]["text"] == "" and out.novelty["reason"] == ""


def test_overlapping_when_every_main_claim_was_reported() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"] = answer["claims"][:1]
    assert run(answer).novelty["verdict"] == "overlapping"


def test_prompt_is_generic_and_lists_floats_with_location() -> None:
    model = Model(ANSWER)
    asyncio.run(nv.assess(report(), ctx(), model))
    prompt = model.prompts[0]
    assert "- Figure 4, Appendix A: More cliffs, p. 9: Cliffs on more datasets" in prompt
    rules = nv._RULES
    for specific in ("SphereNet", "Transformer", "ECFP", "Tanimoto", "Mayr", "P5.3", "50 seeds", "non-deep"):
        assert specific not in rules


def test_unknown_claims_and_no_ledger_leave_the_text_alone() -> None:
    assert run({"claims": [{"claim": "C9"}]}).novelty_assessment == "The paper provides new insights."
    bare = report().model_copy(update={"evidence_ledger": None})
    assert asyncio.run(nv.assess(bare, ctx(), Model(ANSWER))).novelty is None


# Round 3.

SUBSET_METHODS = (
    "Fingerprints are the inputs of SVM, XGB, RF and MLP in our benchmark. "
    "All the models are repeated over 50 random seeds, except on QM7 and Lipop, where only five seeds are used. "
    "Hyperparameters are tuned with 100 trials for the tree models and 20 trials for the deep models. "
    "Datasets with fewer than 100 molecules are excluded. "
    "Molecules are split by scaffold into train and test sets."
)


def test_r3_1_repeats_read_exception_clauses_and_apply_to_the_figure_datasets() -> None:
    out = run(context=ctx(methods=SUBSET_METHODS))
    r = by_claim(out)["C2"]["strength"]["repeats"]
    assert r["covers"] == []  # "all the models" sits in a clause that has an exception
    counts = [(x["count"], x["kind"], x["exception"]) for x in r["subsets"]]
    assert counts == [("50 random seeds", "repeats", False), ("five seeds", "repeats", True),
                      ("100 trials", "tuning", False), ("20 trials", "tuning", False)]
    assert {"label": "Figure 2", "text": "50 random seeds for ESOL; five seeds for Lipop, QM7"} in r["applies"]
    text = out.novelty_assessment
    assert "the count differs by subset" in text and "covers as stated" not in text
    assert "Repeats for Figure 2: 50 random seeds for ESOL; five seeds for Lipop, QM7." in text


def test_r3_2_skeptic_and_limits_need_a_concrete_detail_and_checks_fill_the_objection() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["skeptic"] = {"text": "the evaluation design could bias the comparison", "ids": ["P5.1"]}
    answer["claims"][1]["limits"] = {"text": "the conclusions rest on few experiments", "ids": ["P5.1"]}
    out = run(answer, context=ctx(methods=SUBSET_METHODS))
    found = by_claim(out)
    assert found["C2"]["limits"]["ids"] == []
    assert any("names no concrete detail" in d for d in out.novelty["dropped"])
    checks = {d["check"]: d for d in out.novelty["design_checks"]}
    assert checks["unequal_budget"]["ids"] == ["P6.3"] and checks["split"]["ids"] == ["P6.5"]
    assert checks["exclusion"]["ids"] == ["P6.4"]
    c3 = found["C3"]["skeptic"]  # comparative claim: unequal tuning budgets is the objection
    assert c3["from_check"] and c3["ids"] == ["P6.3"] and "not tuned equally" in c3["text"]
    plain = run(answer)
    assert {d["check"]: d for d in plain.novelty["design_checks"]}["split"]["text"] == "the paper does not say how the data are split"


def test_r3_2_task_and_metric_mix_between_compared_datasets() -> None:
    claims = copy.deepcopy(CLAIMS)
    claims[0]["evidence_ids"] = ["P4.1", "P5.2"]  # Table 1 (AUC) and Table 2 (RMSE)
    c1 = by_claim(run(claims=claims))["C1"]
    mix = [d for d in c1["checks"] if d["check"] == "task_metric_mix"]
    assert mix and "AUC: higher is better" in mix[0]["text"] and "RMSE: lower is better" in mix[0]["text"]


def test_r3_3_strengthen_must_isolate_the_stated_cause() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][1]["strengthen"] = {"text": "add more baselines, other metrics and larger models", "ids": ["P5.1"]}
    out = run(answer)
    s = by_claim(out)["C2"]["strengthen"]
    assert s["from_code"] and s["text"].startswith("a control that varies label smoothness while holding")
    assert any("does not name a control" in d for d in out.novelty["dropped"])
    assert by_claim(out)["C4"]["strengthen"]["text"].startswith("a control that varies dataset size")


def test_r3_4_overall_has_three_roles_and_drops_an_unverified_superlative() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["overall"]["strongest_new"]["text"] = "The most comprehensive benchmark adds the cliff experiment."
    out = run(answer)
    overall = out.novelty["overall"]
    assert overall[0]["text"].startswith("Strongest new claim: C2")  # code wrote it instead
    assert all("most comprehensive" not in o["text"] for o in overall)
    assert any("unverified “most comprehensive”" in d for d in out.novelty["dropped"])
    assert overall[2]["text"].startswith("The benchmark repeats Jiang")


def test_r3_5_evidence_includes_every_float_of_the_paragraph_that_introduces_the_experiment() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["claims"][3]["delta"] = {"text": "a cliff test beyond dataset size", "ids": ["P3.2"], "shown_in": ["Figure 2"]}
    labels = [p["label"] for p in by_claim(run(answer))["C4"]["places"]]
    assert labels == ["Figure 2", "Table 2"]  # Table 2 is introduced in the paragraph that introduces Figure 2


def test_r3_6_prior_work_needs_the_same_subject_and_covers_members_of_its_category() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["prior_work"][2]["applies_to"].append({"claim": "C3", "relation": "same_finding"})
    out = run(answer)
    assert all("Lee et al. (2019)" not in p["citations"] for p in by_claim(out)["C3"]["prior"])
    assert any("different subject than C3" in d for d in out.novelty["dropped"])
    claims = copy.deepcopy(CLAIMS)
    claims[2]["statement"] = "RF beats deep models on small molecular datasets"
    methods = METHODS + " Non-deep models such as SVM and RF are fast to train."
    c3 = by_claim(run(claims=claims, context=ctx(methods=methods)))["C3"]
    assert [(p["covers"], p["by_scope"]) for p in c3["prior"]] == [("non-deep models", True)]


def test_r3_7_new_with_correlational_evidence_and_no_control_is_weakly_supported() -> None:
    answer = copy.deepcopy(ANSWER)
    answer["prior_work"] = []
    answer["claims"][1]["strength"]["control"] = {}
    out = run(answer)
    c2 = by_claim(out)["C2"]
    assert c2["status"] == "new" and c2["support"] == "correlational" and c2["status_label"] == "new (weakly supported)"
    assert out.novelty["verdict_label"] == "new (weakly supported)"
    assert "Verdict: new (weakly supported) —" in out.novelty_assessment


def test_r3_8_ranking_statement_checked_against_the_table() -> None:
    from services.ranking_check import ranking_findings

    table = ("[Table 3 (p. 4) as a table. Columns: Dataset | Metric | SVM | XGB | GCN. "
             "Rows: BACE (1,513) | AUC ROC | 0.886 | 0.896 | 0.850; ESOL (1,128) | RMSE | 1.20 | 0.90 | 0.85]")
    text = ("XGB outperforms all other models on BACE, as Table 3 shows. "
            "XGB achieves the best results on ESOL in Table 3. GCN underperforms XGB on BACE.\n\n" + table)
    found = ranking_findings([("results", text)])
    assert len(found) == 1  # BACE is right (AUC: higher is better); ESOL is not (RMSE: lower is better)
    f = found[0]
    assert "ranks XGB above" in f["message"] and "XGB 0.9 and GCN 0.85 (RMSE: lower is better)" in f["message"]
    assert f["quotes"][0]["quote"] == "XGB achieves the best results on ESOL in Table 3."
    assert f["quotes"][1]["quote"] == "ESOL (1,128) | RMSE | 1.20 | 0.90 | 0.85"
    from models import ConsistencyFinding
    assert ConsistencyFinding(kind="ranking_mismatch", severity="minor", message=f["message"]).severity == "minor"
