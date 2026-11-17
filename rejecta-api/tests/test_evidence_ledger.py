"""The evidence-ledger review: text clean-up, the sentence index, search, rubric plan, claim ledger, the checks
every finding passes (evidence ids, absence check, author defence, specificity), and the answer cache."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from config import settings
from models import AnalysisResult
from services import evidence_ledger as el
from services import local_store
from services import paper_profile as pp
from services import rubrics
from services import section_review as sr
from services.analysis_guard import AnalysisContext
from services.claim_ledger import check_ledger
from services.llm.response_cache import cached, usable_text
from services.paper_index import build_index, ids_in, split_sentences
from services.retrieval import PaperSearch
from services.textclean import dehyphenate, vocabulary

# --------------------------------------------------------------------------- text clean-up


def test_line_end_breaks_are_joined_and_compounds_kept() -> None:
    printed = "the model has re-\ncently improved\nwe compare non-deep models\nnon-\ndeep ones and fast\ngrowing data\nrecently"
    vocab = vocabulary(printed + "\nthe fast data growing field")
    text = "has re- cently improved; non- deep ones; a comprehen- sive study; fast- growing field"
    assert dehyphenate(text, vocab) == "has recently improved; non-deep ones; a comprehensive study; fast-growing field"


def test_one_letter_halves_and_chains_are_left_alone() -> None:
    vocab = vocabulary("x ray state of the art")
    assert dehyphenate("an x- ray study", vocab) == "an x-ray study"  # hyphen kept, as before
    assert dehyphenate("state-of-the- art", vocab) == "state-of-the- art"


# --------------------------------------------------------------------------- sentence index

ABSTRACT = "We benchmark 12 models on 14 datasets. Deep models are generally unable to outperform non-deep ones."
INTRO = (
    "Molecular property prediction matters for drug discovery (Smith et al., 2020). Previous studies compared few "
    "models.\n\nTo narrow this gap, we present the most comprehensive benchmark to date."
)
METHODS = (
    "We randomly split the training, validation and test sets at a ratio of 8:1:1. We tune the hyperparameters with "
    "TPE in 50 evaluations. GNN models on HIV are tuned in 30 evaluations.\n\nThe retained features are normalized "
    "to zero mean and unit variance."
)
RESULTS = (
    "As shown in Fig. 2, non-deep models rank first on 10/14 datasets. Deep models underperform non-deep "
    "counterparts in most cases.\n\nTree models rank first on 8/15 datasets."
)
SECTIONS = {"introduction": INTRO, "methods": METHODS, "results": RESULTS}


def test_sentences_do_not_split_at_abbreviations() -> None:
    parts = split_sentences("As shown in Fig. 2, the yield rose (Smith et al., 2020). It fell e.g. at 40 C. Then it stopped.")
    assert parts == ["As shown in Fig. 2, the yield rose (Smith et al., 2020).", "It fell e.g. at 40 C. Then it stopped."]


def test_index_ids_follow_reading_order_and_are_stable() -> None:
    a = build_index(ABSTRACT, SECTIONS)
    b = build_index(ABSTRACT, SECTIONS)
    assert list(a.sentences) == list(b.sentences)
    assert a.sections["abstract"] == ["P1"] and a.sections["introduction"] == ["P2", "P3"]
    assert a.sentences["P1.2"].text.startswith("Deep models are generally")
    assert a.section_of("P4.2") == "methods"
    # sentences that are not next to each other are joined with an ellipsis
    assert a.text_of(["P4.1", "P4.3"]) == f"{a.sentences['P4.1'].text} … {a.sentences['P4.3'].text}"
    assert a.text_of(["P4.1", "P4.2"]) == f"{a.sentences['P4.1'].text} {a.sentences['P4.2'].text}"
    assert "[P4.2] We tune" in a.numbered("methods")


def test_ids_are_read_from_lists_and_strings() -> None:
    assert ids_in(["P1.2", "see P3.4 and P10"]) == ["P1.2", "P3.4", "P10"]
    assert ids_in({"no": "ids"}) == []


# --------------------------------------------------------------------------- search


def test_keyword_search_is_deterministic_and_respects_sections() -> None:
    index = build_index(ABSTRACT, SECTIONS)
    search = PaperSearch(index)
    first = search.search("hyperparameter tuning evaluations", k=2)
    assert first == search.search("hyperparameter tuning evaluations", k=2)
    assert first[0] == "P4"
    assert "P4" not in search.search("hyperparameter tuning evaluations", k=3, exclude_sections={"methods"})


def test_embeddings_are_fused_with_keywords() -> None:
    index = build_index(ABSTRACT, SECTIONS)
    vectors = {pid: [1.0, 0.0] for pid in index.paragraphs}
    vectors["P5"] = [0.0, 1.0]  # only the meaning search can find this paragraph for the query below
    search = PaperSearch(index, vectors, "test")
    assert "P5" in search.search("unrelated words entirely", k=1, query_vector=[0.0, 1.0])


# --------------------------------------------------------------------------- rubric packs and the plan


def test_every_pack_item_is_well_formed() -> None:
    roles = {pp.ROLE_INTRODUCTION, pp.ROLE_METHODS, pp.ROLE_RESULTS, pp.ROLE_DISCUSSION, pp.ROLE_THEMATIC}
    ids: set[str] = set()
    for pack in rubrics.load_packs():
        assert pack.version and (pack.fields or pack.journals)
        for item in pack.items:
            assert item.id.startswith(f"{pack.name}.") and item.id not in ids
            ids.add(item.id)
            assert set(item.triggers_on) <= set(rubrics.CLAIM_TYPES) | {"paper"}
            assert item.ask_in and set(item.ask_in) <= roles
            assert item.severity_missing in rubrics.SEVERITIES and item.severity_problem in rubrics.SEVERITIES
            assert item.fix and item.source and set(item.kinds) <= set(rubrics.KINDS)


def test_packs_follow_field_and_journal() -> None:
    names = lambda field, journal: [p.name for p in rubrics.select_packs(field, journal)]  # noqa: E731
    assert names("cs_ml", "Nature Machine Intelligence") == ["universal", "cs_ml", "nature"]
    assert names("chemistry_catalysis", "ACS Catalysis") == ["universal", "chemistry_catalysis", "chemistry_synthesis"]
    assert "nature" not in names("cs_ml", "Journal of Chemical Information and Modeling")
    assert rubrics.field_from_journal("Journal of Chemical Information and Modeling") == "cs_ml"


CLAIMS = [
    rubrics.Claim("C1", "Deep models are generally unable to outperform non-deep ones", "comparative", True, ("P1.2",)),
    rubrics.Claim("C2", "Tree models rank first on most datasets", "comparative", False, ("P6.1",)),
]
ROLES = {"introduction": "introduction", "methods": "methods", "results": "results"}


def plan(decided: set[str] | None = None, journal: str = "Nature Machine Intelligence") -> list[rubrics.Question]:
    questions, _ = rubrics.build_plan(
        CLAIMS, "cs_ml", "benchmark", journal, ROLES, list(ROLES), ABSTRACT + METHODS + RESULTS, decided or set()
    )
    return questions


def test_plan_asks_field_checks_about_the_papers_claims() -> None:
    questions = plan()
    by_check = {q.item.id: q for q in questions}
    tuning = by_check["cs_ml.tuning_budget"]
    assert tuning.section == "methods" and tuning.claim is not None and tuning.claim.id == "C1"
    assert "Deep models are generally unable" in tuning.text
    assert by_check["nature.significance"].section == "introduction" and by_check["nature.significance"].claim is None
    assert [q.id for q in questions] == [f"Q{n}" for n in range(1, len(questions) + 1)]
    assert [(q.id, q.item.id) for q in questions] == [(q.id, q.item.id) for q in plan()]  # same paper, same plan


def test_plan_skips_what_the_checklist_decided_and_main_only_checks_on_minor_claims() -> None:
    assert not any(q.item.id == "cs_ml.variance" for q in plan({"seed"}))
    assert not any(q.item.id == "universal.claim_support" and q.claim and q.claim.id == "C2" for q in plan())


def test_a_major_check_on_a_minor_claim_counts_as_minor() -> None:
    q = next(q for q in plan() if q.item.id == "universal.comparison_fairness" and q.claim and q.claim.id == "C2")
    assert q.severity("problem") == "minor"


def test_plan_has_at_most_six_questions_per_section() -> None:
    many = [rubrics.Claim(f"C{n}", f"Claim {n} beats the baseline", "comparative", True, ("P1.1",)) for n in range(1, 9)]
    questions, _ = rubrics.build_plan(many, "cs_ml", "method", "Nature", ROLES, list(ROLES), METHODS, set())
    for section in ROLES:
        assert sum(q.section == section for q in questions) <= rubrics.MAX_QUESTIONS_PER_SECTION


# --------------------------------------------------------------------------- the claim ledger


def test_ledger_keeps_only_what_the_paper_supports() -> None:
    index = build_index(ABSTRACT, SECTIONS)
    raw = {
        "field": "cs_ml", "paper_kind": "benchmark",
        "claims": [
            {"statement": "Deep models are generally unable to outperform non-deep ones", "type": "comparative", "main": True, "ids": ["P1.2"]},
            {"statement": "Deep models fail on 20 datasets", "type": "quantitative", "main": True, "ids": ["P1.2"]},  # 20 not there
            {"statement": "Quantum annealing solves protein folding", "type": "causal", "ids": ["P1.2"]},  # nothing shared
            {"statement": "Benchmark of twelve models", "type": "novelty", "ids": ["P99.1"]},  # no such id
            {"statement": "Trees are better", "type": "opinion", "ids": ["P1.2"]},  # unknown type
        ],
        "facts": [{"kind": "dataset", "value": "14 datasets", "ids": ["P1.1"]}, {"kind": "model", "value": "AlphaFold", "ids": ["P1.1"]}],
    }
    ledger = check_ledger(raw, index)
    assert [c.statement for c in ledger.claims] == ["Deep models are generally unable to outperform non-deep ones"]
    assert ledger.claims[0].id == "C1" and ledger.claims[0].main
    assert [f.value for f in ledger.facts] == ["14 datasets"]
    assert ledger.dropped == 5 and ledger.field == "cs_ml"


# --------------------------------------------------------------------------- the whole review, with a scripted model


def context() -> AnalysisContext:
    return AnalysisContext(
        source_text="\n\n".join([ABSTRACT, *SECTIONS.values()]), body="\n\n".join(SECTIONS.values()),
        structure_mode="detected", section_texts=SECTIONS, section_roles=ROLES, section_titles={},
        article_type=pp.RESEARCH_ARTICLE, abstract=ABSTRACT,
    )


class Scripted:
    """Answers each kind of call (ledger, investigation per section, absence check, defence) from a script."""

    def __init__(self, sections: dict[str, dict[str, Any]], absence: dict[str, Any] | None = None, defence: dict[str, Any] | None = None) -> None:
        self.sections = sections
        self.absence = absence or {"items": []}
        self.defence = defence or {"items": []}
        self.calls: list[str] = []

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        if "record, without judging it" in system:
            self.calls.append("ledger")
            return json.dumps({
                "field": "cs_ml", "paper_kind": "benchmark",
                "claims": [{"statement": "Deep models are generally unable to outperform non-deep ones", "type": "comparative", "main": True, "ids": ["P1.2"]}],
                "facts": [{"kind": "metric", "value": "50 evaluations", "ids": ["P4.2"]}],
            })
        if "check claims that a manuscript lacks" in system:
            self.calls.append("absence")
            return json.dumps(self.absence)
        if "act for the authors" in system:
            self.calls.append("defence")
            return json.dumps(self.defence)
        section = prompt.split('SECTION UNDER REVIEW: "', 1)[1].split('"', 1)[0]
        self.calls.append(section)
        return json.dumps(self.sections.get(section, {}))

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def question_id(check: str) -> str:
    questions, _ = rubrics.build_plan(
        [rubrics.Claim("C1", "Deep models are generally unable to outperform non-deep ones", "comparative", True, ("P1.2",))],
        "cs_ml", "benchmark", "Nature Machine Intelligence", ROLES, list(ROLES),
        " ".join(SECTIONS.values()), set(),
    )
    return next(q.id for q in questions if q.item.id == check)


def run(provider: Scripted) -> el.Outcome:
    parts = [el.SectionPart(name, "", role) for name, role in ROLES.items()]
    return asyncio.run(el.review(parts, SECTIONS, "Nature Machine Intelligence", provider, context()))


TUNING_PROBLEM = {
    "status": "problem", "evidence": ["P4.2", "P4.3"],
    "problem": "GNN models on HIV get 30 TPE evaluations while the other models get 50, so the tuning budget differs.",
    "impact": "The comparison behind C1 may favour the non-deep models.",
    "fix": "Give every model 50 TPE evaluations on HIV, or report results with equal budgets in a table.",
}


def test_a_specific_finding_with_evidence_is_kept_with_the_papers_words() -> None:
    q = question_id("cs_ml.tuning_budget")
    outcome = run(Scripted({"methods": {"answers": [{"q": q, **TUNING_PROBLEM}], "summary": "The methods tune models with TPE in 50 evaluations."}}))
    findings = outcome.sections["methods"]["findings"]
    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == "cs_ml.tuning_budget" and f["severity"] == "major" and f["claim_id"] == "C1"
    assert f["quote"] == "We tune the hyperparameters with TPE in 50 evaluations. GNN models on HIV are tuned in 30 evaluations."
    assert f["evidence_ids"] == ["P4.2", "P4.3"] and f["source"].startswith("NeurIPS")
    assert outcome.summary["claims"][0]["findings"] == 1
    assert any(c["check"] == "cs_ml.tuning_budget" and c["status"] == "problem" for c in outcome.summary["checks"])


def test_ids_that_were_not_shown_are_dropped_and_a_problem_without_evidence_goes() -> None:
    q = question_id("cs_ml.tuning_budget")
    answer = {**TUNING_PROBLEM, "evidence": ["P99.1", "Q3"]}  # no such sentence
    outcome = run(Scripted({"methods": {"answers": [{"q": q, **answer}]}}))
    assert outcome.sections["methods"]["findings"] == []
    assert outcome.summary["removed"]["without_evidence"] == 1




def test_absence_check_needs_a_passage_it_was_shown() -> None:
    q = question_id("cs_ml.data_split")
    missing = {"status": "not_reported", "problem": "The split of the 14 datasets is not described anywhere in the methods.",
               "impact": "x", "fix": "State the split ratio and method for every dataset."}
    outcome = run(Scripted({"methods": {"answers": [{"q": q, **missing}]}}, absence={"items": [{"id": "E1", "verdict": "present", "evidence": ["P99.9"]}]}))
    assert len(outcome.sections["methods"]["findings"]) == 1


def test_the_authors_defence_removes_a_criticism_the_paper_answers() -> None:
    q = question_id("cs_ml.tuning_budget")
    outcome = run(Scripted(
        {"methods": {"answers": [{"q": q, **TUNING_PROBLEM}]}},
        defence={"items": [{"id": "D1", "verdict": "resolved", "evidence": ["P4.2"], "note": "budget stated"}]},
    ))
    assert outcome.sections["methods"]["findings"] == []
    assert outcome.summary["removed"]["answered_by_paper"] == 1


def test_vague_findings_do_not_pass_and_a_checks_vague_fix_is_replaced() -> None:
    q = question_id("cs_ml.tuning_budget")
    vague_fix = {**TUNING_PROBLEM, "fix": "Consider adding more details to improve clarity."}
    free = {"evidence": ["P4.1"], "severity": "major", "problem": "The section could be improved overall.",
            "impact": "Readers may struggle.", "fix": "Improve the clarity of the presentation."}
    outcome = run(Scripted({"methods": {"answers": [{"q": q, **vague_fix}], "findings": [free]}}))
    findings = outcome.sections["methods"]["findings"]
    assert len(findings) == 1 and findings[0]["fix"].startswith("Add a table with, for every compared model")
    assert outcome.summary["removed"]["not_specific"] == 1


def test_a_free_finding_is_major_only_about_a_main_claim() -> None:
    free = {"evidence": ["P6.1"], "severity": "major", "claim": None,
            "problem": "Tree models rank first on 8/15 datasets while the paper uses 14 datasets.",
            "impact": "The dataset count is inconsistent.", "fix": "Correct 8/15 to the count over the 14 datasets."}
    outcome = run(Scripted({"results": {"findings": [free]}}))
    assert outcome.sections["results"]["findings"][0]["severity"] == "minor"


def test_same_check_on_two_claims_of_a_section_is_said_once() -> None:
    log: list[dict[str, Any]] = []

    def cand(claim: str, ids: list[str]) -> el._Candidate:
        return el._Candidate("results", {
            "severity": "major", "problem": f"No significance test for claim {claim}.", "evidence_ids": ids,
            "claim_id": claim, "check": "cs_ml.significance", "origin": "rubric",
        }, None, None, "")

    kept = el._merge_overlaps([cand("C1", ["P5.1"]), cand("C3", ["P6.1"])], log)
    assert len(kept) == 1 and log[0]["reason"] == "same point as another finding"


def test_section_review_uses_the_ledger_and_the_report_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EVIDENCE_LEDGER_ENABLED", True)
    q = question_id("cs_ml.tuning_budget")
    provider = Scripted({"methods": {"answers": [{"q": q, **TUNING_PROBLEM}], "summary": "The methods tune models with TPE in 50 evaluations."}})
    c = context()
    review = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "Nature Machine Intelligence", provider, c))
    assert review.ledger is not None and review.ledger["field"] == "cs_ml"
    base = AnalysisResult.model_validate({
        "mood": "neutral", "mood_score": 70, "verdict": "v", "oracle_letter": "l", "criteria": {},
        "sections": {n: {"score": 70, "role": n} for n in ROLES}, "rejection_reasons": [],
        "novelty_assessment": "n", "journal_fit": "j", "provider_used": "test",
    })
    result = sr.apply_review(base, review, c)
    methods = result.sections["methods"]
    assert methods.findings[0].check == "cs_ml.tuning_budget" and methods.findings[0].claim_id == "C1"
    assert methods.fix_plan[0].rule and methods.fix_plan[0].impact
    assert result.evidence_ledger is not None and result.evidence_ledger.claims[0].id == "C1"


def test_a_failed_ledger_review_falls_back_to_the_batch_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EVIDENCE_LEDGER_ENABLED", True)

    async def broken(*args: Any, **kwargs: Any) -> el.Outcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(el, "review_with_budget", broken)
    provider = Scripted({})
    c = context()
    review = asyncio.run(sr.review_sections(sr.inputs_from_parsed(c), "J", provider, c))
    assert review.ledger is None  # the batch review answered


# --------------------------------------------------------------------------- the store and the answer cache


def test_store_never_uses_supabase_or_the_production_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_STORE", "auto")
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@db.abc.supabase.co:5432/postgres")
    assert local_store.Store().name == "files"
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@localhost:5432/app")
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@localhost:5432/app")
    assert local_store.Store().name == "files"
    monkeypatch.setattr(settings, "RAG_DATABASE_URL", "postgresql://u:p@localhost:5432/rejecta_local")
    assert local_store.Store().name == "postgres"


def test_file_store_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_STORE", "files")
    monkeypatch.setattr(settings, "RAG_CACHE_DIR", str(tmp_path))
    local_store.reset_store()
    store = local_store.get_store()

    async def go() -> tuple[Any, dict[str, list[float]]]:
        await store.put("llm", "k1", {"a": 1})
        await store.put_vectors("m", {"h1": [0.5, 0.25]})
        fresh = local_store.Store()  # a new process reads from disk
        return await fresh.get("llm", "k1"), await fresh.get_vectors("m", ["h1", "h2"])

    value, vectors = asyncio.run(go())
    assert value == {"a": 1} and vectors == {"h1": [0.5, 0.25]}


def test_same_request_gets_the_stored_answer_and_failures_are_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_RESPONSE_CACHE", True)
    local_store.reset_store()
    answers = iter(["not json", '{"n": 1}', '{"n": 2}'])
    calls: list[str] = []

    async def call() -> str:
        calls.append("x")
        return next(answers)

    async def go() -> list[str]:
        request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        return [await cached(request, call, usable_text) for _ in range(3)]

    assert asyncio.run(go()) == ["not json", '{"n": 1}', '{"n": 1}']
    assert len(calls) == 2


# --------------------------------------------------------------------------- the report agrees with the sections


def _report_with(findings: dict[str, list[dict[str, Any]]], reasons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "sections": {name: {"findings": items, "title": name.title()} for name, items in findings.items()},
        "rejection_reasons": reasons or [],
        "criteria": {"rigor": {"score": 90}, "evidence_support": {"score": 88}, "journal_fit": {"score": 80}},
        "evidence_ledger": {"claims": [{"id": "C1", "main": True}, {"id": "C2", "main": False}]},
    }


def _major(problem: str, check: str | None, claim: str | None, quote: str = "") -> dict[str, Any]:
    return {"severity": "major", "problem": problem, "fix": "Give every model 50 evaluations.", "quote": quote,
            "check": check, "claim_id": claim, "origin": "rubric" if check else "review", "impact": "C1 may not hold."}


def test_verified_major_findings_become_report_concerns_main_claims_first() -> None:
    payload = _report_with({
        "introduction": [_major("The significance beyond the subfield is not stated in the introduction.", "nature.significance", None)],
        "methods": [_major("GNN models get 30 TPE evaluations while the others get 50.", "cs_ml.tuning_budget", "C1")],
        "results": [{**_major("Minor wording of Table 1 caption.", None, None), "severity": "minor"}],
    })
    added = sr._ledger_reasons(payload)
    reasons = payload["rejection_reasons"]
    assert added == 2 and [r["criterion"] for r in reasons] == ["rigor", "journal_fit"]
    assert reasons[0]["origin"] == "evidence_ledger" and reasons[0]["section"] == "methods"


def test_a_concern_the_main_analysis_raised_is_not_added_twice() -> None:
    existing = [{"problem": "GNN models get only 30 TPE evaluations while the other models get 50 evaluations.", "evidence": ""}]
    payload = _report_with({"methods": [_major("GNN models get 30 TPE evaluations while the others get 50.", "cs_ml.tuning_budget", "C1")]}, existing)
    assert sr._ledger_reasons(payload) == 0


def test_ledger_concerns_cap_the_criterion_they_bear_on() -> None:
    from services.analysis_guard import CRITERION_CAP_MAJOR, _concern_caps

    criteria = {"rigor": {"score": 90}}
    _concern_caps(criteria, [{"criterion": "rigor", "severity": "major", "origin": "evidence_ledger"}])
    assert criteria["rigor"]["score"] == CRITERION_CAP_MAJOR


# --------------------------------------------------------------------------- lookups kept per paper


def test_lookup_snapshot_reuses_the_first_answer_and_refresh_fetches_again() -> None:
    from services.lookup_snapshot import snapshot

    answers = iter([1, 2, 3])

    async def fetch() -> int:
        return next(answers)

    async def go() -> list[int]:
        same = lambda: snapshot("t", ["paper"], fetch, dump=lambda v: v, load=int)  # noqa: E731
        return [await same(), await same(), await snapshot("t", ["paper"], fetch, dump=lambda v: v, load=int, refresh=True)]

    assert asyncio.run(go()) == [1, 1, 2]


def test_lookup_snapshot_does_not_keep_what_keep_rejects() -> None:
    from services.lookup_snapshot import snapshot

    answers = iter([0, 5])

    async def fetch() -> int:
        return next(answers)

    async def go() -> list[int]:
        run = lambda: snapshot("t2", ["p"], fetch, dump=lambda v: v, load=int, keep=lambda v: v > 0)  # noqa: E731
        return [await run(), await run(), await run()]

    assert asyncio.run(go()) == [0, 5, 5]


def test_the_model_is_given_only_stable_citation_facts() -> None:
    from models import CitationSummary
    from services.pipeline import citation_summary_text

    fields = CitationSummary.model_fields
    base: dict[str, Any] = {name: 0 for name, f in fields.items() if f.annotation is int}
    base["items"] = []
    one = citation_summary_text(CitationSummary(**{**base, "total": 40, "verified": 30, "ghost": 1, "check_incomplete": 5}))
    two = citation_summary_text(CitationSummary(**{**base, "total": 40, "verified": 35, "ghost": 1, "check_incomplete": 0}))
    assert one == two and "40 references" in one and "1 ghost" in one


# --------------------------------------------------------------------------- claims that something is missing


def test_a_problem_that_says_something_is_missing_is_searched_for_not_defended() -> None:
    assert el.asserts_absence({"status": "problem", "problem": "The claim lacks a controlled experiment to isolate the cause."})
    assert el.asserts_absence({"status": "problem", "problem": "The section does not report n or the statistical test."})
    assert el.asserts_absence({"status": "not_reported", "problem": "x"})
    assert not el.asserts_absence({"status": "problem", "problem": "GNN models get 30 evaluations while others get 50."})


CAUSAL = {
    "status": "problem", "evidence": ["P6.2"],
    "problem": "The claim lacks a controlled experiment that isolates why deep models underperform non-deep counterparts.",
    "impact": "C1 rests on observation only.", "fix": "Add an experiment that varies the smoothness of the 14 datasets.",
}








def test_sentence_ids_are_taken_out_of_model_text() -> None:
    assert el.without_ids("It gives qualitative explanations (P13.3, P13.4, P20.1) but no numbers.") == (
        "It gives qualitative explanations but no numbers."
    )
    assert el.without_ids("See P12 and P14.2.") == "See."



def test_a_claim_of_absence_waits_for_the_whole_paper_audit_and_skips_the_defence() -> None:
    q = question_id("universal.claim_support")
    provider = Scripted({"results": {"answers": [{"q": q, **CAUSAL}]}},
                        defence={"items": [{"id": "D1", "verdict": "resolved", "evidence": ["P6.1"]}]})
    outcome = run(provider)
    assert "defence" not in provider.calls and len(outcome.sections["results"]["findings"]) == 1


def test_breaks_after_a_capital_are_joined_and_compound_prefixes_kept() -> None:
    vocab = vocabulary("machine learning networks transformers\nthe table shows")
    text = "Ma- chine learning; Net- works; Trans- formers; As shown in Ta- ble 2; Self- supervised; Sequence- based; SMILES- based"
    assert dehyphenate(text, vocab) == (
        "Machine learning; Networks; Transformers; As shown in Table 2; Self-supervised; Sequence-based; SMILES- based"
    )
