"""Ranking statements in the text checked against the numbers of the table they rest on.

"X outperforms all baselines on D", "X achieves the best AUC on D", "X beats Y on D": code finds the table (the one the
sentence or its paragraph cites, else the only table with both names), reads the values, takes the direction of the
metric from the row, the column headers, the sentence or the caption (RMSE: lower is better; AUC: higher), and
reports a mismatch with both quotations. Field-agnostic: models, methods, catalysts or conditions, as long as the
table names them. Bold type is not in the extracted text, so "best results in bold" cannot be checked here."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TABLE = re.compile(r"\[Table (\S+?) \(p\. (\d+)\) as a table\. Columns: (.*?)\. Rows: (.*?)\]", re.S)
_NUMBER = re.compile(r"[-−]?\d*\.?\d+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_LOWER = re.compile(r"(?i)\b(rmse|mae|mse|mape|errors?|loss|perplexity|latency|runtime|wer|cer|nll)\b")
_HIGHER = re.compile(r"(?i)\b(auc|roc|prc|auprc|accuracy|acc|r2|r\^2|r²|f1|precision|recall|pearson|spearman|"
                     r"correlation|bleu|rouge|map|ndcg|yield|top-?\d+)\b")
_WIN = re.compile(
    r"(?i)\b(?P<verb>outperform(?:s|ed)?|beat(?:s)?|surpass(?:es|ed)?|(?:is|was|are|were|performs?|performed) "
    r"(?:the )?best|achiev(?:es|ed) the (?P<a>best|highest|lowest)|(?:gives?|gave|has|have|had|obtains?|obtained|"
    r"yields?|yielded|reaches|reached) the (?P<b>best|highest|lowest))\b")
_LOSE = re.compile(r"(?i)\b(?P<verb>underperform(?:s|ed)?|(?:is|was|are|were) outperformed by)\b")
_ALL = re.compile(r"(?i)\b(all|every|each|any)\b(?: (?:the )?other)?|\bother (?:models|methods|baselines)\b")
_ALL_ROWS = re.compile(r"(?i)\b(?:on|across|for|in) (?:all|every|each)(?: of the)? (?:\w+ )?(?:datasets?|tasks?|benchmarks?|targets?)\b")
_FRACTION = re.compile(r"(?i)\b\d+\s*(?:of|/|out of)\s*\d+\b")


@dataclass
class Table:
    label: str
    page: int
    header: list[str]
    rows: list[list[str]]
    caption: str = ""
    models: list[int] = field(default_factory=list)  # numeric columns

    def value(self, row: list[str], col: int) -> float | None:
        if col >= len(row):
            return None
        m = _NUMBER.search(row[col].replace(",", ""))
        return float(m.group(0).replace("−", "-")) if m else None


def _name(cell: str) -> str:
    """'BACE (1,513)' -> 'BACE'."""
    return re.sub(r"\s*\(.*?\)\s*", " ", cell).strip(" *")


def parse_tables(text: str, captions: dict[str, str] | None = None) -> list[Table]:
    out = []
    for m in _TABLE.finditer(text or ""):
        header = [c.strip() for c in m.group(3).split(" | ")]
        rows = [[c.strip() for c in r.split(" | ")] for r in m.group(4).split("; ") if r.strip()]
        rows = [r for r in rows if len(r) >= 2]
        if len(header) < 3 or not rows:
            continue
        t = Table(m.group(1), int(m.group(2)), header, rows, (captions or {}).get(f"Table {m.group(1)}", ""))
        t.models = [j for j in range(1, len(header))
                    if sum(t.value(r, j) is not None for r in rows) >= max(1, len(rows) // 2 + len(rows) % 2)]
        if len(t.models) >= 2:
            out.append(t)
    return out


def _pattern(name: str) -> re.Pattern[str]:
    flags = re.I if len(name) > 4 else 0
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", flags)


def _found(names: list[str], text: str) -> list[tuple[int, int]]:
    """(position, index) of each name found in the text, longest names first so 'GCN-2' is not read as 'GCN'."""
    out, taken = [], []
    for i in sorted(range(len(names)), key=lambda i: -len(names[i])):
        if not names[i]:
            continue
        for m in _pattern(names[i]).finditer(text):
            if not any(a <= m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                out.append((m.start(), i))
                break
    return sorted(out)


def _direction(t: Table, row: list[str], sentence: str) -> tuple[str, str] | None:
    """('lower' | 'higher', the metric word) from the row's own cells, the column headers, the sentence, the caption."""
    non_model = [row[j] for j in range(len(row)) if j not in t.models]
    for text in (" ".join(non_model), " ".join(t.header[j] for j in range(len(t.header)) if j not in t.models),
                 sentence, t.caption):
        lo, hi = _LOWER.search(text), _HIGHER.search(text)
        if lo and not hi:
            return "lower", lo.group(0)
        if hi and not lo:
            return "higher", hi.group(0)
    return None


def _cells(t: Table, row: list[str]) -> str:
    return " | ".join(row)


def _check(sentence: str, section: str, t: Table) -> list[dict[str, Any]]:
    models = [t.header[j] for j in t.models]
    rows = [_name(r[0]) for r in t.rows]
    win, lose = _WIN.search(sentence), _LOSE.search(sentence)
    match = win or lose
    if not match or _FRACTION.search(sentence):
        return []  # "on 10 of 14 datasets" is a count, compared elsewhere
    found = _found(models, sentence)
    before = [models[i] for p, i in found if p < match.start()]
    after = [models[i] for p, i in found if p > match.end()]
    if not before:
        return []
    explicit = (win and (win.group("a") or win.group("b")) or "").lower()
    if lose:
        winners, losers = after, before
        if not winners:
            return []
    else:
        winners = before
        rest = sentence[match.end():]
        losers = after or ([m for m in models if m not in winners]
                           if (_ALL.search(rest) or "best" in match.group(0).lower() or explicit) else [])
    losers = [m for m in losers if m not in winners]
    if not losers:
        return []
    rows_named = [rows[i] for _, i in _found(rows, sentence)]
    if not rows_named and _ALL_ROWS.search(sentence):
        rows_named = rows
    out = []
    for name in rows_named:
        row = t.rows[rows.index(name)]
        if explicit in ("highest", "lowest"):
            way, metric = ("higher" if explicit == "highest" else "lower"), explicit
        else:
            got = _direction(t, row, sentence)
            if got is None:
                continue
            way, metric = got
        for w in winners:
            wv = t.value(row, t.header.index(w))
            if wv is None:
                continue
            better = [(o, ov) for o in losers if (ov := t.value(row, t.header.index(o))) is not None
                      and (ov > wv if way == "higher" else ov < wv)]
            if better:
                o, ov = max(better, key=lambda x: x[1]) if way == "higher" else min(better, key=lambda x: x[1])
                out.append({
                    "message": (f"The text ranks {w} above {', '.join(losers) if len(losers) <= 3 else 'the others'} "
                                f"on {name}, but Table {t.label} gives {w} {_fmt(wv)} and {o} {_fmt(ov)} "
                                f"({metric}: {way} is better)."),
                    "quotes": [{"quote": sentence.strip(), "section": section, "values": w},
                               {"quote": _cells(t, row), "section": "", "values": f"{w} {_fmt(wv)}; {o} {_fmt(ov)}",
                                "page": t.page}],
                })
    return out


def _fmt(v: float) -> str:
    return f"{v:g}"


def ranking_findings(texts: list[tuple[str, str]], figures: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """`texts`: (section label, text) in reading order. Returns findings as plain dicts: message and two quotations."""
    captions = {f.get("label", ""): f.get("caption", "") for f in figures or []}
    tables = [t for _, text in texts for t in parse_tables(text, captions)]
    if not tables:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section, text in texts:
        for paragraph in re.split(r"\n\s*\n", text or ""):
            cited = {m.group(1) for m in re.finditer(r"(?i)\btables?\s*(\w+)", paragraph)}
            for sentence in _SENTENCE.split(paragraph):
                if sentence.lstrip().startswith("[") or not (_WIN.search(sentence) or _LOSE.search(sentence)):
                    continue
                own = {m.group(1) for m in re.finditer(r"(?i)\btables?\s*(\w+)", sentence)}
                pool = [t for t in tables if t.label in (own or cited)]
                if not pool:  # the only table that has the names
                    pool = [t for t in tables if _found([t.header[j] for j in t.models], sentence)
                            and _found([_name(r[0]) for r in t.rows], sentence)]
                    pool = pool if len(pool) == 1 else []
                for t in pool:
                    for f in _check(sentence, section, t):
                        if f["message"] not in seen:
                            seen.add(f["message"])
                            out.append(f)
    return out
