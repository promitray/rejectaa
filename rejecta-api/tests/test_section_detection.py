"""Section detection that does not depend on a fixed list of titles: marks, layout, styles, and stability."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import fitz
from docx import Document

from services import paper_profile as pp
from services import parser
from services.parser import _heading_parts, _heading_title, parse_document, parse_docx

SENTENCE = (
    "The analysis of the retraction records shows how often each reason occurs among the chemistry manuscripts "
    "that were withdrawn during the period under study"
)
HEADINGS = (
    "■ WHY ARE MANUSCRIPTS RETRACTED?",
    "■ WHAT DOES THE TIMELINE REVEAL ABOUT A RETRACTION?",
    "■ HOW DOES THE TOPIC OF MULTIDISCIPLINARY RESEARCH AFFECT RETRACTIONS?",
    "■ CONCLUSIONS AND FUTURE OUTLOOK",
)
REFS = [
    "(1) Fanelli, D. Why Growing Retractions Are (Mostly) a Good Sign. PLoS Medicine 2013, 10 (12), No. e1001563.",
    "(2) Larsen, P. O.; von Ins, M. The rate of growth in scientific publication. Scientometrics 2010, 84, 575-603.",
    "(3) Bornmann, L.; Haunschild, R.; Mutz, R. Growth rates of modern science. Humanit. Soc. Sci. Commun. 2021, 8, 224.",
    "(4) Franzoni, C.; Scellato, G.; Stephan, P. Changing Incentives to Publish. Science 2011, 333 (6043), 702-703.",
]


def _wrap(text: str, width: int = 12) -> list[str]:
    words = text.split()
    return [" ".join(words[i : i + width]) for i in range(0, len(words), width)]


def _paragraph(page: fitz.Page, y: float, repeat: int = 3) -> float:
    for _ in range(repeat):
        for line in _wrap(SENTENCE + "."):
            page.insert_text((50, y), line, fontsize=9.5, fontname="helv")
            y += 12
    return y + 6


def make_viewpoint_pdf(bold_headings: bool = True) -> bytes:
    """A Viewpoint: no Introduction, Methods or Results, only question headings set in bold capitals with a mark."""
    doc = fitz.open()
    first = doc.new_page()
    first.insert_text((50, 40), "ACS Omega", fontsize=8, fontname="helv")
    first.insert_text((50, 80), "Analysis of Retracted Manuscripts in Chemistry", fontsize=18, fontname="hebo")
    first.insert_text((50, 105), "Yulia Sevryugina and Ryan Jimenez", fontsize=10, fontname="helv")
    y = 140.0
    for line in _wrap("ABSTRACT: " + SENTENCE + " and the reasons given for it.", 12):
        first.insert_text((50, y), line, fontsize=9, fontname="helv")
        y += 11
    y += 12
    y = _paragraph(first, y, 2)
    font = "hebo" if bold_headings else "helv"
    size = 11 if bold_headings else 9.5  # without bold or larger type nothing sets a heading apart
    page = first
    for number, heading in enumerate(HEADINGS):
        if y > 650:
            page = doc.new_page()
            page.insert_text((50, 40), "ACS Omega", fontsize=8, fontname="helv")
            y = 80.0
        page.insert_text((50, y), heading, fontsize=size, fontname=font)
        y = _paragraph(page, y + 18, 3)
    page = doc.new_page()
    page.insert_text((50, 40), "ACS Omega", fontsize=8, fontname="helv")
    page.insert_text((50, 80), "■ ASSOCIATED CONTENT", fontsize=size, fontname=font)
    page.insert_text((50, 100), "Supporting information is available free of charge on the publisher site.", fontsize=9.5, fontname="helv")
    page.insert_text((50, 140), "■ ACKNOWLEDGMENTS", fontsize=size, fontname=font)
    page.insert_text((50, 160), "The authors thank the Retraction Watch team for their support and for the data.", fontsize=9.5, fontname="helv")
    page.insert_text((50, 200), "■ REFERENCES", fontsize=size, fontname=font)
    y = 220.0
    for ref in REFS:
        page.insert_text((50, y), ref, fontsize=8.5, fontname="helv")
        y += 12
    data = doc.tobytes()
    doc.close()
    return data


# ------------------------------------------------------------------ headings are read through their marks

def test_a_mark_in_front_of_a_heading_is_not_part_of_its_title() -> None:
    assert _heading_title("■ REFERENCES") == "references"
    assert _heading_title("▪ Acknowledgments") == "acknowledgments"
    assert _heading_title("• MATERIALS & METHODS") == "materials and methods"
    assert _heading_title("2.1 Methods") == "methods"
    assert _heading_title("(3) Results") == "results"


def test_numbered_or_capital_headings_that_name_a_part_are_headings_without_being_in_a_list() -> None:
    assert _heading_title("3. EXPERIMENTAL RESULTS AND ANALYSIS") == "experimental results and analysis"
    assert _heading_title("4 Proposed approach") == "proposed approach"
    assert _heading_title("CONCLUSIONS AND FUTURE OUTLOOK") == "conclusions and future outlook"
    # without a number or capitals, or when it is cut off in the middle of a sentence, it is text
    assert _heading_title("Proposed approach") is None
    assert _heading_title("4 Results of the") is None
    assert _heading_title("The results were") is None


def test_the_part_of_a_paper_is_read_from_the_words_of_the_heading() -> None:
    assert parser._keyword_role("experimental results and analysis") == pp.ROLE_RESULTS
    assert parser._keyword_role("materials and methods") == pp.ROLE_METHODS
    assert parser._keyword_role("conclusions and future outlook") == pp.ROLE_DISCUSSION
    assert parser._keyword_role("why are manuscripts retracted?") is None


def test_a_heading_outside_every_list_needs_the_layout_to_say_so() -> None:
    assert _heading_parts("■ WHY ARE MANUSCRIPTS RETRACTED?") is None
    token = parser._LAYOUT_HEADINGS.set(frozenset({parser._layout_key("■ WHY ARE MANUSCRIPTS RETRACTED?")}))
    try:
        assert _heading_parts("� WHY ARE MANUSCRIPTS RETRACTED?") == (
            "why are manuscripts retracted?", "Why are manuscripts retracted?",
        )
    finally:
        parser._LAYOUT_HEADINGS.reset(token)


# ------------------------------------------------------------------ a PDF with headings that are in no list

def test_a_viewpoint_with_bold_question_headings_is_read_by_those_headings() -> None:
    parsed = parse_document(make_viewpoint_pdf(), "viewpoint.pdf")
    assert parsed.structure == "detected"
    assert list(parsed.sections) == [
        "Why are manuscripts retracted?",
        "What does the timeline reveal about a retraction?",
        "How does the topic of multidisciplinary research affect retractions?",
        "discussion",
    ]
    assert [parsed.section_roles[name] for name in parsed.sections] == ["thematic"] * 3 + ["discussion"]
    # the back matter is not glued to the last section, and the reference list is its own
    assert "Retraction Watch team" not in parsed.sections["discussion"]
    assert "Supporting information is available" not in parsed.sections["discussion"]
    assert len(parsed.references) == len(REFS)
    assert parsed.references[0].startswith("Fanelli, D.")
    assert parsed.parse_warnings == []


def test_a_title_or_a_label_that_appears_once_is_never_a_heading() -> None:
    parsed = parse_document(make_viewpoint_pdf(), "viewpoint.pdf")
    assert not any("Analysis of Retracted" in name for name in parsed.sections)
    assert "ACS Omega" not in " ".join(parsed.sections)


def test_without_any_typographic_mark_headings_outside_the_list_stay_text() -> None:
    parsed = parse_document(make_viewpoint_pdf(bold_headings=False), "viewpoint.pdf")
    assert "Why are manuscripts retracted?" not in parsed.sections
    assert parsed.structure != "detected"


# ------------------------------------------------------------------ Word headings

def _docx(headings: list[str], styled: bool = True, bold_only: bool = False) -> bytes:
    document = Document()
    document.add_paragraph("A Study of Layered Systems", style="Title")
    document.add_paragraph("Abstract")
    document.add_paragraph(SENTENCE + ". " + SENTENCE + ".")
    for heading in headings:
        if styled:
            document.add_heading(heading, level=1)
        else:
            run = document.add_paragraph().add_run(heading)
            run.bold = bold_only
        for _ in range(3):
            document.add_paragraph(SENTENCE + ".")
    document.add_paragraph("References")
    for index in range(1, 4):
        document.add_paragraph(f"{index} A. Author, B. Writer, Nature, 200{index}, 1, 10{index}-110.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_word_heading_styles_give_the_sections_even_for_titles_in_no_list() -> None:
    parsed = parse_docx(_docx(["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"]))
    assert parsed.structure == "detected"
    assert list(parsed.sections) == ["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"]


def test_bold_only_paragraphs_in_word_are_headings_when_they_repeat() -> None:
    parsed = parse_docx(_docx(["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"], styled=False, bold_only=True))
    assert list(parsed.sections) == ["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"]


def test_plain_unmarked_paragraphs_in_word_are_not_headings() -> None:
    parsed = parse_docx(_docx(["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"], styled=False))
    assert "Proposed Framework" not in parsed.sections


# ------------------------------------------------------------------ the same file gives the same sections

def test_the_same_file_gives_the_same_structure_every_time() -> None:
    pdf = make_viewpoint_pdf()
    first = parse_document(pdf, "a.pdf")
    for _ in range(3):
        again = parse_document(pdf, "a.pdf")
        assert again.structure_signature == first.structure_signature
        assert again.sections == first.sections and again.section_roles == first.section_roles


def test_the_structure_does_not_depend_on_the_process(tmp_path: Path) -> None:
    """String hashing differs from one process to the next: the sections must not."""
    path = tmp_path / "viewpoint.pdf"
    path.write_bytes(make_viewpoint_pdf())
    script = (
        "import json, sys\n"
        "from services.parser import parse_document\n"
        "p = parse_document(open(sys.argv[1], 'rb').read(), 'x.pdf')\n"
        "print(json.dumps([p.structure_signature, list(p.sections), p.section_roles, p.abstract]))\n"
    )
    root = Path(__file__).resolve().parents[1]
    outputs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root)}
        done = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True, text=True, env=env, cwd=root, check=True
        )
        outputs.append(json.loads(done.stdout.strip().splitlines()[-1]))
    assert outputs[0] == outputs[1] == outputs[2]


def test_a_small_edit_inside_a_section_keeps_the_section_names() -> None:
    before = _docx(["Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"])
    document = Document(io.BytesIO(before))
    document.add_paragraph("An extra sentence added by the author during the revision of the manuscript.")
    buffer = io.BytesIO()
    document.save(buffer)
    after = parse_docx(buffer.getvalue())
    original = parse_docx(before)
    assert list(after.sections) == list(original.sections)
    assert after.structure_signature == original.structure_signature


# ------------------------------------------------------------------ the introduction that has no heading

ABSTRACT_TEXT = (
    "ABSTRACT: In this Viewpoint we discuss manuscripts retracted during a twenty year period and we show that "
    "most of them were withdrawn because of misconduct while errors and other concerns make up a smaller part."
)
OPENING_TEXT = (
    "Retractions and corrections are tools that are imperative for correcting science and preserving its integrity. "
    "The number of retractions has been increasing every year since the nineteen eighties. This growth is explained "
    "by a stronger system in which the scientific community has become more vigilant and more proactive in "
    "identifying and reporting fraudulent research. The surge in retractions demanded a curated list of retracted "
    "articles, which became the largest and most comprehensive repository of retracted articles available today. "
    "Exploring the reasons for retraction raises awareness of the mechanisms that exist for correcting science and "
    "for advancing the ethics policies that journals and institutions apply to the work of their authors."
)


def make_unheaded_intro_pdf() -> bytes:
    """Abstract and the first paragraphs in the same typeface, each in its own block, then bold headings."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Retractions in Chemistry", fontsize=18, fontname="hebo")
    page.insert_textbox(fitz.Rect(50, 90, 550, 190), ABSTRACT_TEXT, fontsize=9.5, fontname="helv")
    page.insert_textbox(fitz.Rect(50, 205, 550, 350), OPENING_TEXT, fontsize=9.5, fontname="helv")
    y = 370.0
    for number, heading in enumerate(HEADINGS[:3]):
        page.insert_text((50, y), heading, fontsize=11, fontname="hebo")
        page.insert_textbox(fitz.Rect(50, y + 8, 550, y + 80), SENTENCE + ". " + SENTENCE + ".", fontsize=9.5, fontname="helv")
        y += 100
    data = doc.tobytes()
    doc.close()
    return data


def test_the_abstract_ends_with_its_own_block_not_at_the_next_heading() -> None:
    parsed = parse_document(make_unheaded_intro_pdf(), "paper.pdf")
    assert "most of them were withdrawn because of misconduct" in parsed.abstract
    assert "Retractions and corrections are tools" not in parsed.abstract


def test_the_paragraphs_before_the_first_heading_become_the_introduction() -> None:
    parsed = parse_document(make_unheaded_intro_pdf(), "paper.pdf")
    assert list(parsed.sections)[0] == "introduction"
    assert parsed.section_roles["introduction"] == pp.ROLE_INTRODUCTION
    assert parsed.sections["introduction"].startswith("Retractions and corrections are tools")
    assert "ABSTRACT" not in parsed.sections["introduction"] and "Why are manuscripts" not in parsed.sections["introduction"]
    assert parsed.structure == "detected"


def test_a_real_introduction_heading_is_never_doubled_by_an_implicit_one() -> None:
    text = "\n".join([
        "Title of the paper", "Abstract", ABSTRACT_TEXT.replace("ABSTRACT: ", ""),
        "1. Introduction", OPENING_TEXT, "2. Methods", SENTENCE + ". " + SENTENCE + ".",
        "3. Results", SENTENCE + ". " + SENTENCE + ".", "4. Conclusions", SENTENCE + ". " + SENTENCE + ".",
    ])
    parsed = parser._build_document(text, one_reference_per_line=True)
    assert list(parsed.sections).count("introduction") == 1
    assert parsed.sections["introduction"].startswith("Retractions and corrections")


def test_a_short_front_matter_before_the_first_heading_is_not_an_introduction() -> None:
    text = "\n".join([
        "Title of the paper", "Abstract", ABSTRACT_TEXT.replace("ABSTRACT: ", ""),
        "Some words said here once more.",
        "2. Methods", SENTENCE + ". " + SENTENCE + ".",
        "3. Results", SENTENCE + ". " + SENTENCE + ".", "4. Conclusions", SENTENCE + ". " + SENTENCE + ".",
    ])
    parsed = parser._build_document(text, one_reference_per_line=True)
    assert "introduction" not in parsed.sections


def test_in_a_word_file_a_long_paragraph_after_a_full_abstract_is_the_introduction() -> None:
    abstract = " ".join(["The study measured the reasons for retraction among many manuscripts."] * 14)
    document = Document()
    document.add_paragraph("A Study of Retractions", style="Title")
    document.add_paragraph("Abstract")
    document.add_paragraph(abstract)
    document.add_paragraph(OPENING_TEXT)
    for heading in ("Proposed Framework", "Evaluation Protocol Design", "Limits of the Design"):
        document.add_heading(heading, level=1)
        document.add_paragraph(SENTENCE + ". " + SENTENCE + ".")
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = parse_docx(buffer.getvalue())
    assert "Retractions and corrections are tools" not in parsed.abstract
    assert parsed.sections["introduction"].startswith("Retractions and corrections are tools")


# ------------------------------------------------------------------ a revision keeps the earlier sections

def _derived_ctx(previous: list[str]):
    from services.analysis_guard import AnalysisContext

    return AnalysisContext(
        article_type=pp.RESEARCH_ARTICLE, structure_mode="derived", previous_sections=previous, body="text"
    )


def test_a_revision_of_a_paper_without_headings_is_told_to_keep_its_earlier_sections() -> None:
    from services.llm import base

    block = base._profile_block(_derived_ctx(["Introduction", "Results and discussion", "Conclusion"]))
    assert "earlier version" in block
    assert '"Introduction", "Results and discussion", "Conclusion"' in block


def test_only_names_from_the_fixed_list_are_carried_over_and_a_first_version_has_no_such_note() -> None:
    from services.llm import base

    assert "earlier version" not in base._profile_block(_derived_ctx([]))
    assert "earlier version" not in base._profile_block(_derived_ctx(["Why are manuscripts retracted?"]))
    mixed = base._profile_block(_derived_ctx(["Why are manuscripts retracted?", "Conclusion"]))
    assert '"Conclusion"' in mixed and "Why are manuscripts" not in mixed


def test_the_earlier_sections_are_read_from_the_stored_summary_without_any_text() -> None:
    import datetime
    import uuid

    from services.revisions import PreviousAnalysis

    previous = PreviousAnalysis(
        id=uuid.uuid4(), created_at=datetime.datetime.now(datetime.timezone.utc), filename="a.docx", journal="J",
        article_type=None, file_sha256="x", signature=[], summary={"sections": {"Introduction": 70, "Conclusion": 65}},
    )
    assert previous.section_names() == ["Introduction", "Conclusion"]


# ------------------------------------------------------------------ a table's column header is not a heading

PROSE = " ".join(["The model was evaluated on the benchmark and compared with earlier work in detail."] * 6)


def _lines_with_table(page_number: bool) -> list[str]:
    return [
        "1 Introduction", PROSE, "2 Methodology", PROSE, "3 Results", PROSE,
        *(["10"] if page_number else []),
        "Method",
        "Penalized logP QED 1st 2nd 3rd ZINC (Dataset) 4.52 4.30 4.23 0.948 0.948",
        "CVAE [4] 1.98 1.42 1.19 N/A N/A N/A GVAE [5] 2.94 2.89 2.80",
        "JT-VAE [8] 5.30 4.93 4.49 0.925 0.911 0.910",
        "The constrained benchmark result after the table stays in the Results. " + PROSE,
        "4 Conclusion", PROSE,
    ]


def test_method_over_a_results_table_does_not_start_a_second_methods_section() -> None:
    for page_number in (False, True):
        titles: dict[str, str] = {}
        sections, roles, _ = parser._extract_sections_detailed(_lines_with_table(page_number), titles_out=titles)
        assert list(sections) == ["introduction", "methods", "results", "discussion"]
        assert "stays in the Results" in sections["results"] and "stays in the Results" not in sections["methods"]
        assert titles == {"introduction": "Introduction", "methods": "Methodology", "results": "Results", "discussion": "Conclusion"}


def test_a_real_methods_heading_followed_by_prose_is_kept() -> None:
    lines = ["1 Introduction", PROSE, "2 Methods", "2.1 Materials", PROSE, "3 Results", PROSE]
    sections, roles, _ = parser._extract_sections_detailed(lines)
    assert roles.get("methods") == "methods" and sections["methods"].startswith(PROSE[:40])


def test_two_standard_headings_under_one_key_are_both_named() -> None:
    lines = ["1 Introduction", PROSE, "2 Results", PROSE, "3 Discussion", PROSE, "4 Conclusions", PROSE]
    titles: dict[str, str] = {}
    parser._extract_sections_detailed(lines, titles_out=titles)
    assert titles["discussion"] == "Discussion / Conclusions"
