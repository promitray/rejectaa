"""Page text with tables kept as tables and the text inside figures left out (PDF only).

A PDF's plain text runs a table's cells together ("BACE (1,513) AUC ROC 0.886 0.896 0.890 ...") so a reader cannot
tell which value belongs to which row and column, and it mixes the labels printed inside figures (axis ticks, legends,
SMILES strings drawn in a scheme) into the paragraphs around them. Here, per page:

1. Tables, anchored on their caption ("Table 2. ...", "Table 2 | ...", or "Table 2" on its own line):
   - the words below the caption (or above it, when nothing below is tabular), in the caption's column on a
     two-column page, are grouped into rows by height on the page;
   - the table ends at a caption, a row of running text, a large vertical gap, or a row that stops lining up with
     the table's columns (a figure or prose starts);
   - columns come from white space: the x-ranges that no word of any data row covers; every word goes to the
     column it overlaps; the first row is the header;
   - a ruled table PyMuPDF's `find_tables` finds inside that same area is used instead when it is complete.
   The table's text blocks are replaced by one line that keeps its structure:
       [Table 1 (p. 3) as a table. Columns: Dataset (No.) | Metric | SVM | ... Rows: BACE (1,513) | AUC ROC | 0.886 | ...; ...]
2. Figure text: short text blocks lying mostly inside an embedded image or a large vector drawing are left out:
   they are a figure's labels, not the paper's prose. Captions, headings and paragraphs are always kept.

Everything else is the page's plain text, block by block, exactly as `page.get_text()` gives it. Never raises: if
anything fails on a page, that page's plain text is used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)

_TABLE_CAPTION = re.compile(r"^\s*Table\s+(S?\d{1,2}[A-Za-z]?)\s*(?:[.:|]|\n|$)", re.IGNORECASE)
_ANY_CAPTION = re.compile(r"^\s*(?:Table|Fig(?:ure)?s?\.?|Scheme|Chart)\s+S?\d{1,2}[A-Za-z]?\s*(?:[.:|]|\n|$)", re.IGNORECASE)
_FIGURE_CAPTION = re.compile(r"^\s*(?:Fig(?:ure)?s?\.?|Scheme|Chart)\s+S?\d{1,2}[A-Za-z]?\s*(?:[.:|]|\n|$)", re.IGNORECASE)
_FURNITURE = re.compile(r"(?i)www\.|https?://|doi\.org|vol\.\s*\d|\|\s*https?")
MAX_ROWS = 80
MAX_COLS = 24
MAX_CELL_CHARS = 60
MAX_CELL_WORDS = 6  # the median cell of a table row has at most this many words
MIN_TABLE_ROWS = 3  # header + at least two rows
ROW_TOLERANCE = 0.45  # share of the word height within which two words are on one row
CELL_GAP = 0.55  # a gap wider than this share of the word height starts a new cell (for row detection)
WORD_SPACE = 0.4  # gaps up to this share of the word height are spaces inside a column (for the grid)
GAP_TOLERANCE = 0.2  # a column gap may be crossed by words of at most this share of the data rows
COLUMN_TOLERANCE = 1.8  # a cell lines up with a column within this many word heights
ALIGNED_SHARE = 0.6  # share of a row's cells that must line up with the table's columns
ROW_GAP_LIMIT = 3.0  # a vertical gap this many times the usual row gap ends the table
PROSE_WORDS = 14  # a row with this many words, mostly letters, in at most two cells is running text
FIGURE_OVERLAP = 0.6  # share of a text block inside a figure area for it to be the figure's text
FIGURE_MAX_WORDS = 25  # longer blocks are never treated as figure text
MIN_FIGURE_WIDTH = 0.15  # of the page width, for a vector drawing to count as a figure
MIN_FIGURE_HEIGHT = 50.0
MAX_FIGURE_AREA = 0.6  # of the page area: larger drawings are page decoration
TWO_COLUMN_SHARE = 0.6  # of the paragraph blocks narrower than 55 % of the page: a two-column page


@dataclass
class Table:
    label: str
    caption: str
    page: int
    header: list[str]
    rows: list[list[str]]
    source: str  # "words" | "ruled"

    def one_line(self) -> str:
        def cells(row: list[str]) -> str:
            return " | ".join(re.sub(r"\s+", " ", c or "").strip()[:MAX_CELL_CHARS] for c in row)

        body = "; ".join(cells(r) for r in self.rows)
        return f"[Table {self.label} (p. {self.page}) as a table. Columns: {cells(self.header)}. Rows: {body}]"

    def to_json(self) -> dict[str, Any]:
        return {"label": f"Table {self.label}", "caption": self.caption[:300], "page": self.page,
                "header": self.header, "rows": self.rows, "source": self.source}


@dataclass
class PageLayout:
    text: str
    tables: list[Table] = field(default_factory=list)
    figure_blocks_dropped: int = 0
    figure_chars_dropped: int = 0


# --------------------------------------------------------------------------- geometry

def _area(r: tuple[float, ...]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Share of a's area inside b."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = (x1 - x0) * (y1 - y0) if x1 > x0 and y1 > y0 else 0.0
    return inter / _area(a) if _area(a) else 0.0


def _is_prose(text: str) -> bool:
    words = text.split()
    letters = sum(c.isalpha() for c in text)
    return len(words) >= PROSE_WORDS and letters >= 0.6 * max(1, len(text.replace(" ", "")))


def _local_height(words: list[tuple]) -> float:
    heights = [w[3] - w[1] for w in words if w[3] > w[1]]
    return median(heights) if heights else 8.0


# --------------------------------------------------------------------------- rows

@dataclass
class _Row:
    cells: list[tuple[float, float, str]]  # (x0, x1, text)
    top: float
    bottom: float
    words: list[tuple] = field(default_factory=list)

    def centres(self) -> list[float]:
        return [(c[0] + c[1]) / 2 for c in self.cells]

    def text(self) -> str:
        return " ".join(c[2] for c in self.cells)


def _rows(words: list[tuple], height: float) -> list[_Row]:
    """Words (x0, y0, x1, y1, text, ...) grouped into rows by vertical centre, top to bottom, each row into cells."""
    groups: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        centre = (w[1] + w[3]) / 2
        if groups and abs(centre - sum((v[1] + v[3]) / 2 for v in groups[-1]) / len(groups[-1])) <= ROW_TOLERANCE * height:
            groups[-1].append(w)
        else:
            groups.append([w])
    out: list[_Row] = []
    for group in groups:
        group.sort(key=lambda w: w[0])
        cells: list[list[tuple]] = [[group[0]]]
        for w in group[1:]:
            if w[0] - cells[-1][-1][2] > CELL_GAP * height:
                cells.append([w])
            else:
                cells[-1].append(w)
        out.append(_Row(
            cells=[(c[0][0], c[-1][2], " ".join(w[4] for w in c)) for c in cells],
            top=min(w[1] for w in group), bottom=max(w[3] for w in group), words=list(group),
        ))
    return out


def _aligned(row: _Row, columns: list[float], height: float) -> float:
    if not row.cells:
        return 0.0
    hits = 0
    for x0, x1, _ in row.cells:
        centre = (x0 + x1) / 2
        if any(abs(centre - col) <= COLUMN_TOLERANCE * height or x0 <= col <= x1 for col in columns):
            hits += 1
    return hits / len(row.cells)


def _tabular(row: _Row) -> bool:
    """Cells are short (table cells), not lines of running text (two text columns look like a 2-column table)."""
    sizes = sorted(len(c[2].split()) for c in row.cells)
    return sizes[len(sizes) // 2] <= MAX_CELL_WORDS


def _table_rows(rows: list[_Row], height: float) -> list[_Row]:
    """The leading rows that form a table: short cells, consistent columns, no caption, no prose, no big gap."""
    if len(rows) < MIN_TABLE_ROWS:
        return []
    reference = max(rows[: min(6, len(rows))], key=lambda r: len(r.cells))
    if len(reference.cells) < 2 or not _tabular(reference):
        return []
    columns = reference.centres()
    gaps: list[float] = []
    kept: list[_Row] = []
    for row in rows[:MAX_ROWS]:
        text = row.text()
        if kept:
            gap = max(0.0, row.top - kept[-1].bottom)
            if gaps and gap > ROW_GAP_LIMIT * max(median(gaps), 0.3 * height):
                break
            gaps.append(gap)
        if _FURNITURE.search(text):  # a running header or footer printed next to the table
            continue
        if _ANY_CAPTION.match(text) or (_is_prose(text) and len(row.cells) <= 2) or not _tabular(row):
            break
        if row is not reference and len(row.cells) >= 2 and _aligned(row, columns, height) < ALIGNED_SHARE:
            break
        kept.append(row)
    return kept if len(kept) >= MIN_TABLE_ROWS and max(len(r.cells) for r in kept) >= 2 else []


def _columns(data: list[_Row], pad: float) -> list[list[float]]:
    """Column x-ranges from white space: an x-range at least `pad` wide that words of at most GAP_TOLERANCE of the
    data rows cover separates two columns (one long name spilling into a gap does not merge the whole table)."""
    words = [(w[0], w[2]) for r in data for w in r.words]
    if not words:
        return []
    left, right = min(w[0] for w in words), max(w[1] for w in words)
    step = 0.5
    n = int((right - left) / step) + 1
    coverage = [0] * n
    for row in data:
        covered = [False] * n
        for w in row.words:
            for k in range(int((w[0] - left) / step), min(n, int((w[2] - left) / step) + 1)):
                covered[k] = True
        for k in range(n):
            coverage[k] += covered[k]
    limit = GAP_TOLERANCE * len(data)
    columns: list[list[float]] = []
    k = 0
    while k < n:
        if coverage[k] <= limit:
            start = k
            while k < n and coverage[k] <= limit:
                k += 1
            if (k - start) * step < pad and columns:  # too narrow to be a column gap: a space inside a cell
                columns[-1][1] = left + k * step
            continue
        start = k
        while k < n and coverage[k] > limit:
            k += 1
        if columns and (left + start * step) - columns[-1][1] < pad:
            columns[-1][1] = left + k * step
        else:
            columns.append([left + start * step, left + k * step])
    return columns[:MAX_COLS]


def _grid(kept: list[_Row], height: float) -> tuple[list[str], list[list[str]]]:
    """Rows laid out on columns found from white space shared by the data rows (rows with a digit); the header's
    words may span several columns. Every word goes to the column it overlaps (or the nearest); the first row is the
    header."""
    data = [r for r in kept[1:] if any(ch.isdigit() for ch in r.text())] or kept[1:] or kept
    pad = WORD_SPACE * height
    columns = _columns(data, pad)

    def distance(col: list[float], centre: float) -> float:
        if col[0] - pad <= centre <= col[1] + pad:
            return 0.0
        return min(abs(centre - col[0]), abs(centre - col[1]))

    def place(row: _Row) -> list[str]:
        out = [""] * len(columns)
        for w in sorted(row.words, key=lambda w: w[0]):
            centre = (w[0] + w[2]) / 2
            j = min(range(len(columns)), key=lambda k: distance(columns[k], centre))
            out[j] = f"{out[j]} {w[4]}".strip()
        return out

    grid = [place(r) for r in kept]
    return grid[0], grid[1:]


# --------------------------------------------------------------------------- page parts

def _figure_areas(page: Any) -> list[tuple[float, float, float, float]]:
    rect = page.rect
    page_area = rect.width * rect.height
    areas: list[tuple[float, float, float, float]] = []
    try:
        for info in page.get_image_info():
            bbox = tuple(info.get("bbox") or ())
            if len(bbox) == 4 and _area(bbox) < MAX_FIGURE_AREA * page_area:
                areas.append(bbox)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        logger.debug("image info failed", exc_info=True)
    try:
        for cluster in page.cluster_drawings():
            box = (cluster.x0, cluster.y0, cluster.x1, cluster.y1)
            if (cluster.width >= MIN_FIGURE_WIDTH * rect.width and cluster.height >= MIN_FIGURE_HEIGHT
                    and _area(box) < MAX_FIGURE_AREA * page_area):
                areas.append(box)
    except Exception:  # noqa: BLE001
        logger.debug("drawing clusters failed", exc_info=True)
    return areas


def _two_column(page: Any, blocks: list[tuple]) -> bool:
    width = page.rect.width
    paragraphs = [b for b in blocks if len(b[4].split()) >= 30]
    if len(paragraphs) < 2:
        return False
    narrow = sum(1 for b in paragraphs if (b[2] - b[0]) < 0.55 * width)
    return narrow / len(paragraphs) >= TWO_COLUMN_SHARE


def _ruled(page: Any, region: tuple[float, float, float, float]) -> tuple[list[str], list[list[str]]] | None:
    """A ruled table PyMuPDF finds inside the region, when it is complete (most cells filled, short cells)."""
    import fitz

    try:
        found = page.find_tables(clip=fitz.Rect(region), strategy="lines").tables
    except Exception:  # noqa: BLE001
        return None
    best: list[list[str]] | None = None
    for t in found:
        if _overlap(tuple(t.bbox), region) < 0.8:  # a table reaching outside this area belongs to another caption
            continue
        rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in t.extract()]
        cells = [c for r in rows for c in r]
        filled = [c for c in cells if c]
        if not filled:
            continue
        short = sorted(len(c.split()) for c in filled)[len(filled) // 2] <= MAX_CELL_WORDS
        if len(rows) >= MIN_TABLE_ROWS and t.col_count >= 2 and len(filled) / len(cells) >= 0.6 and short:
            if best is None or len(cells) > sum(len(r) for r in best):
                best = rows
    return (best[0], best[1:]) if best else None


def _table_near(page: Any, caption: tuple, words: list[tuple], two_column: bool) -> tuple[list[_Row], tuple, float] | None:
    """The table below the caption (or above it): its rows, its region and its word height."""
    cx0, cy0, cx1, cy1 = caption[:4]
    width = page.rect.width
    if two_column and (cx1 - cx0) < 0.55 * width:  # a caption in one column: the table is in that column
        half = width / 2
        x0, x1 = (0.0, half + 4) if (cx0 + cx1) / 2 < half else (half - 4, width)
    else:
        x0, x1 = 0.0, width

    def inside(w: tuple) -> bool:
        return x0 <= (w[0] + w[2]) / 2 <= x1

    below = sorted((w for w in words if w[1] >= cy1 - 1 and inside(w)), key=lambda w: w[1])
    height = _local_height(below[:80])
    kept = _table_rows(_rows(below, height), height)
    if not kept:
        above = sorted((w for w in words if w[3] <= cy0 + 1 and inside(w)), key=lambda w: -w[3])
        height = _local_height(above[:80])
        upward = list(reversed(_rows(above, height)))  # nearest the caption first
        kept = list(reversed(_table_rows(upward, height)))
    if not kept:
        return None
    region = (min(c[0] for r in kept for c in r.cells) - 2, min(r.top for r in kept) - 2,
              max(c[1] for r in kept for c in r.cells) + 2, max(r.bottom for r in kept) + 2)
    return kept, region, height


# --------------------------------------------------------------------------- one page

def page_layout(page: Any, page_number: int) -> PageLayout:
    """The page's text with tables as one structured line each and figure text left out."""
    blocks = [b for b in page.get_text("blocks") if b[6] == 0]
    plain = "".join(b[4] for b in blocks)
    try:
        words = page.get_text("words")
        two_column = _two_column(page, blocks)
        tables: list[Table] = []
        regions: list[tuple[tuple, Table, int]] = []  # (region, table, index of its caption block)
        for i, b in enumerate(blocks):
            match = _TABLE_CAPTION.match(b[4])
            if not match:
                continue
            found = _table_near(page, b, words, two_column)
            if found is None:
                continue
            kept, region, height = found
            ruled = _ruled(page, region)
            header, rows = ruled if ruled else _grid(kept, height)
            table = Table(match.group(1), re.sub(r"\s+", " ", b[4]).strip(), page_number, header, rows,
                          "ruled" if ruled else "words")
            tables.append(table)
            regions.append((region, table, i))
        # figure text is left out only on a page that has a figure or scheme caption: a coloured box behind a heading
        # or a licence notice is page decoration, and its words are the paper's
        has_figure = any(_FIGURE_CAPTION.match(b[4]) for b in blocks)
        figures = _figure_areas(page) if has_figure else []
        out: list[str] = []
        dropped = dropped_chars = 0
        inserted: set[int] = set()
        for i, b in enumerate(blocks):
            box, text = tuple(b[:4]), b[4]
            caption = bool(_ANY_CAPTION.match(text))
            owner = next((r for r in regions if i != r[2] and _overlap(box, r[0]) >= 0.5), None)
            if owner is not None and not caption:
                if id(owner[1]) not in inserted and owner[2] > i:  # a table printed above its caption
                    out.append(owner[1].one_line() + "\n")
                    inserted.add(id(owner[1]))
                continue
            in_figure = any(_overlap(box, f) >= FIGURE_OVERLAP for f in figures)
            if in_figure and not caption and len(text.split()) <= FIGURE_MAX_WORDS and not _is_prose(text):
                dropped += 1
                dropped_chars += len(text)
                continue
            out.append(text)
            for _, table, caption_index in regions:
                if caption_index == i and id(table) not in inserted:
                    out.append(table.one_line() + "\n")
                    inserted.add(id(table))
        return PageLayout("".join(out), tables, dropped, dropped_chars)
    except Exception:  # noqa: BLE001 - the plain text is always a correct fallback
        logger.warning("Layout reading failed on page %s; using its plain text", page_number, exc_info=True)
        return PageLayout(plain)
