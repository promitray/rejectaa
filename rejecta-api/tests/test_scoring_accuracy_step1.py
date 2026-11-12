"""Step 1 of the scoring-accuracy work: what the parser reads, which journal is matched, how editorials,
unknown references and long papers are scored, and how a cut-off model answer is handled."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from services import journals
from services import paper_profile as pp
from services import parser
from services.citation_refs import candidate_from_crossref
from services.llm import base
from services.llm.openai import OpenAIProvider
from tests.test_analysis_guard import make_ctx, payload, run

# ------------------------------------------------------------------ parser


def _numbered_refs(first: int, count: int) -> list[str]:
    return [f"{n} A. Author{n} and B. Writer, J. Chem. Res., 20{10 + n % 10}, {n}, {100 + n}-{110 + n}." for n in range(first, first + count)]


NATURE_LAYOUT = "\n".join(
    [
        "Introduction",
        *["The catalyst converts carbon dioxide at room temperature with high selectivity."] * 20,
        "Results",
        *["Turnover reached 4000 per hour in water and the product was isolated in 91 percent yield."] * 20,
        "References",
        *_numbered_refs(1, 30),
        "Methods",
        *["Reactions were run in a sealed vessel under argon and analysed by gas chromatography."] * 20,
        "References",
        *_numbered_refs(31, 10),
        "Acknowledgements",
        "We thank the funders.",
    ]
)


def test_methods_after_the_main_reference_list_stay_in_the_body_and_both_lists_are_read() -> None:
    parsed = parser._build_document(NATURE_LAYOUT, one_reference_per_line=False)
    assert len(parsed.references) == 40
    assert parsed.references[0].startswith("A. Author1 ")
    assert parsed.references[-1].startswith("A. Author40 ")
    assert "methods" in parsed.section_roles.values()
    assert "sealed vessel under argon" in parsed.body
    assert "A. Author5 and" not in parsed.body
    assert not parsed.parse_warnings


def test_a_page_range_alone_on_a_line_is_not_a_running_header() -> None:
    assert parser._running_key("1770–1782.") is None
    assert parser._running_key("Polym. Chem., 2017, 8, 6199 | 6200") is not None


def test_a_reference_whose_title_starts_with_a_number_is_its_own_entry() -> None:
    text = "\n".join(
        [
            "References",
            "1 A Century, Indian Association for the Cultivation of Science, Calcutta, India, 1976.",
            "2 A. K. Biswas, Gleanings of the Past, Asiatic Society, Kolkata, India, 2000, p. 441.",
            "3 125 Years IACS: Indian Association for the Cultivation of Science, 2001.",
            "4 Collected Works of Mahendralal Sircar, ed. A. K. Biswas, Asiatic Society, Kolkata, 2003, p. 620.",
        ]
    )
    parsed = parser._build_document(text, one_reference_per_line=False)
    assert len(parsed.references) == 4
    assert parsed.references[2].startswith("125 Years IACS")


def test_sections_are_no_longer_cut_at_fourteen_thousand_characters() -> None:
    long_intro = "\n".join(["Sulfur-containing polymers combine degradability with a high refractive index."] * 400)
    parsed = parser._build_document(f"Introduction\n{long_intro}\nConclusions\nIt works well overall.", True)
    assert len(parsed.sections["introduction"]) > 20_000


def test_a_reference_list_that_could_only_partly_be_read_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(parser, "MAX_REFERENCES", 10)
    parsed = parser._build_document("Introduction\nText.\nReferences\n" + "\n".join(_numbered_refs(1, 30)), False)
    assert len(parsed.references) == 10
    assert any("Only 10 of the 30" in w for w in parsed.parse_warnings)


def test_the_editorial_banner_at_the_end_of_the_page_text_names_the_type() -> None:
    first_page = ["Cite this: Chem. Commun., 2026, 62, 22107"] + ["Body text line."] * 120 + ["ChemComm", "EDITORIAL"]
    assert pp.detect_article_type(first_page) == pp.EDITORIAL
    assert pp.coerce_article_type("editorial") == pp.EDITORIAL


# ------------------------------------------------------------------ scoring


def editorial_criteria(score: int = 80) -> dict[str, Any]:
    return {k: {"score": score, "reason": "r"} for k, _ in pp.PROFILES[pp.EDITORIAL].criteria}


def test_an_editorial_is_scored_on_its_own_criteria_and_never_desk_rejected() -> None:
    reason = {
        "problem": "The piece reports no new experimental data or research findings at all.",
        "evidence": "covalent ligation of chromophores to a lateral polymer chain",
        "impact_area": "novelty",
        "impact": "An editor looking for novel chemistry would not send it to review at all.",
        "solution": "Add new chromophores ligation data to the lateral polymer chain section.",
        "severity": "desk_reject",
    }
    ctx = make_ctx(article_type=pp.EDITORIAL, refers_to_si=False, citations_total=0)
    out = run(payload(criteria=editorial_criteria(80), rejection_reasons=[reason]), ctx)
    assert set(out["criteria"]) == {k for k, _ in pp.PROFILES[pp.EDITORIAL].criteria}
    assert out["mood_score"] == 80
    assert [r["severity"] for r in out["rejection_reasons"]] == ["minor"]
    assert any("not a research paper" in n for n in out["not_assessed"])


def test_parse_warnings_reach_the_report() -> None:
    ctx = make_ctx(parse_warnings=["Only 60 of the 88 numbered references could be read from the file."])
    out = run(payload(), ctx)
    assert any("Only 60 of the 88" in n for n in out["not_assessed"])


def test_the_prompt_has_no_reason_quota_and_a_score_scale() -> None:
    prompt = base._build_system_prompt(base.MODE_A)
    assert "2-4 reasons" not in prompt and "brutally" not in prompt
    assert "75-89" in prompt and "Never make unverified references a rejection reason" in prompt
    assert "editorial" in prompt


# ------------------------------------------------------------------ citations


def test_an_article_number_counts_as_the_first_page() -> None:
    item = {"author": [{"family": "Mutlu"}], "volume": "223", "article-number": "2200111", "issued": {"date-parts": [[2022]]}}
    assert candidate_from_crossref(item).first_page == "2200111"


def test_a_two_letter_surname_matches_exactly_but_not_as_part_of_a_longer_name() -> None:
    from services.citation_refs import compare, parse_reference

    ref = parse_reference("Y. Hu, Z. Yin, T. Werner, A. Spannenberg and X.-F. Wu, Eur. J. Org. Chem., 2018, 2018, 1274.")
    record = {
        "author": [{"family": n} for n in ("Hu", "Yin", "Werner", "Spannenberg", "Wu")],
        "volume": "2018", "page": "1274-1276", "issued": {"date-parts": [[2018, 3, 14]]},
        "container-title": ["European Journal of Organic Chemistry"],
    }
    report = compare(ref, candidate_from_crossref(record))
    assert report.author and report.year and report.volume and report.page
    other = {**record, "author": [{"family": "Zhu"}, {"family": "Shu"}]}
    assert not compare(ref, candidate_from_crossref(other)).author


# ------------------------------------------------------------------ journals


def source(name: str, works: int, alternates: tuple[str, ...] = (), abbreviation: str | None = None) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/S{works}", "display_name": name, "works_count": works, "type": "journal",
        "alternate_titles": list(alternates), "abbreviated_title": abbreviation,
    }


def test_the_journal_named_exactly_wins_over_one_that_has_the_name_as_an_alternate_title() -> None:
    jps = source("Journal of Polymer Science Part A Polymer Chemistry", 16565, ("Polymer Chemistry",))
    rsc = source("Polymer Chemistry", 12345)
    picked, exact = journals.pick_source("Polymer Chemistry", [jps, rsc])
    assert picked is rsc and exact


def test_an_abbreviated_name_matches_the_full_name() -> None:
    picked, exact = journals.pick_source("Chem. Commun.", [source("Chemical Communications", 78893)])
    assert exact and picked["display_name"] == "Chemical Communications"
    assert journals.pick_source("J. Am. Chem. Soc.", [source("Journal of the American Chemical Society", 9)])[1]
    assert journals.looks_abbreviated("Angew. Chem. Int. Ed.")
    assert "angewandte international edition" in journals.expansions("Angew. Chem. Int. Ed.")


def test_a_name_that_matches_no_journal_exactly_is_reported_as_approximate() -> None:
    picked, exact = journals.pick_source(
        "chemistry communication", [source("Structural Chemistry & Crystallography Communication", 121)]
    )
    assert picked is not None and not exact


def test_similar_paper_keywords_skip_common_words() -> None:
    words = journals.abstract_keywords("This review summarizes polythioesters. Polythioesters are sulfur polymers made by polymerization.")
    assert words[0] == "polythioesters" and "review" not in words[:1]


# ------------------------------------------------------------------ model output


class _FakeCompletions:
    def __init__(self, answers: list[tuple[str, str]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content, finish = self.answers.pop(0)
        choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)
        return SimpleNamespace(choices=[choice], model="gpt-4o-2024-08-06")


def test_a_report_cut_off_at_the_token_limit_is_asked_for_again_shorter() -> None:
    import json

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.model = "gpt-4o"
    good = json.dumps(payload())
    fake = _FakeCompletions([('{"verdict": "cut', "length"), (good, "stop")])
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    result = asyncio.run(provider.analyse("abs", {}, "J", "c", {}))
    assert len(fake.calls) == 2
    assert base.COMPACT_RETRY_NOTE in fake.calls[1]["messages"][-1]["content"]
    assert result.provider_used == "openai/gpt-4o-2024-08-06"


# ------------------------------------------------------------------ Fix with Calibrator


def test_a_rejection_reason_carries_its_section_and_the_paragraphs_around_its_evidence() -> None:
    from tests.test_analysis_guard import derived

    filler = " ".join(f"Sentence {i} describes the screening of catalysts and solvents in detail." for i in range(12))
    body = "\n\n".join(
        [
            "Introduction paragraph about sulfur polymers and their uses in optics and recycling today.",
            *[f"Methods paragraph {n}. {filler}" for n in range(12)],
            "The polymerization followed a traditional step-growth mechanism which was favored over depolymerization.",
            "A final paragraph of the same section closes the discussion of the mechanism here.",
        ]
    )
    ctx = make_ctx(body=body, source_text=body, structure_mode="derived", citations_total=0, refers_to_si=False)
    reason = {
        "problem": "The step-growth mechanism claim is not compared with ring-opening routes at all.",
        "evidence": "The polymerization followed a traditional step-growth mechanism which was favored over depolymerization.",
        "impact_area": "publication_potential",
        "impact": "Reviewers cannot judge what the step-growth mechanism adds over existing routes.",
        "solution": "Compare the step-growth mechanism and depolymerization behaviour with ring-opening routes.",
        "severity": "major",
    }
    sections = [
        derived("Introduction", "Introduction paragraph about sulfur polymers and their uses", role="introduction"),
        derived("Methods and mechanism", "Methods paragraph 0. Sentence 0 describes the screening", role="methods"),
    ]
    out = run(payload(sections=sections, rejection_reasons=[reason]), ctx)
    kept = out["rejection_reasons"][0]
    assert kept["section"] == "Experimental"  # the fixed name the model's "Methods and mechanism" is filed under
    assert kept["tool_text"].count("step-growth mechanism which was favored") == 1
    assert "A final paragraph of the same section" in kept["tool_text"]
    assert "Methods paragraph 0." not in kept["tool_text"]  # a long section is cut around the evidence, not at its start
    assert kept["tool_notes"]


# ------------------------------------------------------------------ Roman-numbered layouts (IEEE style)

ROMAN_PAPER = "\n".join(
    [
        "Smart Hospital Queue System",
        "ABSTRACT",
        "A system that shortens outpatient waiting through live token scheduling and workload-aware routing of patients.",
        "I. INTRODUCTION",
        *["Long queues are one of the biggest problems in hospitals and patients wait for hours."] * 6,
        "II.LITERATURE REVIEW",
        *["Appointment systems rarely handle no-shows or predict waiting times from live delays."] * 6,
        "III. METHODOLOGY",
        *["The scheduling engine uses rule-based prioritization and a model that predicts consultation time."] * 6,
        "IV. IDENTIFIED GAPS IN EXISTING",
        "RESEARCH",
        *["Few systems combine disease routing with workload balancing across the available doctors."] * 6,
        "V. PROPOSED SYSTEM",
        *["The objectives are shorter waits, balanced doctor workload and a transparent live queue status."] * 6,
        "VII. LIMITATIONS",
        *["Integration with electronic records needs coordination and prediction needs historical data."] * 6,
        "VIII. CONCLUSION",
        *["We propose a conceptual design for a district hospital queue management system."] * 6,
        "IX.REFERENCES",
        "[1] S. Gupta, A. Jain and R. Sharma, “Smart hospital management,” IJARSCT, 2023.",
        "[2]",
        "A. Verma et al., “Smart healthcare queue management,” IEEE Xplore, 2024.",
        "[3] L. V. Green and S. Savin, “Reducing delays for medical appointments,” Operations Research, vol. 56, 2008.",
        "[4] T. Cayirli and E. Veral, “Outpatient scheduling in health care,” Prod. Oper. Manag., vol. 12, 2003.",
    ]
)


def test_roman_numbered_headings_give_sections_and_a_reference_list() -> None:
    parsed = parser._build_document(ROMAN_PAPER, one_reference_per_line=False)
    assert len(parsed.references) == 4  # "IX.REFERENCES" is a heading; "[2]" alone on its line belongs to what follows
    assert parsed.references[1].startswith("A. Verma")
    assert parsed.structure == "detected"
    assert set(parsed.sections) == {
        "introduction", "literature review", "methods", "Identified gaps in existing research",
        "Proposed system", "limitations", "discussion",
    }
    assert "RESEARCH" not in parsed.sections["Proposed system"] and "RESEARCH" not in parsed.sections["methods"]
    assert "Few systems combine disease routing" in parsed.sections["Identified gaps in existing research"]
    assert "A system that shortens" not in parsed.sections["introduction"]


def test_roman_numerals_are_read_correctly() -> None:
    assert [parser._roman_value(n) for n in ("I", "IV", "VI", "VIII", "IX", "XI")] == [1, 4, 6, 8, 9, 11]
