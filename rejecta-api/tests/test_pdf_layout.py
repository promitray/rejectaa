"""Tables read as rows and columns, figure labels left out (services.pdf_layout), on PDFs built for the test."""

from __future__ import annotations

import fitz
import pytest

from config import settings
from services.parser import parse_document
from services.pdf_layout import page_layout

PROSE = ("Deep models are compared with tree models on fourteen datasets in this study, and the results are "
         "discussed in detail below with the statistics that support each of the conclusions drawn here.")


def _table_page(page: fitz.Page, top: float = 120) -> None:
    page.insert_text((72, top), "Table 1. Comparison of models on the datasets.", fontsize=9)
    header = ["Dataset", "Metric", "SVM", "XGB", "GCN"]
    rows = [["BACE", "AUC", "0.886", "0.896", "0.880"], ["HIV", "AUC", "0.817", "0.823", "0.834"],
            ["BBBP", "AUC", "0.913", "0.926", "0.902"]]
    xs = [72, 150, 220, 290, 360]
    for r, row in enumerate([header, *rows]):
        for x, cell in zip(xs, row):
            page.insert_text((x, top + 20 + 14 * r), cell, fontsize=8)


def _pdf(with_figure: bool = True) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 40, 540, 100), "1. Introduction\n" + PROSE, fontsize=9)
    _table_page(page)
    page.insert_textbox(fitz.Rect(72, 200, 540, 260), PROSE, fontsize=9)
    if with_figure:
        page.draw_rect(fitz.Rect(72, 280, 400, 420), color=(0, 0, 0))
        for k in range(6):
            page.draw_line(fitz.Point(80 + 50 * k, 300), fitz.Point(80 + 50 * k, 410))
        page.insert_text((90, 330), "0-smooth", fontsize=7)
        page.insert_text((180, 360), "RMSE", fontsize=7)
        page.insert_text((72, 440), "Figure 1. Performance on smoothed datasets.", fontsize=9)
    page.insert_textbox(fitz.Rect(72, 460, 540, 520), "2. Results\n" + PROSE, fontsize=9)
    return doc.tobytes()


def test_a_table_is_read_with_its_rows_and_columns() -> None:
    doc = fitz.open(stream=_pdf(), filetype="pdf")
    layout = page_layout(doc[0], 1)
    assert len(layout.tables) == 1
    table = layout.tables[0]
    assert table.label == "1" and table.header == ["Dataset", "Metric", "SVM", "XGB", "GCN"]
    assert table.rows[1] == ["HIV", "AUC", "0.817", "0.823", "0.834"]
    assert "[Table 1 (p. 1) as a table. Columns: Dataset | Metric | SVM | XGB | GCN. Rows: BACE | AUC | 0.886" in layout.text
    assert layout.text.count("0.817") == 1  # the cells are not also left in the text as loose words


def test_figure_labels_are_left_out_and_the_caption_and_prose_kept() -> None:
    doc = fitz.open(stream=_pdf(), filetype="pdf")
    layout = page_layout(doc[0], 1)
    assert "0-smooth" not in layout.text and "RMSE" not in layout.text
    assert "Figure 1. Performance on smoothed datasets." in layout.text and layout.text.count(PROSE[:40]) == 3
    assert layout.figure_blocks_dropped >= 2


def test_a_sentence_mentioning_a_table_is_not_a_caption() -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Table 1 shows that tree models win on most datasets.", fontsize=9)
    assert page_layout(page, 1).tables == []


def test_without_a_figure_caption_nothing_inside_drawings_is_removed() -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(60, 60, 400, 200), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    page.insert_text((80, 120), "License and Terms", fontsize=11)
    assert "License and Terms" in page_layout(page, 1).text


def test_the_parser_keeps_the_tables_and_can_switch_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _pdf()
    parsed = parse_document(data, "paper.pdf")
    assert parsed.tables and parsed.tables[0]["header"][2] == "SVM"
    assert "as a table. Columns: Dataset | Metric" in parsed.full_text
    monkeypatch.setattr(settings, "PDF_LAYOUT_TABLES", False)
    plain = parse_document(data, "paper.pdf")
    assert plain.tables == [] and "as a table" not in plain.full_text
