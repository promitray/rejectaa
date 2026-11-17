"""The verified outline (main text / appendix, pages, figures, venue), the prose checks that keep the report inside
it, the journal policy, and the editor's letter written from verified facts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from models import AnalysisResult
from services import editor_letter, outline
from services import paper_profile as pp
from services.analysis_guard import AnalysisContext, _journal_weights, score_report
from services.journal_policy import SELECTIVE, SOUNDNESS, STANDARD, policy_for
from services.parser import _extract_sections_detailed, _FILE_LAYOUT_KEYS, _layout_key

INTRO = "Molecular property prediction matters. " * 30
RESULTS = "Deep models underperform non-deep models on 10 of 14 datasets in Table 1. " * 20
WHY = "Molecular data are irregular, so the target functions are not smooth. " * 20
SETUP = "We tune hyperparameters with TPE in 50 evaluations and split data 8:1:1. " * 20
DISC = "Tree models remain strong baselines and deep models need better inductive biases. " * 10


def lines_of_paper() -> list[str]:
    return [
        "Abstract", "A short abstract of the paper that says what it does.",
        "1. Introduction", INTRO,
        "2. Benchmarking Results.", RESULTS,
        "3. Why above phenomena would occur?", WHY,
        "References", "[1] A. Author. A paper. 2020.",
        "A. The performance of various models on the orthogonally transformed dataset", RESULTS,
        "B. Experimental Setups", SETUP,
        "D. Discussion and Conclusion", DISC,
    ]


def test_numbered_and_lettered_headings_marked_by_the_layout_become_sections() -> None:
    lines = lines_of_paper()
    keys = frozenset(_layout_key(line) for line in lines[2::2] if len(line) < 120)
    token = _FILE_LAYOUT_KEYS.set(keys)
    try:
        sections, roles, _ = _extract_sections_detailed(lines, "research_article")
    finally:
        _FILE_LAYOUT_KEYS.reset(token)
    assert "Why above phenomena would occur?" in sections
    assert "The performance of various models on the orthogonally transformed dataset" in sections
    assert roles["methods"] == "methods" and "Tree models" in sections["discussion"]


def test_without_layout_marks_an_unknown_numbered_line_is_not_a_heading() -> None:
    sections, _, _ = _extract_sections_detailed(lines_of_paper(), "research_article")
    assert "Why above phenomena would occur?" not in sections  # glued to Results as before


class Parsed:
    def __init__(self) -> None:
        self.sections = {"introduction": INTRO, "results": RESULTS, "Why above phenomena would occur?": WHY,
                         "methods": SETUP, "discussion": DISC}
        self.section_titles = {"introduction": "Introduction", "results": "Benchmarking Results",
                               "methods": "Experimental Setups", "discussion": "Discussion and Conclusion"}
        self.full_text = "\n".join(lines_of_paper())


PAGES = [
    "Title\nAbstract\nThe ICML 2023 Workshop on Interpretable Machine Learning in Healthcare (IMLH), Honolulu, Hawaii, USA. "
    "PMLR 202, 2023.\n1. Introduction\nMolecular property prediction matters.",
    "2. Benchmarking Results.\nTable 1: The comparison of representative models.\n3. Why above phenomena would occur?",
    "References\n[1] A. Author.",
    "B. Experimental Setups\nFigure 4. The performance on transformed data.\nD. Discussion and Conclusion",
]


def outlined() -> Parsed:
    parsed = Parsed()
    outline.attach(parsed, lines_of_paper(), PAGES, 1, PAGES[0])
    return parsed


def test_outline_tells_main_text_from_appendix_with_pages() -> None:
    p = outlined()
    assert p.section_labels == {"introduction": "1", "results": "2", "Why above phenomena would occur?": "3", "methods": "B", "discussion": "D"}
    assert p.section_parts["results"] == "main" and p.section_parts["methods"] == "appendix" and p.section_parts["discussion"] == "appendix"
    assert p.section_pages == {"introduction": 1, "results": 2, "Why above phenomena would occur?": 2, "methods": 4, "discussion": 4}
    assert [f["label"] for f in p.figures] == ["Table 1", "Figure 4"]
    assert p.venue_statements[0].startswith("The ICML 2023 Workshop on Interpretable Machine Learning")


def ctx(article_type: str = pp.RESEARCH_ARTICLE, journal: str = "Scientific Reports") -> AnalysisContext:
    p = outlined()
    roles = {"introduction": "introduction", "results": "results", "Why above phenomena would occur?": "thematic",
             "methods": "methods", "discussion": "discussion"}
    return AnalysisContext(
        source_text=p.full_text, body=p.full_text, structure_mode="detected", section_texts=p.sections,
        section_roles=roles, section_titles=p.section_titles, article_type=article_type, abstract="",
        section_labels=p.section_labels, section_parts=p.section_parts, section_pages=p.section_pages,
        figures=p.figures, venue_statements=p.venue_statements, journal=journal,
        numbers_text=p.full_text,
    )


def test_structure_notes_say_what_the_main_text_lacks() -> None:
    notes = outline.structure_notes(ctx())
    assert any("no Methods / Experimental section; it is in Appendix B: Experimental Setups (p. 4)" in n for n in notes)
    assert any("no Discussion / Conclusion section; it is in Appendix D" in n for n in notes)
    assert outline.structure_notes(ctx(pp.REVIEW)) == []


def test_prose_naming_a_missing_section_or_figure_is_removed_and_appendix_is_marked() -> None:
    text = (
        "The 'Benchmarking Results' section is clear. The 'Limitations' section is too short. "
        "Figure 9 is unreadable. The 'Discussion and Conclusion' section could say more about future work."
    )
    checked, removed = outline.check_prose(text, ctx())
    assert "Limitations" not in checked and "Figure 9" not in checked and len(removed) == 2
    assert "'Discussion and Conclusion' section (Appendix D)" in checked
    assert checked.startswith("The 'Benchmarking Results' section is clear.")


def test_role_words_are_fine_when_the_paper_has_that_role_and_section_numbers_are_checked() -> None:
    checked, removed = outline.check_prose("The Methods section names TPE. Section 3 explains it. Section 7 is long.", ctx())
    assert removed and "Section 7" in removed[0] and "Section 3 explains it." in checked


# --------------------------------------------------------------------------- journal policy


def test_journal_tiers() -> None:
    assert policy_for("Scientific Reports").tier == SOUNDNESS
    assert policy_for("PLOS ONE").tier == SOUNDNESS
    assert policy_for("Nature Machine Intelligence").tier == SELECTIVE
    assert policy_for("Journal of Chemical Information and Modeling").tier == STANDARD


def test_soundness_journal_weights_novelty_less_and_reruns_do_not_compound() -> None:
    criteria = {"novelty": {"score": 30, "weight": 25.0, "adjustments": []}, "rigor": {"score": 85, "weight": 25.0, "adjustments": []}}
    c = ctx(journal="Scientific Reports")
    _journal_weights(criteria, c)
    first = (criteria["novelty"]["weight"], criteria["rigor"]["weight"])
    for item in criteria.values():
        item["adjustments"] = []
    _journal_weights(criteria, c)
    assert first == (criteria["novelty"]["weight"], criteria["rigor"]["weight"])
    assert criteria["novelty"]["weight"] < 25.0 < criteria["rigor"]["weight"]
    out = {"criteria": criteria, "score_penalty": 0}
    score_report(out, c)
    selective = {"criteria": {k: {"score": v["score"], "weight": 25.0} for k, v in criteria.items()}, "score_penalty": 0}
    score_report(selective, ctx(journal="Nature Machine Intelligence"))
    assert out["mood_score"] > selective["mood_score"]


# --------------------------------------------------------------------------- the editor's letter


def analysis(score: int = 72, reasons: list[dict[str, Any]] | None = None) -> AnalysisResult:
    return AnalysisResult.model_validate({
        "mood": "neutral", "mood_score": score, "verdict": "old verdict", "oracle_letter": "old letter",
        "sections": {
            "methods": {"score": 70, "role": "methods", "strengths": [{"point": "Tuning is stated", "quote": SETUP[:70]}],
                        "findings": []},
        },
        "rejection_reasons": reasons or [], "novelty_assessment": "n", "journal_fit": "old fit", "provider_used": "t",
    })


MAJOR = {"severity": "major", "problem": "GNN models get 30 TPE evaluations while the others get 50.", "evidence": SETUP[:60],
         "section": "methods", "origin": "evidence_ledger", "reason": "x"}


class Writer:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def generate(self, system: str, prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.answer)

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        return json.loads(raw)


def test_decision_is_made_by_code() -> None:
    assert editor_letter.decision(analysis(80)) == editor_letter.REVIEW
    assert editor_letter.decision(analysis(80, [MAJOR, MAJOR])) == editor_letter.REVISE
    assert editor_letter.decision(analysis(60)) == editor_letter.REVISE
    assert editor_letter.decision(analysis(35)) == editor_letter.DESK_REJECT


def test_facts_carry_appendix_location_venue_and_journal_policy() -> None:
    given = editor_letter.facts(analysis(70, [MAJOR]), ctx(), {})
    assert given["F1"]["where"].startswith("Appendix B: Experimental Setups (p. 4)")
    assert any(v["kind"].startswith("publication context") for v in given.values())
    assert any(v.get("kind") == "how the journal decides" and "Scientific Reports" in v["text"] for v in given.values())
    assert any(v["kind"] == "observation" for v in given.values())


def test_letter_keeps_only_verified_sentences() -> None:
    answer = {
        "verdict": {"text": "I would send this to review: the benchmark is broad.", "refs": ["S1"]},
        "letter": [
            {"text": "The setup in Appendix B states the tuning with TPE in 50 evaluations.", "refs": ["S1"]},
            {"text": "However, GNN models get 30 TPE evaluations while the others get 50.", "refs": ["F1"]},
            {"text": "The 'Limitations' section is missing detail.", "refs": ["F1"]},  # no such section
            {"text": "The discussion lacks depth.", "refs": ["S1"]},  # criticism citing no concern
            {"text": "It reaches 97% accuracy.", "refs": ["F1"]},  # number not in the paper
            {"text": "Nice work overall.", "refs": []},  # cites nothing
        ],
        "journal_fit": [{"text": "Scientific Reports judges soundness, not novelty.", "refs": ["J1"]}],
    }
    out = asyncio.run(editor_letter.rewrite(analysis(72, [MAJOR]), ctx(), {}, Writer(answer)))
    assert out.oracle_letter == (
        "The setup in Appendix B states the tuning with TPE in 50 evaluations. "
        "However, GNN models get 30 TPE evaluations while the others get 50."
    )
    assert out.journal_fit == "Scientific Reports judges soundness, not novelty."
    assert out.verdict.startswith("I would send this to review")
    assert any("4 sentence(s) were left out" in n for n in out.grounding_notes)
    assert out.letter_points[1]["facts"]["F1"]["problem"].startswith("GNN models")


def test_a_verdict_that_contradicts_the_decision_is_replaced() -> None:
    answer = {"verdict": {"text": "I would send this to review.", "refs": ["F1"]},
              "letter": [{"text": "x", "refs": []}], "journal_fit": []}
    out = asyncio.run(editor_letter.rewrite(analysis(55, [MAJOR, MAJOR]), ctx(), {}, Writer(answer)))
    assert out.verdict.startswith("I would return this for major revision before sending it to review")
    assert out.oracle_letter == "old letter"  # too little verified: the first letter stays


def test_retracted_papers_keep_the_code_verdict() -> None:
    from models import PublicationStatus

    c = ctx()
    c.publication = PublicationStatus(status="retracted")
    writer = Writer({})
    out = asyncio.run(editor_letter.rewrite(analysis(5), c, {}, writer))
    assert out.verdict == "old verdict" and writer.prompts == []


def test_significance_check_is_not_asked_at_a_soundness_journal() -> None:
    from services import rubrics

    claims = [rubrics.Claim("C1", "Deep models fail on molecular data", "comparative", True, ("P1.1",))]
    roles = {"introduction": "introduction", "results": "results"}

    def checks(journal: str) -> set[str]:
        plan, _ = rubrics.build_plan(claims, "cs_ml", "benchmark", journal, roles, list(roles), "", set(), policy_for(journal).tier)
        return {q.item.id for q in plan}

    assert "nature.significance" in checks("Nature Machine Intelligence")
    assert "nature.significance" not in checks("Scientific Reports")


# --------------------------------------------------------------------------- subsections


def test_subsections_are_read_in_sequence_under_a_numbered_section() -> None:
    lines = [
        "2. Benchmarking Results.", "text", "2.1. Observations", "more", "1.5 M NaOH was added to the mixture",
        "2.3. Skipped number is not a heading", "C. Related Work", "C.1. Fingerprints-based Molecular Descriptors",
        "C.2. Linear Notation-based Descriptors", "C.3. 2D and 3D Graph-based Molecular Descriptors",
    ]
    subs = outline.subsections(lines, {"results": "2", "related work": "C"}, [], 1)
    assert [(s["label"], s["parent"]) for s in subs] == [
        ("2.1", "results"), ("C.1", "related work"), ("C.2", "related work"), ("C.3", "related work"),
    ]


def test_section_and_appendix_numbers_must_exist_exactly() -> None:
    c = ctx()
    c.subsections = [{"label": "2.1", "title": "Observations", "parent": "results"}]
    checked, removed = outline.check_prose(
        "Section 2.1 lists the observations. Section 2.4 is missing. Appendix B gives the setup. Appendix F is empty. "
        "The 'Observations' section is short.",
        c,
    )
    assert "Section 2.1 lists" in checked and "Appendix B gives" in checked and "'Observations' section" in checked
    assert len(removed) == 2 and "Section 2.4" in removed[0] and "Appendix F" in removed[1]
