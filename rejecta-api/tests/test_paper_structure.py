"""Article type, section detection and abstract extraction for papers that are not computer-science IMRaD."""

from __future__ import annotations

import fitz

from services import paper_profile as pp
from services import parser
from services.parser import (
    _build_document,
    _extract_sections_detailed,
    _heading_title,
    _reflow_lines,
    parse_document,
)

ABSTRACT = (
    "We pioneer a self-reporting profluorescent release system driven by the thermo-reversible dynamic "
    "covalent ligation of chromophores to a lateral polymer chain, whose fluorescence is silenced by unpaired "
    "spins of nitroxide moieties prior to release. The platform holds significant potential for imaging "
    "substrate release events."
)
BODY_SENTENCE = (
    "The design and synthesis of self-reporting delivery systems has attracted intense research interest "
    "due to its applications in imaging and in drug delivery systems"
)


def _wrap(text: str, width: int = 11) -> list[str]:
    words = text.split()
    return [" ".join(words[i : i + width]) for i in range(0, len(words), width)]


def make_communication_pdf() -> bytes:
    """A journal cover, then a Communication with a label, an abstract in its own typeface and no headings."""
    doc = fitz.open()
    cover = doc.new_page()
    for i, line in enumerate(
        [
            "Registered charity number: 207890",
            "rsc.li/polymers",
            "ISSN 1759-9962",
            "Volume 8 Number 40 28 October 2017 Pages 6191-6336",
            "As featured in:",
            "A joint team pioneers a different paper that is only advertised on this cover page.",
        ]
    ):
        cover.insert_text((50, 80 + 18 * i), line, fontsize=11, fontname="helv")

    first = doc.new_page()
    first.insert_text((50, 50), "COMMUNICATION", fontsize=14, fontname="helv")
    first.insert_text((50, 80), "Cite this: Polym. Chem., 2017, 8, 6199", fontsize=7, fontname="helv")
    first.insert_text((50, 95), "Received 24th August 2017", fontsize=7, fontname="helv")
    first.insert_text((50, 125), "Spin fluorescence silencing enables a release system", fontsize=16, fontname="helv")
    first.insert_text((50, 150), "Hatice Mutlu, Christian Schmitt and Christopher Barner-Kowollik", fontsize=10, fontname="helv")
    y = 190
    for line in _wrap(ABSTRACT, 11):
        first.insert_text((50, y), line, fontsize=8, fontname="tiro")
        y += 11
    y += 12
    for _ in range(14):  # body text: the dominant typeface on the page
        for line in _wrap(BODY_SENTENCE, 11):
            if y < 800:
                first.insert_text((50, y), line, fontsize=9, fontname="helv")
                y += 11
    for number in (2, 3):
        page = doc.new_page()
        page.insert_text((50, 30), "Communication", fontsize=8, fontname="helv")  # running header on later pages
        y = 70
        for _ in range(10):
            for line in _wrap(BODY_SENTENCE + " (see Fig. S4, ESI).", 11):
                page.insert_text((50, y), line, fontsize=9, fontname="helv")
                y += 11
    refs = doc.new_page()
    refs.insert_text((50, 30), "Communication", fontsize=8, fontname="helv")
    refs.insert_text((50, 70), "References", fontsize=12, fontname="helv")
    refs.insert_text((50, 100), "1 A. Author and B. Writer, Polym. Chem., 2001, 5, 1000-1010.", fontsize=9, fontname="helv")
    refs.insert_text((50, 115), "2 C. Chemist, J. Am. Chem. Soc., 2005, 127, 2000-2010.", fontsize=9, fontname="helv")
    refs.insert_text((50, 130), "3 D. Doe and E. Roe, Angew. Chem. Int. Ed., 2010, 49, 300-310.", fontsize=9, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


# ------------------------------------------------------------------ PDF end to end

def test_cover_page_is_dropped_the_label_names_the_type_and_the_abstract_is_found_by_its_typeface() -> None:
    parsed = parse_document(make_communication_pdf(), "paper.pdf")
    assert parsed.article_type == pp.COMMUNICATION
    assert parsed.article_type_source == "label"  # read from page 1, although it is a running header later
    assert parsed.abstract.startswith("We pioneer a self-reporting")
    assert "Registered charity" not in parsed.full_text and "only advertised" not in parsed.full_text
    assert parsed.structure != "detected"  # no headings: the content-based path is used
    assert parsed.refers_to_si is True
    assert len(parsed.references) >= 3


def test_the_users_article_type_wins_over_the_label_in_the_file() -> None:
    parsed = parse_document(make_communication_pdf(), "paper.pdf", article_type="review")
    assert parsed.article_type == pp.REVIEW
    assert parsed.article_type_source == "user"


def test_without_a_label_the_type_is_unknown_not_guessed() -> None:
    text = "A Study of Something\n" + ABSTRACT + "\n" + BODY_SENTENCE
    parsed = _build_document(text, one_reference_per_line=True)
    assert parsed.article_type is None and parsed.article_type_source == "none"


# ------------------------------------------------------------------ headings

def test_a_lowercase_word_alone_on_a_line_is_not_a_heading() -> None:
    assert _heading_title("results") is None
    assert _heading_title("Results") == "results"
    assert _heading_title("3. RESULTS AND DISCUSSION") == "results and discussion"


def test_chemistry_and_outlook_headings_map_to_the_four_roles() -> None:
    assert parser._TITLE_TO_SECTION["experimental section"] == "methods"
    assert parser._TITLE_TO_SECTION["experimental procedures"] == "methods"
    assert parser._TITLE_TO_SECTION["results and discussions"] == "results"
    assert parser._TITLE_TO_SECTION["outlook"] == "discussion"
    assert parser._TITLE_TO_SECTION["conclusion and outlook"] == "discussion"


def test_back_matter_ends_the_last_section() -> None:
    lines = [
        "Introduction", "Intro text. " * 10,
        "Results and discussions", "Result text. " * 10,
        "Conclusions", "Conclusion text. " * 10,
        "Conflicts of interest", "There are no conflicts to declare. " * 3,
        "Supporting Information", "Details in the ESI. " * 3,
    ]
    sections, roles, _ = _extract_sections_detailed(lines)
    assert set(sections) == {"introduction", "results", "discussion"}
    assert "no conflicts" not in sections["discussion"]
    assert roles["results"] == "results"


def test_sentence_fragments_a_justified_pdf_leaves_on_their_own_lines_are_joined() -> None:
    lines = [
        "the observed SEC tailing", "results", "from", "interactions", "between", "the", "polymer and",
        "the column material.", "Results", "A heading that stays on its own line.",
    ]
    out = _reflow_lines(lines)
    assert "results from interactions between the polymer and the column material." in out[1]
    assert "Results" in out  # the real heading is untouched
    sections, _, _ = _extract_sections_detailed(out)
    assert "results" not in sections or len(sections["results"]) > 0


# ------------------------------------------------------------------ perspective / review: thematic sections

REVIEW_TEXT = "\n".join(
    [
        "Polymer brushes in medicine",
        "Abstract",
        "This review summarises recent progress on polymer brushes for medical devices and surfaces. " * 2,
        "1 Introduction",
        "Polymer brushes are dense layers of chains grafted to a surface. " * 8,
        "2 Polymer brushes against fouling",
        "Antifouling brushes resist proteins and cells on implants. " * 8,
        "3 Stimuli-responsive gels",
        "Responsive gels swell and collapse when the temperature changes. " * 8,
        "4 Outlook",
        "Open questions remain about long-term stability in vivo. " * 8,
        "References",
        "1. A. Author, Nature, 2001, 1, 100-110.",
        "2. B. Writer, Science, 2002, 2, 200-210.",
        "3. C. Chemist, J. Am. Chem. Soc., 2003, 3, 300-310.",
    ]
)


def test_a_review_gets_its_numbered_topic_headings_as_thematic_sections() -> None:
    parsed = _build_document(REVIEW_TEXT, one_reference_per_line=True, article_type_hint="review")
    assert list(parsed.sections) == [
        "introduction", "Polymer brushes against fouling", "Stimuli-responsive gels", "discussion",
    ]
    assert parsed.section_roles["Stimuli-responsive gels"] == "thematic"
    assert parsed.structure == "detected"


def test_the_same_text_as_a_research_article_does_not_invent_topic_sections() -> None:
    parsed = _build_document(REVIEW_TEXT, one_reference_per_line=True, article_type_hint="research_article")
    assert "Stimuli-responsive gels" not in parsed.sections
    assert parsed.structure != "detected"


def test_no_more_than_the_maximum_of_topic_sections_are_kept() -> None:
    topics = []
    for n in range(2, parser.MAX_THEMATIC_SECTIONS + 5):
        topics += [f"{n} Topic number {n}", f"Text about topic {n}. " * 8]
    text = "\n".join(["Title", "1 Introduction", "Intro text here. " * 8, *topics, "References", "1. A. Author, Nature, 2001, 1, 100-110."])
    parsed = _build_document(text, one_reference_per_line=True, article_type_hint="review")
    thematic = [k for k, r in parsed.section_roles.items() if r == "thematic"]
    assert len(thematic) == parser.MAX_THEMATIC_SECTIONS
    assert parsed.structure == "partial"  # topics were left out, so the content-based path takes over


# ------------------------------------------------------------------ profile helpers

def test_article_type_labels_are_read_from_the_top_of_the_first_page() -> None:
    assert pp.detect_article_type(["Polymer", "Chemistry", "COMMUNICATION", "Cite this: ..."]) == pp.COMMUNICATION
    assert pp.detect_article_type(["Mini-review", "Title"]) == pp.MINI_REVIEW
    assert pp.detect_article_type(["Perspective"]) == pp.PERSPECTIVE
    assert pp.detect_article_type(["Review Article"]) == pp.REVIEW
    assert pp.detect_article_type(["Full Paper", "x"]) == pp.RESEARCH_ARTICLE
    assert pp.detect_article_type(["This review discusses", "a communication channel"]) is None


def test_coerce_article_type_accepts_keys_and_common_spellings_only() -> None:
    assert pp.coerce_article_type("Mini-review") == pp.MINI_REVIEW
    assert pp.coerce_article_type("full paper") == pp.RESEARCH_ARTICLE
    assert pp.coerce_article_type("Letters") == pp.COMMUNICATION
    assert pp.coerce_article_type("auto") is None
    assert pp.coerce_article_type(None) is None


def test_si_references_are_detected() -> None:
    assert pp.refers_to_si("see ESI section A.2")
    assert pp.refers_to_si("(Fig. S4)")
    assert pp.refers_to_si("Supporting Information is available")
    assert not pp.refers_to_si("measured in SI units at 25 C")


def test_structure_is_detected_only_with_enough_roles_and_coverage() -> None:
    roles = {"introduction": "introduction", "results": "results", "discussion": "discussion"}
    assert pp.assess_structure(roles, 700, 1000, pp.RESEARCH_ARTICLE) == ("detected", 0.7)
    assert pp.assess_structure(roles, 300, 1000, pp.RESEARCH_ARTICLE)[0] == "partial"
    assert pp.assess_structure({"results": "results"}, 900, 1000, pp.RESEARCH_ARTICLE)[0] == "partial"
    assert pp.assess_structure({}, 0, 1000, None) == ("none", 0.0)


# ------------------------------------------------------------------ lettered headings ("D. References")

def test_a_heading_with_a_letter_prefix_is_a_heading() -> None:
    assert _heading_title("D. References") == "references"
    assert _heading_title("D) References") == "references"
    assert _heading_title("A. Introduction") == "introduction"
    assert _heading_title("A. Author, B. Writer, Nature 2001, 5, 100") is None  # an author list is not a heading


REFS = [
    "J. O. Mueller, F. G. Schmidt, J. P. Blinco and C. Barner-Kowollik, Angew. Chem. Int. Ed. 2015, 54, 10284-10288.",
    "Y. G. Lee, Y. Koyama, M. Yonekawa and T. Takata, Macromolecules 2009, 42, 7709-7717.",
]


def _word_text(refs: list[str]) -> str:
    return "\n".join(["Title of a study", "Abstract", "We study something in detail. " * 8, "D. References", *refs])


def test_references_under_a_lettered_heading_with_typed_numbers_are_found_in_a_word_file() -> None:
    typed = [f"{i} {ref}" for i, ref in enumerate(REFS, start=1)]  # "1<tab>citation" in Word becomes "1 citation"
    parsed = _build_document(_word_text(typed), one_reference_per_line=True)
    assert parsed.references == REFS


def test_references_without_typed_numbers_are_unchanged() -> None:
    parsed = _build_document(_word_text(REFS), one_reference_per_line=True)
    assert parsed.references == REFS


def test_a_reference_that_starts_with_a_number_is_not_stripped_unless_the_numbers_run_in_order() -> None:
    odd = ["2019 Annual report of the society. J. Chem. 2019, 5, 100-110.", REFS[1]]
    assert parser._strip_list_numbers(odd) == odd
    assert parser._strip_list_numbers(["1 A. B, J. Chem. 2001, 1, 1.", "3 C. D, J. Chem. 2002, 2, 2."]) == [
        "1 A. B, J. Chem. 2001, 1, 1.", "3 C. D, J. Chem. 2002, 2, 2.",
    ]


# ------------------------------------------------------------------ readable body text with paragraphs

def _pdf_lines() -> list[str]:
    full = "This is a full line of justified text that runs the whole width of the column"
    return [
        "Introduction",
        full, full, "and this line ends the first paragraph here.",
        "The second paragraph starts here with a capital letter and goes on for a while",
        full, "and it ends here.",
    ]


def test_a_pdf_body_keeps_paragraphs_where_a_short_line_ends_a_sentence() -> None:
    text = parser._paragraph_text(_pdf_lines())
    paragraphs = text.split("\n\n")
    assert paragraphs[0] == "Introduction"  # a heading is a paragraph of its own
    assert paragraphs[1].endswith("ends the first paragraph here.")
    assert paragraphs[2].startswith("The second paragraph starts")
    assert len(paragraphs) == 3


def test_in_a_word_body_every_line_is_a_paragraph() -> None:
    assert parser._paragraph_text(["First paragraph.", "Second paragraph."], per_line=True) == "First paragraph.\n\nSecond paragraph."


def test_back_matter_at_the_end_of_the_body_is_cut_but_an_early_heading_is_not() -> None:
    lines = ["Title"] + [f"Body line {i} with enough words to count as text." for i in range(10)] + [
        "Conflicts of interest", "There are none.", "Acknowledgements", "Thanks to everyone.",
    ]
    text = parser._body_text(lines, per_line=True)
    assert "Body line 9" in text and "Conflicts" not in text and "Thanks" not in text
    early = ["Funding", "Some early line."] + [f"Body line {i} with enough words to count as text." for i in range(10)]
    assert "Body line 9" in parser._body_text(early, per_line=True)  # a heading in the first half is ignored


def test_the_paragraph_body_still_gives_the_same_section_boundaries() -> None:
    parsed = _build_document(REVIEW_TEXT, one_reference_per_line=True, article_type_hint="review")
    assert list(parsed.sections)[0] == "introduction"
    assert "\n\n" in parsed.body and "References" not in parsed.body
