"""Checks on the model's output: sections, quotations, numbers, criteria and the overall score."""

from __future__ import annotations

from typing import Any

from services import paper_profile as pp
from services.analysis_guard import (
    UNVERIFIED_QUOTE,
    AnalysisContext,
    mood_for_score,
    paper_context,
)
from config import settings
from services.llm.base import MESSAGE_OVERHEAD_CHARS, AbstractAnalysisProvider

PAPER = (
    "We pioneer a self-reporting profluorescent release system driven by the thermo-reversible dynamic "
    "covalent ligation of chromophores to a lateral polymer chain. The design and synthesis of self-reporting "
    "delivery systems has attracted intense research interest. The polymer was obtained with a molar mass of "
    "8700 g mol and a dispersity of 2.3. Heating above 80 degrees released the reporter within minutes. "
    "In conclusion the platform holds potential for imaging substrate release events (Fig. S4, ESI)."
)


class _Provider(AbstractAnalysisProvider):
    async def analyse(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError

    async def generate(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover - not used
        raise NotImplementedError


def make_ctx(**overrides: Any) -> AnalysisContext:
    values: dict[str, Any] = dict(
        source_text=PAPER,
        article_type=pp.COMMUNICATION,
        type_source="label",
        structure_mode="derived",
        body=PAPER,
        refers_to_si=True,
        citations_total=10,
        citations_verified=9,
    )
    values.update(overrides)
    return AnalysisContext(**values)


def criteria(score: int = 70) -> dict[str, Any]:
    return {k: {"score": score, "reason": "r"} for k, _ in pp.PROFILES[pp.RESEARCH_ARTICLE].criteria}


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mood": "neutral",
        "mood_score": 50,
        "verdict": "A short verdict.",
        "oracle_letter": "A letter.",
        "criteria": criteria(),
        "sections": [],
        "rejection_reasons": [],
        "novelty_assessment": "n",
        "journal_fit": "j",
    }
    base.update(overrides)
    return base


def run(raw: dict[str, Any], ctx: AnalysisContext | None) -> dict[str, Any]:
    with paper_context(ctx):
        return _Provider().normalize_analysis_payload(raw)


def derived(name: str, anchor: str, score: int = 60, role: str = "results") -> dict[str, Any]:
    return {"name": name, "role": role, "source": "derived", "anchor": anchor, "score": score, "issue": "i", "fix": "f"}


# ------------------------------------------------------------------ sections

def test_derived_sections_are_kept_in_paper_order_when_the_anchor_is_in_the_paper() -> None:
    out = run(
        payload(
            sections=[
                derived("Results and discussion", "Heating above 80 degrees released the reporter"),
                derived("Introduction", "The design and synthesis of self-reporting delivery systems", role="introduction"),
            ]
        ),
        make_ctx(),
    )
    assert list(out["sections"]) == ["Introduction", "Results and discussion"]  # sorted by where they start
    assert out["sections"]["Introduction"]["source"] == "derived"


def test_derived_section_names_come_from_the_fixed_list_not_from_the_model() -> None:
    out = run(
        payload(
            sections=[
                derived("Design and motivation", "The design and synthesis of self-reporting delivery systems", role="introduction"),
                derived("Thermal release studies", "Heating above 80 degrees released the reporter", role="results"),
                derived("Concluding remarks", "In conclusion the platform holds potential for imaging", role="discussion"),
            ]
        ),
        make_ctx(),
    )
    assert list(out["sections"]) == ["Introduction", "Results and discussion", "Conclusion"]


def test_two_derived_parts_with_the_same_name_keep_the_first_one() -> None:
    out = run(
        payload(
            sections=[
                derived("Results", "Heating above 80 degrees released the reporter"),
                derived("Discussion of results", "The polymer was obtained with a molar mass of"),
            ]
        ),
        make_ctx(),
    )
    assert list(out["sections"]) == ["Results and discussion"]
    assert out["sections"]["Results and discussion"]["anchor"].startswith("The polymer was obtained")


def test_derived_section_with_an_invented_anchor_is_removed_and_reported() -> None:
    out = run(
        payload(
            sections=[
                derived("Design", "The design and synthesis of self-reporting delivery systems", role="introduction"),
                derived("Toxicity", "Cell viability was measured by an MTT assay in HeLa cells"),
            ]
        ),
        make_ctx(),
    )
    assert list(out["sections"]) == ["Introduction"]
    assert any("Toxicity" in note for note in out["grounding_notes"])


def test_anchor_matches_across_hyphen_breaks_and_ligatures() -> None:
    ctx = make_ctx(source_text="self-reporting ﬂuorescence is silenced by unpaired spins of nitroxide moieties.")
    out = run(payload(sections=[derived("Quenching", "self reporting fluorescence is silenced by unpaired spins")]), ctx)
    assert "Results and discussion" in out["sections"]


def test_detected_sections_must_be_ones_the_parser_found() -> None:
    ctx = make_ctx(
        structure_mode="detected",
        article_type=pp.RESEARCH_ARTICLE,
        section_texts={"introduction": "x" * 100, "results": "y" * 100},
        section_roles={"introduction": "introduction", "results": "results"},
    )
    out = run(
        payload(
            sections=[
                {"name": "Introduction", "score": 70, "issue": "i", "fix": "f"},
                {"name": "Synthesis", "score": 10, "issue": "i", "fix": "f"},
            ]
        ),
        ctx,
    )
    assert list(out["sections"]) == ["introduction"]
    assert out["sections"]["introduction"]["source"] == "detected"


def test_section_without_a_score_is_dropped_and_both_shapes_are_accepted() -> None:
    old_shape = {"introduction": {"score": 55, "issue": "i", "fix": "f"}, "methods": {"issue": "no score"}}
    out = run(payload(sections=old_shape), None)
    assert list(out["sections"]) == ["introduction"]


def test_without_a_context_the_models_own_score_and_sections_pass_through() -> None:
    out = run(payload(mood_score=61, sections={"introduction": {"score": 55}}), None)
    assert out["mood_score"] == 61
    assert out["sections"]["introduction"]["score"] == 55


# ------------------------------------------------------------------ quotations and numbers

def test_a_quotation_that_is_not_in_the_paper_is_removed() -> None:
    out = run(
        payload(oracle_letter='You write "the platform cures all known cancers in mice" which is not shown.'),
        make_ctx(),
    )
    assert UNVERIFIED_QUOTE in out["oracle_letter"]
    assert "cures all known cancers" not in out["oracle_letter"]
    assert any("quotation" in note for note in out["grounding_notes"])


def test_a_real_quotation_and_a_short_term_in_quotes_are_kept() -> None:
    out = run(
        payload(
            oracle_letter='The paper says "intense research interest" and calls it "novel", as in '
            '"the design and synthesis of self-reporting delivery systems has attracted intense research interest".'
        ),
        make_ctx(),
    )
    assert UNVERIFIED_QUOTE not in out["oracle_letter"]


def test_numbers_and_years_the_paper_does_not_contain_are_flagged_not_rewritten() -> None:
    out = run(payload(verdict="Yield of 93% and a 2019 benchmark; the dispersity of 2.3 is fine."), make_ctx())
    flagged = " ".join(out["grounding_notes"])
    assert "93%" in flagged and "2019" in flagged
    assert "2.3" not in flagged
    assert "93%" in out["verdict"]  # flagged, left for the reader to check


# ------------------------------------------------------------------ article type, criteria, score

def test_the_type_from_the_file_wins_over_the_model() -> None:
    out = run(payload(paper_format="review"), make_ctx(article_type=pp.COMMUNICATION))
    assert out["paper_format"] == pp.COMMUNICATION


def test_the_models_type_is_used_only_when_the_type_is_unknown_and_valid() -> None:
    out = run(payload(paper_format="Mini-review"), make_ctx(article_type=pp.UNKNOWN, refers_to_si=False))
    assert out["paper_format"] == pp.MINI_REVIEW
    out = run(payload(paper_format="novel"), make_ctx(article_type=pp.UNKNOWN))
    assert out["paper_format"] == pp.UNKNOWN
    assert any("could not be determined" in n for n in out["not_assessed"])


def test_overall_score_is_the_weighted_mean_of_criteria_with_a_computed_citation_criterion() -> None:
    ctx = make_ctx(article_type=pp.PERSPECTIVE, refers_to_si=False, citations_total=10, citations_verified=10)
    review_criteria = {k: {"score": 80, "reason": "r"} for k, _ in pp.PROFILES[pp.REVIEW].criteria}
    out = run(payload(criteria=review_criteria, mood_score=5), ctx)
    assert out["mood_score"] == 82  # (90 * 80 + 10 * 100) / 100: five criteria at 80, citations at 100
    assert out["criteria"][pp.CITATION_CRITERION]["computed"] is True
    assert out["mood"] == "delighted"


def test_citation_criterion_penalises_ghosts_only() -> None:
    ctx = make_ctx(citations_total=10, citations_verified=6, citations_unchecked=2, citations_ghost=1)
    out = run(payload(), ctx)
    assert out["criteria"][pp.CITATION_CRITERION]["score"] == 85  # 15 points for one ghost
    many = run(payload(), make_ctx(citations_total=10, citations_verified=0, citations_ghost=6))
    assert many["criteria"][pp.CITATION_CRITERION]["score"] == 40  # the penalty is capped at 60


def test_references_the_databases_do_not_know_cost_nothing() -> None:
    ctx = make_ctx(citations_total=5, citations_verified=1, citations_unchecked=0, citations_ghost=0)
    out = run(payload(), ctx)
    assert out["criteria"][pp.CITATION_CRITERION]["score"] == 100


def test_no_citation_criterion_when_nothing_could_be_checked() -> None:
    out = run(payload(), make_ctx(citations_total=5, citations_unchecked=5))
    assert pp.CITATION_CRITERION not in out["criteria"]


def test_a_communication_that_points_to_its_si_gets_a_note_and_a_capped_penalty() -> None:
    no_si = run(payload(), make_ctx(refers_to_si=False, citations_total=0))
    with_si = run(payload(), make_ctx(refers_to_si=True, citations_total=0))
    assert no_si["mood_score"] - with_si["mood_score"] == 5
    assert any("Supporting Information" in n for n in with_si["not_assessed"])
    assert not no_si["not_assessed"]


def test_a_research_article_without_methods_loses_more_than_one_that_points_to_its_si() -> None:
    base = dict(article_type=pp.RESEARCH_ARTICLE, structure_mode="derived", citations_total=0)
    missing = run(payload(sections=[derived("Results", "The polymer was obtained with a molar mass of")]), make_ctx(refers_to_si=False, **base))
    si = run(payload(sections=[derived("Results", "The polymer was obtained with a molar mass of")]), make_ctx(refers_to_si=True, **base))
    with_methods = run(
        payload(sections=[derived("Experimental", "The polymer was obtained with a molar mass of", role="methods")]),
        make_ctx(refers_to_si=False, **base),
    )
    assert with_methods["mood_score"] - missing["mood_score"] == 8
    assert with_methods["mood_score"] - si["mood_score"] == 5


def test_reviews_are_not_penalised_for_having_no_experimental_section() -> None:
    ctx = make_ctx(article_type=pp.REVIEW, refers_to_si=True, citations_total=0)
    review_criteria = {k: {"score": 70, "reason": "r"} for k, _ in pp.PROFILES[pp.REVIEW].criteria}
    out = run(payload(criteria=review_criteria), ctx)
    assert out["mood_score"] == 70
    assert not out["not_assessed"]


def test_without_criteria_the_models_score_is_used_and_the_user_is_told() -> None:
    out = run(payload(criteria={}, mood_score=64), make_ctx(refers_to_si=False, article_type=pp.UNKNOWN, citations_total=0))
    assert out["mood_score"] == 64
    assert any("no criteria" in n for n in out["grounding_notes"])


def test_mood_follows_the_score_bands() -> None:
    assert [mood_for_score(s) for s in (0, 20, 21, 55, 56, 70, 71, 80, 81, 100)] == [
        "furious", "furious", "skeptical", "unimpressed", "neutral", "neutral",
        "interested", "interested", "delighted", "delighted",
    ]


# ------------------------------------------------------------------ the prompt

def test_prompt_lists_what_is_expected_and_what_is_not_for_the_article_type() -> None:
    with paper_context(make_ctx()):
        message = _Provider().build_user_message("abs", {}, "Polym. Chem.", "9 verified", {})
    assert "Communication" in message
    assert "NOT expected" in message and "methods" in message
    assert "derived" in message and "anchor" in message
    assert "9 verified" in message


def test_prompt_for_detected_sections_names_them_exactly() -> None:
    ctx = make_ctx(
        structure_mode="detected",
        article_type=pp.RESEARCH_ARTICLE,
        section_texts={"introduction": "x", "results": "y"},
        section_roles={"introduction": "introduction", "results": "results"},
    )
    with paper_context(ctx):
        message = _Provider().build_user_message("abs", ctx.section_texts, "J", "c", {})
    assert '"introduction", "results"' in message


def test_prompt_for_unknown_type_offers_both_criteria_sets() -> None:
    with paper_context(make_ctx(article_type=pp.UNKNOWN)):
        message = _Provider().build_user_message("abs", {}, "J", "c", {})
    assert "novelty" in message and "coverage_balance" in message


def test_a_huge_body_is_trimmed_but_the_citation_summary_and_journal_data_are_kept() -> None:
    budget = settings.ANALYSIS_BODY_BUDGET_CHARS
    huge = "word " * (budget // 5 + 20000)
    ctx = make_ctx(body=huge, source_text=huge)
    with paper_context(ctx):
        message = _Provider().build_user_message("abstract", {}, "J", "CITATION-SUMMARY-MARKER", {"x": "JOURNAL-MARKER"})
    assert len(message) <= budget + MESSAGE_OVERHEAD_CHARS
    assert "CITATION-SUMMARY-MARKER" in message and "JOURNAL-MARKER" in message
    assert "text omitted" in message
    assert "Never treat content you were not shown as missing" in message


def test_a_typical_long_paper_is_sent_whole() -> None:
    body = "The polythioester was made by ring-opening polymerization. " * 1000  # about 60k characters
    ctx = make_ctx(body=body, source_text=body)
    with paper_context(ctx):
        message = _Provider().build_user_message("abstract", {}, "J", "c", {})
    assert body.strip() in message
    assert "text omitted" not in message


def test_a_provider_with_a_small_context_cuts_the_paper_and_says_so() -> None:
    class Small(_Provider):
        body_budget_chars = 26_000

    body = "word " * 20000
    ctx = make_ctx(body=body, source_text=body)
    with paper_context(ctx):
        message = Small().build_user_message("abstract", {}, "J", "c", {})
    assert len(message) < 26_000 + MESSAGE_OVERHEAD_CHARS and "text omitted" in message


def test_without_a_context_the_message_is_still_built() -> None:
    message = _Provider().build_user_message("abs", {"introduction": "text"}, "J", "c", {})
    assert "introduction" in message and "Target journal" in message


def test_a_communication_gets_the_si_note_even_when_the_model_labels_a_synthesis_section_as_methods() -> None:
    sections = [derived("Design and synthesis", "The design and synthesis of self-reporting delivery systems", role="methods")]
    out = run(payload(sections=sections), make_ctx(refers_to_si=True, citations_total=0))
    assert any("Supporting Information" in n for n in out["not_assessed"])
    assert out["mood_score"] == 65  # criteria at 70 (all weights present), minus the 5-point cap


def test_numbers_found_only_in_the_reference_list_do_not_count_as_in_the_paper() -> None:
    ctx = make_ctx(source_text=PAPER + " references: J. Chem. 2001, 5, 97-101.", numbers_text=PAPER)
    out = run(payload(verdict="A yield of 97% was reported."), ctx)
    assert any("97%" in n for n in out["grounding_notes"])


# ------------------------------------------------------------------ rejection reasons: problem, evidence, impact, solution

REAL_EVIDENCE = "Heating above 80 degrees released the reporter within minutes"


def reason(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "problem": "The release of the reporter is only shown qualitatively, so the rate is unknown.",
        "evidence": REAL_EVIDENCE,
        "impact_area": "scientific_validity",
        "impact": "Without a measured rate the central claim of controlled release cannot be judged by a reviewer.",
        "solution": "Fit the fluorescence intensity against time at 80 degrees and report the release rate and percentage.",
        "severity": "major",
    }
    item.update(overrides)
    return item


def reasons_of(out: dict[str, Any]) -> list[dict[str, Any]]:
    return out["rejection_reasons"]


def test_a_complete_reason_is_kept_with_a_location_found_by_code() -> None:
    ctx = make_ctx(
        source_text=PAPER + " Fig. 3 displays the evolution of fluorescence.",
        structure_mode="derived",
    )
    sections = [
        derived("Design", "The design and synthesis of self-reporting delivery systems", role="introduction"),
        derived("Release", "Heating above 80 degrees released the reporter within minutes", role="results"),
    ]
    out = run(payload(sections=sections, rejection_reasons=[reason()]), ctx)
    (item,) = reasons_of(out)
    assert item["problem"].startswith("The release of the reporter")
    assert item["reason"] == item["problem"] and item["fix"] == item["solution"]  # older clients keep working
    assert item["impact_area"] == "scientific_validity"
    assert item["evidence"] == REAL_EVIDENCE
    assert "section: Results and discussion" in item["location"]  # worked out from where the evidence sits, not asked of the model


def test_the_location_names_the_figure_next_to_the_evidence() -> None:
    text = PAPER + " The trace in Fig. 3 shows the fluorescence of the reporter rising. " + "Filler words here. " * 80 + "Fig. 9 is elsewhere."
    out = run(payload(rejection_reasons=[reason(evidence="The trace in Fig. 3 shows the fluorescence of the reporter rising")]), make_ctx(source_text=text, numbers_text=text))
    assert "next to Fig. 3" in reasons_of(out)[0]["location"]


def test_a_reason_missing_any_of_the_four_parts_is_removed() -> None:
    out = run(
        payload(
            rejection_reasons=[
                reason(problem=""),
                reason(impact=""),
                reason(solution="Fix it."),
                reason(evidence=""),
                reason(evidence="Cell viability was measured by an MTT assay in HeLa cells"),  # not in the paper
            ]
        ),
        make_ctx(),
    )
    assert reasons_of(out) == []
    notes = " ".join(out["grounding_notes"])
    assert "did not point to any text" in notes and "lacked a problem, an impact or a solution" in notes


def test_a_generic_solution_that_shares_nothing_with_the_problem_and_evidence_is_removed() -> None:
    generic = reason(
        problem="The discussion does not connect the results to the wider implications of the platform.",
        solution="Strengthen the discussion by clearly articulating the broader significance and potential applications.",
    )
    specific = reason()
    out = run(payload(rejection_reasons=[generic, specific]), make_ctx())
    assert [r["problem"] for r in reasons_of(out)] == [specific["problem"]]
    assert any("not specific to the cited passage" in n for n in out["grounding_notes"])


def test_impact_area_accepts_common_spellings_and_drops_unknown_values() -> None:
    out = run(
        payload(rejection_reasons=[reason(impact_area="Scientific validity"), reason(impact_area="vibes")]),
        make_ctx(),
    )
    assert [r["impact_area"] for r in reasons_of(out)] == ["scientific_validity", ""]


def test_evidence_matches_across_ligatures_and_hyphen_breaks() -> None:
    ctx = make_ctx(source_text="the intro- duced technology platform holds signiﬁcant potential for imaging substrate release")
    out = run(
        payload(
            rejection_reasons=[
                reason(
                    problem="The claim of significant potential for imaging is not backed by an imaging experiment.",
                    evidence="introduced technology platform holds significant potential for imaging",
                    solution="Add a cell or tissue imaging experiment that shows the release being read out by fluorescence.",
                )
            ]
        ),
        ctx,
    )
    assert len(reasons_of(out)) == 1


def test_no_more_than_six_reasons_are_kept() -> None:
    out = run(payload(rejection_reasons=[reason() for _ in range(9)]), make_ctx())
    assert len(reasons_of(out)) == 6


def test_when_every_reason_is_removed_the_user_is_told_why() -> None:
    out = run(payload(rejection_reasons=[reason(evidence="")]), make_ctx())
    assert reasons_of(out) == []
    assert any("rejection reason" in n for n in out["grounding_notes"])


def test_without_a_context_reasons_pass_through_untouched() -> None:
    out = run(payload(rejection_reasons=[{"reason": "Vague.", "severity": "major", "fix": "Do more."}]), None)
    assert len(reasons_of(out)) == 1


def test_the_prompt_demands_all_four_parts_without_a_fixed_example() -> None:
    for prompt in (_Provider.SYSTEM_PROMPT_A, _Provider.SYSTEM_PROMPT_B):
        for part in ('"problem"', '"evidence"', '"impact"', '"solution"', '"impact_area"'):
            assert part in prompt
        assert "pyrene" not in prompt.lower()  # nothing from one field is baked into the prompt


# ------------------------------------------------------------------ section content: where a section starts and ends

ABSTRACT_TEXT = "We pioneer a self-reporting release system driven by dynamic covalent ligation of chromophores."
INTRO = "The design and synthesis of self-reporting delivery systems has attracted intense research interest. More background follows here."
SYNTH = "The polymer was obtained by radical copolymerisation with a molar mass of 8700 g mol and a dispersity of 2.3."
RELEASE = "Heating above 80 degrees released the reporter within minutes and the fluorescence switched on."
CONCL = "In conclusion the platform holds potential for imaging substrate release events."
BODY = "\n\n".join(["A title line and the authors", ABSTRACT_TEXT, INTRO, SYNTH, RELEASE, CONCL])


def content_ctx(**overrides: Any) -> AnalysisContext:
    values: dict[str, Any] = dict(source_text=BODY, body=BODY, abstract=ABSTRACT_TEXT, structure_mode="derived", article_type=pp.COMMUNICATION, citations_total=0)
    values.update(overrides)
    return AnalysisContext(**values)


def test_a_derived_section_runs_from_its_anchor_to_where_the_next_one_starts() -> None:
    sections = [
        derived("Background", "The design and synthesis of self-reporting delivery systems"),
        derived("Synthesis", "The polymer was obtained by radical copolymerisation", role="methods"),
        derived("Release", "Heating above 80 degrees released the reporter within minutes"),
    ]
    out = run(payload(sections=sections), content_ctx())
    background, synthesis, release = (
        out["sections"][n] for n in ("Introduction", "Experimental", "Results and discussion")
    )
    assert background["content"] == INTRO
    assert synthesis["content"] == SYNTH
    assert release["content"] == RELEASE + "\n\n" + CONCL  # the last section runs to the end of the body
    assert background["word_count"] == len(INTRO.split())
    assert background["start_pct"] < synthesis["start_pct"] < release["start_pct"]
    assert background["end_pct"] == synthesis["start_pct"] and release["end_pct"] == 100


def test_the_text_before_the_first_section_title_and_abstract_is_not_part_of_a_section() -> None:
    out = run(payload(sections=[derived("Background", "The design and synthesis of self-reporting delivery systems")]), content_ctx())
    assert "title line" not in out["sections"]["Introduction"]["content"]
    assert ABSTRACT_TEXT not in out["sections"]["Introduction"]["content"]


def test_an_anchor_that_is_also_in_the_abstract_is_taken_from_the_text_after_it() -> None:
    body = "\n\n".join(["Title", ABSTRACT_TEXT, "Intro: " + ABSTRACT_TEXT + " More text of the introduction follows."])
    out = run(payload(sections=[derived("Intro", "We pioneer a self-reporting release system driven by dynamic covalent")]), content_ctx(source_text=body, body=body))
    assert out["sections"]["Introduction"]["content"].startswith("We pioneer")
    assert out["sections"]["Introduction"]["start_pct"] > 20  # not the abstract at the top


def test_two_sections_with_the_same_start_do_not_both_get_text() -> None:
    same = "The polymer was obtained by radical copolymerisation"
    out = run(payload(sections=[derived("One", same), derived("Two", same)]), content_ctx())
    assert [("content" in s and s["content"] is not None) for s in out["sections"].values()].count(True) <= 1


def test_very_long_content_is_shortened_and_flagged_but_the_word_count_is_the_real_one() -> None:
    long_text = "word " * 5000
    body = "\n\n".join(["Title", ABSTRACT_TEXT, INTRO + " " + long_text])
    out = run(payload(sections=[derived("Background", "The design and synthesis of self-reporting delivery systems")]), content_ctx(source_text=body, body=body))
    section = out["sections"]["Introduction"]
    assert section["content_truncated"] is True and len(section["content"]) <= 12002
    assert section["word_count"] > 5000


def test_a_section_whose_anchor_is_not_in_the_body_keeps_its_score_but_has_no_content() -> None:
    ctx = content_ctx(body="Only a short body without the anchor sentence in it at all.")
    out = run(payload(sections=[derived("Background", "The design and synthesis of self-reporting delivery systems")]), ctx)
    assert out["sections"]["Introduction"]["score"] == 60
    assert out["sections"]["Introduction"].get("content") is None


def test_detected_sections_carry_the_text_the_parser_found_under_the_heading() -> None:
    ctx = content_ctx(
        structure_mode="detected",
        article_type=pp.RESEARCH_ARTICLE,
        section_texts={"introduction": INTRO, "results": SYNTH + "\n\n" + RELEASE},
        section_roles={"introduction": "introduction", "results": "results"},
    )
    out = run(payload(sections=[{"name": "introduction", "score": 70, "issue": "i", "fix": "f"}, {"name": "results", "score": 60, "issue": "i", "fix": "f"}]), ctx)
    assert out["sections"]["introduction"]["content"] == INTRO
    assert out["sections"]["results"]["content"] == SYNTH + "\n\n" + RELEASE
    assert out["sections"]["introduction"]["start_pct"] < out["sections"]["results"]["start_pct"]


def test_without_a_body_there_is_no_content() -> None:
    out = run(payload(sections=[derived("Background", "The design and synthesis of self-reporting delivery systems")]), content_ctx(body=""))
    assert out["sections"]["Introduction"].get("content") is None
