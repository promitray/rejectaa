"""Splitting a bibliography into references: numbered lists with and without punctuation,
(a)/(b) sub-references, running headers, and trimming text glued to the last reference.

The sample lines are taken from a real RSC paper (Polym. Chem. 2017, 8, 6199) whose 27 numbered
entries were once read as 2 references.
"""
import io

import fitz
import pytest
from docx import Document

from services import citation_refs, citations, parser
from services.citation_refs import candidate_from_crossref, compare, parse_reference, trim_after_citation
from services.parser import (
    _drop_running_lines,
    _extract_references,
    _split_numbered_entries,
    _split_subreferences,
    parse_document,
)

# As PyMuPDF emits it: numbers inline with the text, wrapped lines, stray one-word lines.
RSC_LINES = """\
1 (a) A. M. Schenzel, N. Moszner and C. Barner-Kowollik,
Polym. Chem., 2017, 8, 414–420; (b) A. M. Schenzel,
C. Klein, K. Rist, N. Moszner and C. Barner-Kowollik, Adv.
Sci., 2016, 3, 1500361.
2 (a) A. Noel, Y. P. Borguet, J. E. Raymond and K. L. Wooley,
Macromolecules,
2014,
47,
7109–7117;
(b)
A.
Noel,
Y. P. Borguet and K. L. Wooley, ACS Macro Lett., 2015, 4,
645–650.
3 (a) M. P. Robin, A. B. Mabire, J. C. Damborsky, E. S. Thom,
U. H. Winzer-Serhan, J. E. Raymond and R. K. O’Reilly,
J. Am. Chem. Soc., 2013, 135, 9518–9524; (b) W. Chen,
Q. Huang, W. Ou, Y. Hao, L. Wang, K. Zeng, H. Guo, J. Li
and Y.-N. Liu, Small, 2014, 10, 1261–1265.
4 R. J. Nudo, Front. Hum. Neurosci., 2013, 7, 887.
5 (a) D. A. Davis, A. Hamilton, J. L. Yang, L. D. Cremar,
Nature, 2009, 459, 68–72; (b) N. Bruns, K. Pustelny,
Chem., Int. Ed., 2009, 48, 5666–5669.
6 K. Zhang, Y. Hu, L. Wang, J. Fan, M. J. Monteiro and Z. Jia,
Polym. Chem., 2017, 8, 1815–1823.
Fig. 4 Analysis of P1, P2 and P3 by DLS in DMAc + 0.3% LiBr, the
average (minimum of 3 measurements per sample) volume size distributions are shown.
7 J. R. Lakowicz, Principles of Fluorescence Spectroscopy,
Kluwer Academic, New York, 2nd edn, 1999.
8 (a) I. M. Bystryak, G. I. Likhtenshtein, A. I. Kotelnikov,
O. H. Hankovsky and K. Hideg, Russ. J. Phys. Chem. A, 1986,
60, 1679–1983; (b) J. P. Blinco, K. E. Fairfull-Smith,
B. J. Morrow and S. E. Bottle, Aust. J. Chem., 2011, 64, 373–389.
9 (a) K. E. Fairfull-Smith and S. E. Bottle, Eur. J. Org. Chem.,
2008, 5391–5400; (b) J. P. Blinco, J. C. McMurtrie and
S. E. Bottle, Eur. J. Org. Chem., 2007, 4638–4641.
10 Pyrene excimer formation is found to be dramatically
enhanced when dealing with randomly labeled polymers.
11 F. M. Winnik, Chem. Rev., 1993, 93, 587–614.""".split("\n")


def refs(lines):
    return _extract_references(lines)


# ---------------------------------------------------------------------------
# Numbered lists
# ---------------------------------------------------------------------------
def test_a_list_numbered_with_only_a_space_is_split_into_its_references():
    result = refs(RSC_LINES)
    assert result[0].startswith("A. M. Schenzel, N. Moszner and C. Barner-Kowollik, Polym. Chem., 2017, 8, 414")
    assert any(r.startswith("R. J. Nudo") for r in result)
    assert any(r.startswith("J. R. Lakowicz") for r in result)
    # entries 1-9 carry 2+2+2+1+2+1+1+2+2 = 15 citations, entry 10 is a note (dropped), entry 11 adds one
    assert len(result) == 16
    assert not any("Pyrene excimer" in r for r in result)
    # nothing was merged into a blob
    assert max(len(r) for r in result) < 260


def test_the_old_failure_two_giant_references_cannot_happen_again():
    result = refs(RSC_LINES)
    assert len(result) > 10
    assert not any("8 (a) I. M. Bystryak" in r or "7 J. R. Lakowicz" in r for r in result)


@pytest.mark.parametrize("style", ["[{n}] ", "{n}. ", "{n}) ", "{n} "])
def test_every_numbering_style_gives_the_same_references(style):
    entries = [
        "A. Author, B. Author, J. Chem. Phys., 2001, 114, 100-110.",
        "C. Writer and D. Writer, Nature, 2005, 435, 200-204.",
        "E. Scribe, Science, 2010, 330, 12-15.",
        "F. Penman, Chem. Rev., 2015, 115, 3000-3050.",
    ]
    lines = [f"{style.format(n=i)}{text}" for i, text in enumerate(entries, 1)]
    assert refs(lines) == entries


def test_a_wrapped_line_that_starts_with_a_digit_does_not_start_a_new_entry():
    lines = [
        "1 A. Author and B. Author, J. Chem. Phys.,",
        "2001, 114, 100-110.",
        "2 C. Writer, Angew. Chem. Int. Ed.,",
        "45 Reviewer Notes appear here by accident and continue,",  # 45 is not the next number (3)
        "2005, 435, 200-204.",
        "3 E. Scribe, Science, 2010, 330, 12-15.",
    ]
    result = refs(lines)
    assert len(result) == 3
    assert "45 Reviewer Notes" in result[1]


def test_a_lost_entry_does_not_swallow_the_rest_of_the_list():
    lines = [
        "1 A. Author, J. Chem. Phys., 2001, 114, 100-110.",
        "2 C. Writer, Nature, 2005, 435, 200-204.",
        "4 E. Scribe, Science, 2010, 330, 12-15.",
        "5 F. Penman, Chem. Rev., 2015, 115, 3000-3050.",
    ]
    assert len(refs(lines)) == 4


def test_a_list_that_is_not_numbered_is_left_to_the_other_splitters():
    assert _split_numbered_entries("Smith, J. (2001). A title. Journal, 5, 1-2.\n\nJones, K. (2002). Other. J, 6, 3-4.") is None


# ---------------------------------------------------------------------------
# (a) (b) (c) sub-references
# ---------------------------------------------------------------------------
def test_sub_references_become_separate_citations_in_order():
    result = refs(RSC_LINES)
    assert result[0].endswith("Polym. Chem., 2017, 8, 414–420")
    assert result[1].startswith("A. M. Schenzel, C. Klein") and result[1].endswith("Adv. Sci., 2016, 3, 1500361.")
    assert result[2].startswith("A. Noel, Y. P. Borguet, J. E. Raymond") and result[3].startswith("A. Noel, Y. P. Borguet and K. L. Wooley")


def test_a_lead_in_before_the_first_marker_is_dropped():
    text = "See for examples: (a) S. Sinnwell, Chem. Commun., 2008, 2052–2054; (b) S. Sinnwell, Macromolecules, 2008, 41, 7904–7912."
    pieces = _split_subreferences(text)
    assert [p[:12] for p in pieces] == ["S. Sinnwell,", "S. Sinnwell,"]
    assert not any("See for examples" in p for p in pieces)


def test_markers_inside_a_sentence_or_out_of_order_are_not_split():
    assert _split_subreferences("A. Author, J. Chem., 2001, 5, 1 (c) is not a marker here.") == [
        "A. Author, J. Chem., 2001, 5, 1 (c) is not a marker here."
    ]
    # (b) with no (a) before it
    assert len(_split_subreferences("A. Author, J. Chem., 2001, 5, 1; (b) B. Author, J. Phys., 2002, 6, 2.")) == 1


def test_a_single_a_marker_is_just_removed():
    result = refs(["1 (a) A. Author, J. Chem. Phys., 2001, 114, 100-110.", "2 B. Author, J. Chem. Phys., 2002, 115, 1-9.",
                   "3 C. Author, J. Chem. Phys., 2003, 116, 2-9."])
    assert result[0].startswith("A. Author")


def test_a_footnote_inside_the_list_is_not_a_reference_but_a_doi_only_entry_is():
    lines = [
        "1 A. Author, J. Chem. Phys., 2001, 114, 100-110.",
        "2 Pyrene excimer formation is found to be dramatically enhanced when dealing with randomly labeled polymers.",
        "3 B. Writer, Dataset. 10.5281/zenodo.123456",
        "4 C. Scribe, Science, 2010, 330, 12-15.",
    ]
    result = refs(lines)
    assert len(result) == 3
    assert not any("Pyrene excimer" in r for r in result)
    assert any("10.5281/zenodo.123456" in r for r in result)


# ---------------------------------------------------------------------------
# Running headers and footers
# ---------------------------------------------------------------------------
def page(n, body):
    return f"Polymer Chemistry\nCommunication\n{body}\nThis journal is © The Royal Society of Chemistry 2017\nPolym. Chem., 2017, 8, {6198 + n}–6203 | {6198 + n}\nView Article Online\n"


def test_lines_repeated_on_several_pages_are_removed_even_when_page_numbers_change():
    words = ["alpha", "bravo", "charlie", "delta", "echo"]
    pages = [page(n, f"Body text of the {words[n - 1]} page that is unique.") for n in range(1, 6)]
    cleaned = "\n".join(_drop_running_lines(pages))
    for junk in ("This journal is", "View Article Online", "Polym. Chem., 2017", "Polymer Chemistry", "Communication"):
        assert junk not in cleaned
    assert "Body text of the charlie page that is unique." in cleaned


def test_short_documents_and_short_lines_are_left_alone():
    two_pages = [page(1, "A"), page(2, "B")]
    assert _drop_running_lines(two_pages) == two_pages
    names = ["one", "two", "three", "four", "five"]
    many = [f"Fig. {n}\nUnique body {names[n]}.\nTable 1" for n in range(5)]
    assert all("Table 1" in text for text in _drop_running_lines(many))  # under 10 characters


def test_a_real_pdf_with_running_headers_gives_clean_references():
    pdf = fitz.open()
    for number in range(1, 6):
        pg = pdf.new_page()
        pg.insert_text((50, 40), "Journal of Tests  View Article Online", fontsize=9)
        pg.insert_text((50, 800), f"This journal is (c) The Society 2017   Test. Chem., 2017, 8, {100 + number}", fontsize=9)
        if number < 5:
            pg.insert_text((50, 100), f"Body text for page {number} of this synthetic paper about polymers.", fontsize=10)
        else:
            y = 100
            pg.insert_text((50, y), "References", fontsize=12)
            for line in ("1 (a) A. Author and B. Author, J. Chem. Phys., 2001, 114, 100-110; (b)",
                         "C. Writer, Nature, 2005, 435, 200-204.",
                         "2 E. Scribe, Science, 2010, 330, 12-15.",
                         "3 F. Penman, Chem. Rev., 2015, 115, 3000-3050."):
                y += 18
                pg.insert_text((50, y), line, fontsize=10)
    parsed = parse_document(pdf.tobytes(), "x.pdf")
    assert len(parsed.references) == 4
    assert not any("This journal" in r or "View Article" in r for r in parsed.references)


# ---------------------------------------------------------------------------
# Trimming and tidying
# ---------------------------------------------------------------------------
def test_text_glued_after_the_last_reference_is_cut_off():
    glued = "M. J. Robb, W. Li, J. S. Moore, ACS Cent. Sci., 2016, 2, 598–603. Fig. 4 Analysis of P1, P2 and P3 by DLS in DMAc."
    assert trim_after_citation(glued).endswith("2016, 2, 598–603.")
    assert "Fig. 4" not in trim_after_citation(glued)
    assert trim_after_citation("S. Majeed, Nanoscale Res. Lett., 2012, 7, 296. Commun").endswith("7, 296.")


def test_a_doi_or_a_note_in_brackets_after_the_citation_is_kept():
    with_doi = "A. Author, J. Chem. Phys., 2001, 114, 100-110. DOI: 10.1063/1.1234567"
    assert trim_after_citation(with_doi) == with_doi
    assert trim_after_citation("A. Author, Nature, 2001, 5, 10-11. (Nobel Lecture)").endswith("(Nobel Lecture)")


def test_hyphens_and_dashes_broken_by_line_wraps_are_healed():
    result = refs([
        "1 K. E. Fairfull-Smith, C. Ouellet-",
        "Plamondon, I. Burgert, J. Mater. Chem. A, 2014, 2, 827–",
        "831.",
        "2 B. Author, J. Chem. Phys., 2002, 115, 1-9.",
        "3 C. Author, J. Chem. Phys., 2003, 116, 2-9.",
    ])
    assert "Ouellet-Plamondon" in result[0]
    assert "827–831" in result[0]


# ---------------------------------------------------------------------------
# Word files share the same cleaning
# ---------------------------------------------------------------------------
def test_a_word_paragraph_with_sub_references_is_split_too():
    document = Document()
    document.add_heading("References", level=1)
    document.add_paragraph("A. Author, J. Chem. Phys., 2001, 114, 100-110; (b) B. Writer, Nature, 2005, 435, 200-204.")
    document.add_paragraph("(a) C. Scribe, Science, 2010, 330, 12-15; (b) D. Penman, Chem. Rev., 2015, 115, 3000-3050.")
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = parser.parse_docx(buffer.getvalue())
    assert len(parsed.references) == 3  # first paragraph has no (a): not split; second splits in two


# ---------------------------------------------------------------------------
# Matching: journals without a volume, spelling differences, books
# ---------------------------------------------------------------------------
def test_a_journal_without_a_volume_is_read_from_year_and_pages():
    parsed = parse_reference("K. E. Fairfull-Smith and S. E. Bottle, Eur. J. Org. Chem., 2008, 5391–5400")
    assert (parsed.first_author, parsed.year, parsed.volume, parsed.first_page) == ("fairfullsmith", 2008, None, "5391")
    normal = parse_reference("Hatice Mutlu, Polym. Chem., 2017, 8, 6199–6203")
    assert (normal.volume, normal.first_page) == ("8", "6199")  # the volume pattern still wins


def _item(**overrides):
    base = {"title": ["Fluorescent Protein Senses and Reports Mechanical Damage"], "author": [{"family": "Mäkya"}],
            "issued": {"date-parts": [[2013]]}, "volume": "25", "page": "2701-2706"}
    base.update(overrides)
    return base


def test_a_one_letter_spelling_difference_in_the_surname_is_accepted_when_year_volume_and_page_agree():
    parsed = parse_reference("K. Makyla, C. Muller and N. Bruns, Adv. Mater., 2013, 25, 2701–2706.")
    assert compare(parsed, candidate_from_crossref(_item())).verified


def test_but_not_when_only_the_year_agrees():
    parsed = parse_reference("K. Makyla, C. Muller and N. Bruns, Adv. Mater., 2013, 25, 2701–2706.")
    other = _item(volume="9", page="11-12")
    assert not compare(parsed, candidate_from_crossref(other)).verified


def test_a_different_surname_is_not_accepted_just_because_everything_else_matches():
    parsed = parse_reference("K. Smith, C. Muller and N. Bruns, Adv. Mater., 2013, 25, 2701–2706.")
    assert not compare(parsed, candidate_from_crossref(_item())).verified


def test_a_book_is_verified_by_its_title_appearing_in_the_cited_line_with_the_author():
    parsed = parse_reference("J. R. Lakowicz, Principles of Fluorescence Spectroscopy, Kluwer Academic, New York, 2nd edn, 1999.")
    book = {"title": ["Principles of Fluorescence Spectroscopy"], "author": [{"family": "Lakowicz"}],
            "issued": {"date-parts": [[1999]]}}
    assert compare(parsed, candidate_from_crossref(book)).verified
    other_author = {**book, "author": [{"family": "Someone Else"}]}
    assert not compare(parsed, candidate_from_crossref(other_author)).verified


def test_an_unrelated_record_that_shares_a_year_and_page_is_not_offered_as_closest():
    trace = citations.Trace()
    parsed = parse_reference("I. M. Bystryak, G. I. Likhtenshtein, Russ. J. Phys. Chem. A, 1986, 60, 1679–1983")
    unrelated = {"title": ["Reversal of sulfamerazine inhibition of rat hepatic synthesis"], "author": [{"family": "Rosenthal"}],
                 "issued": {"date-parts": [[1986]]}, "volume": "9", "page": "1679-1683"}
    candidate = candidate_from_crossref(unrelated)
    citations._offer_closest(trace, "CrossRef", compare(parsed, candidate), candidate.title, 1986)
    assert trace.closest is None


# ---------------------------------------------------------------------------
# Unnumbered author-year lists (APA, Harvard)
# ---------------------------------------------------------------------------
AUTHOR_YEAR_LINES = """\
Akopyan, F., J. Sawada, A. Cassidy, R. Alvarez-Icaza, J. Arthur, P. Merolla, N. Imam, Y. Nakamura, P. Datta, G.-J.
Nam, B. Taba, M. Beakes, B. Brezzo, J. B. Kuang, R. Manohar, W. P. Risk, B. Jackson, and D. S. Modha (2015).
Truenorth: Design and tool flow of a 65 mw 1 million neuron programmable neurosynaptic chip. IEEE Transactions
on Computer-Aided Design of Integrated Circuits and Systems 34(10), 1537–1557.
Ayasi, B., ´
A. M. Garc´
ıa-Vico, C. J. Carmona, and M. Saleh (2024). Advancing computational frontiers: Spiking neural
networks in high-energy efficiency computing across diverse domains. In Advances in Artificial Intelligence,
pp. 9–18. Springer Nature Switzerland.
Aydin, A., M. Gehrig, D. Gehrig, and D. Scaramuzza (2024). A hybrid ann-snn architecture for low-power and low
latency visual perception. In Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition,
pp. 5701–5711.
Bacho, F. and D. Chu (2023). Exploring trade-offs in spiking neural networks. Neural Computation 35(10), 1627–
1656.
Bittar, A. and P. Garner (2022). A surrogate gradient spiking baseline for speech command recognition. Frontiers in
Neuroscience 16, 865897.""".split("\n")


def test_an_unnumbered_author_year_list_is_split_entry_by_entry():
    result = refs(AUTHOR_YEAR_LINES)
    assert len(result) == 5
    assert [r.split(",")[0] for r in result] == ["Akopyan", "Ayasi", "Aydin", "Bacho", "Bittar"]
    assert result[0].endswith("34(10), 1537–1557.")


def test_a_wrapped_author_list_is_not_cut_where_a_line_happens_to_start_like_an_author():
    first = refs(AUTHOR_YEAR_LINES)[0]
    assert "G.-J. Nam, B. Taba" in first  # "Nam, B. Taba..." starts a line but the entry is not finished yet


def test_a_page_range_broken_over_two_lines_stays_in_one_entry():
    entry = [r for r in refs(AUTHOR_YEAR_LINES) if r.startswith("Bacho")][0]
    assert entry.endswith("1627–1656.")


def test_blank_line_separated_lists_keep_their_blank_line_splitting():
    entries = [
        "Smith, J. (2001). A study of things. Journal of Stuff 5(2), 100-110.",
        "Jones, K. and L. Lee (2002). Another study of other things. Nature 435, 200-204.",
        "Brown, M. (2003). Yet another study. Science 300, 12-15.",
    ]
    text = "\n\n".join(entries).split("\n")
    assert refs(text) == entries


def test_giant_chunks_from_page_breaks_fall_back_to_author_year_splitting():
    # Blank lines appear only at page breaks, so blank-line splitting alone would give 2 giant chunks.
    lines = AUTHOR_YEAR_LINES[:9] + [""] + AUTHOR_YEAR_LINES[9:]
    assert len(refs(lines)) == 5


def test_lists_that_start_with_first_names_are_left_as_before():
    # "Jimmy Lei Ba, ..." does not look like "Surname, I.", so the author-year splitter stays out of it
    from services.parser import _split_author_year
    assert _split_author_year("Jimmy Lei Ba, Jamie Ryan Kiros. Layer normalization. 2016.\nGeoffrey Hinton. Dropout. 2014.") is None
