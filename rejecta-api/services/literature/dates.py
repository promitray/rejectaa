"""The uploaded paper's date: the cut-off before which other work counts as prior work.

Order, most precise first:
1. the arXiv stamp the file carries ("arXiv:2306.17702v1 [cs.LG] 30 Jun 2023")  -> that day;
2. an arXiv id without a stamp ("arXiv:2306.17702")                              -> the 1st of that month;
3. the publication record (Crossref, services.integrity) when the paper is published -> 1 Jan of that year;
4. a year in a venue statement ("The ICML 2023 Workshop on ...")                  -> 1 Jan of that year;
5. today.
A year or month alone is placed at its START, so no later work can ever count as evidence against the paper.

`concurrent_since` gives the start of the concurrent-work window: work within CONCURRENT_MONTHS before the paper's
date is labelled concurrent (weak weight), not prior.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from typing import Any

from services.literature.types import PaperDate

CONCURRENT_MONTHS = 6
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}
_ARXIV_STAMP = re.compile(r"arXiv:\s*(\d{2})(\d{2})\.\d{4,5}(?:v\d+)?(?:\s*\[[^\]]{2,20}\])?\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})")
_ARXIV_ID = re.compile(r"arXiv:\s*(\d{2})(\d{2})\.\d{4,5}")
_YEAR = re.compile(r"\b(19[89]\d|20\d{2})\b")


def resolve_paper_date(
    venue_statements: Iterable[str] = (),
    publication: Any = None,
    front_text: str = "",
    today: dt.date | None = None,
) -> PaperDate:
    """The paper's date and where it came from (see the module docstring for the order)."""
    today = today or dt.date.today()
    texts = [*venue_statements, front_text or ""]
    for text in texts:
        stamp = _ARXIV_STAMP.search(text)
        if stamp and stamp.group(4).lower()[:3] in _MONTHS:
            try:
                date = dt.date(int(stamp.group(5)), _MONTHS[stamp.group(4).lower()[:3]], int(stamp.group(3)))
            except ValueError:
                date = None
            if date is not None and date <= today:
                return PaperDate(date, "arxiv_stamp", "day", stamp.group(0))
    for text in texts:
        found = _ARXIV_ID.search(text)
        if found:
            year, month = 2000 + int(found.group(1)), int(found.group(2))
            if 1 <= month <= 12 and dt.date(year, month, 1) <= today:
                return PaperDate(dt.date(year, month, 1), "arxiv_id", "month", found.group(0))
    status = getattr(publication, "status", None)
    year = getattr(publication, "year", None)
    if status in ("published", "corrected", "retracted", "expression_of_concern") and isinstance(year, int) and year <= today.year:
        return PaperDate(dt.date(year, 1, 1), "publication_record", "year", f"{getattr(publication, 'journal', '') or ''} ({year})".strip())
    for text in venue_statements:
        years = [int(y) for y in _YEAR.findall(text) if int(y) <= today.year]
        if years:
            return PaperDate(dt.date(min(years), 1, 1), "venue_statement", "year", text[:200])
    return PaperDate(today, "today", "day", "")


def concurrent_since(paper_date: PaperDate) -> dt.date:
    """The first day of the concurrent window: CONCURRENT_MONTHS before the paper's date."""
    d = paper_date.date
    month = d.month - CONCURRENT_MONTHS
    year = d.year
    while month < 1:
        month += 12
        year -= 1
    day = min(d.day, 28)
    return dt.date(year, month, day)


def classify_by_date(work_date: dt.date | None, work_year: int | None, paper_date: PaperDate) -> str:
    """"prior", "concurrent", "later" or "undated" for a candidate work. A work known only by its year counts from
    the END of that year (the cautious reading: it might be from December), so it is never wrongly prior."""
    if work_date is None and work_year is None:
        return "undated"
    if work_date is None:
        work_date = dt.date(work_year, 12, 31)  # type: ignore[arg-type]
    if work_date >= paper_date.date:
        return "later"
    if work_date >= concurrent_since(paper_date):
        return "concurrent"
    return "prior"
