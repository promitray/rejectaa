"""A checklist for each section of a paper, decided by code from the text alone (no model).

What a careful handling editor looks for before reading for content: does the Introduction say what the paper
does, does the Methods name its software with versions, its replicates, its statistics, an ethics approval when
there are patients or animals, does every figure the text points to exist and show what the sentence says,
do the abstract's numbers appear in the paper, does the Discussion admit a limitation.

Precision comes before recall. A check only reports a fault it can show: when the text cannot settle a
question (a caption printed as an image, citations as superscripts the PDF lost) the item is left out rather
than marked missing. Every item quotes the paper. The checks are pure functions of the text, so the same file
always gets the same checklist.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from models import SectionCheck
from services import paper_profile as pp

MAX_QUOTE_CHARS = 300
MAX_ITEMS_OF_A_KIND = 3  # e.g. at most three overclaims or wrong numbers per section
MIN_TOPIC_OVERLAP = 3  # shared content words before a sentence counts as describing another figure
MIN_METHODS_WORDS = 150
MIN_INTRODUCTION_WORDS = 120
MIN_DISCUSSION_WORDS_FOR_CITATIONS = 300
MIN_REPEATED_SENTENCES = 3

RESEARCH_TYPES = (pp.RESEARCH_ARTICLE, pp.COMMUNICATION, pp.UNKNOWN)

MET, MISSING, ISSUE, INFO = "met", "missing", "issue", "info"
MAJOR, MINOR = "major", "minor"


@dataclass(frozen=True)
class PaperText:
    full_text: str  # the whole file as read, back matter included: statements often sit after the references
    abstract: str
    sections: dict[str, str]  # section key -> its whole text, in the order of the paper
    roles: dict[str, str]  # section key -> role
    article_type: str = pp.UNKNOWN
    titles: dict[str, str] = field(default_factory=dict)  # section key -> heading as printed


# --------------------------------------------------------------------------- text helpers

def _clean(text: str) -> str:
    """Words as a reader sees them: line-break hyphens joined, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", text)
    return re.sub(r"\s+", " ", text).strip()


_SENTENCE_START = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[“\"])")


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence of `text` around [start, end), at most MAX_QUOTE_CHARS, as a reader sees it."""
    while start < end and text[start].isspace():
        start += 1
    lo = 0
    for match in _SENTENCE_START.finditer(text, 0, start + 1):  # +1: the lookahead must see the first letter
        if match.end() <= start:
            lo = match.end()
    following = _SENTENCE_START.search(text, end)
    hi = following.start() if following else len(text)
    quote = _clean(text[lo:hi])
    # a sentence broken by a page that a table interrupts runs on into the table's cells: stop before them
    cells = re.search(r"(?:\S*\d\S*\s+){5,}", quote[end - lo :] if end - lo < len(quote) else "")
    if cells and cells.start() > 0:
        quote = quote[: end - lo + cells.start()].rstrip() + " …"
    if len(quote) <= MAX_QUOTE_CHARS:
        return quote
    middle = _clean(text[lo:start])  # keep the matched words in view
    cut = max(0, len(middle) - MAX_QUOTE_CHARS // 2)
    piece = quote[cut : cut + MAX_QUOTE_CHARS]
    return ("… " if cut else "") + piece.strip() + " …"


def _first(pattern: re.Pattern[str], text: str, unless: re.Pattern[str] | None = None) -> str:
    """The sentence of the first match, skipping sentences that `unless` matches."""
    for match in pattern.finditer(text):
        sentence = _sentence(text, match.start(), match.end())
        if unless is None or not unless.search(sentence):
            return sentence
    return ""


def _last(pattern: re.Pattern[str], text: str) -> str:
    matches = list(pattern.finditer(text))
    return _sentence(text, matches[-1].start(), matches[-1].end()) if matches else ""


def _words(text: str) -> int:
    return len(text.split())


_STOP = frozenset(
    "the and for with that this from were was are been have has had into onto over under than then also each "
    "which while where when there their these those such both other using used use shown show shows figure "
    "figures table tables panel results result data values value respectively here our we its can may per via "
    "between within after before during more most less least very well based".split()
)


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    return word[:6]


def _topic(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


def _item(key: str, label: str, status: str, detail: str = "", quote: str = "", severity: str | None = None, fix: str = "") -> SectionCheck:
    return SectionCheck(key=key, label=label, status=status, severity=severity if status in (MISSING, ISSUE) else None,
                        detail=detail, quote=quote, fix=fix)


# --------------------------------------------------------------------------- the paper's figures and tables

_KIND_WORD = r"(Fig(?:ure)?s?\.?|Tables?|Schemes?)"
_CAPTION_LINE = re.compile(rf"^\s*{_KIND_WORD}\s*(\d+)\s*(?:[:.|]|$|\s+(?=[A-Z]))")
# "Fig. 3", "Figs. 2 and 3", "Figures 2–4", "Schemes 7, 8", "Fig. 2a,b". A citation number glued on by the PDF
# ("Scheme 7.41", the superscript 41) still names Scheme 7.
_MENTION = re.compile(
    rf"\b{_KIND_WORD}\s*(\d+)[a-z]?((?:\s*(?:[-–—,]|and|&|to)\s*\d+[a-z]?(?![\d.]\d))*)"
)


def _numbers(first: str, rest: str) -> list[int]:
    numbers = [int(first)]
    for joint, number in re.findall(r"\s*([-–—,]|and|&|to)\s*(\d+)", rest):
        value = int(number)
        if joint in "-–—to" and joint != "," and value > numbers[-1] and value - numbers[-1] <= 20:
            numbers.extend(range(numbers[-1] + 1, value + 1))
        else:
            numbers.append(value)
    return numbers
_CONTINUES = re.compile(r"(?i)(?:\b(?:in|see|of|and|or|to|from|with|by|as|the|both|shown|cf\.?|e\.g\.|i\.e\.)|[,(\-–])\s*$")


def _kind(word: str) -> str:
    word = word.lower()
    return "Fig." if word.startswith("fig") else ("Table" if word.startswith("table") else "Scheme")


@dataclass
class _Figures:
    captions: dict[tuple[str, int], str]  # (kind, number) -> the caption's first sentence
    ambiguous: set[tuple[str, int]]  # numbers with two captions: not judged
    mentions: list[tuple[str, int, int, int]]  # (kind, number, start, end) in `text`
    text: str  # the body lines joined, references left out


def _figures(full_text: str) -> _Figures:
    from services.parser import _split_reference_blocks  # the parser's own split, so both agree on the body

    lines, _ = _split_reference_blocks(full_text.split("\n"))
    text = "\n".join(lines)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1

    captions: dict[tuple[str, int], str] = {}
    ambiguous: set[tuple[str, int]] = set()
    caption_starts: set[int] = set()
    for index, line in enumerate(lines):
        match = _CAPTION_LINE.match(line)
        if not match:
            continue
        previous = next((lines[j] for j in range(index - 1, -1, -1) if lines[j].strip()), "")
        if _CONTINUES.search(previous):
            continue  # "... shown in" + "Fig. 3. The next ...": a mention that wrapped onto a new line
        key = (_kind(match.group(1)), int(match.group(2)))
        caption_starts.add(offsets[index] + match.start(1))
        # the caption's first sentence, which may wrap over several lines
        rest = text[offsets[index] + match.end() : offsets[index] + match.end() + 600]
        # a caption's title ends at its first full stop, whatever follows (body text can run on after it)
        first = re.split(r"(?<=[a-z0-9)\]][.!?])\s+", _clean(rest), maxsplit=1)[0]
        if key in captions:
            ambiguous.add(key)
        captions[key] = first[:MAX_QUOTE_CHARS]

    mentions = [
        (_kind(m.group(1)), number, m.start(), m.end())
        for m in _MENTION.finditer(text)
        if m.start() not in caption_starts
        for number in _numbers(m.group(2), m.group(3) or "")
    ]
    return _Figures(captions, ambiguous, mentions, text)


def _figure_checks(figs: _Figures) -> list[SectionCheck]:
    out: list[SectionCheck] = []
    by_kind: dict[str, dict[int, str]] = {}
    for (kind, number), caption in figs.captions.items():
        by_kind.setdefault(kind, {})[number] = caption

    # 1. a sentence that describes another figure than the one it names
    wrong = 0
    flagged: set[tuple[str, int]] = set()
    meant: set[tuple[str, int]] = set()  # the figures those sentences describe
    for kind, number, start, end in figs.mentions:
        key = (kind, number)
        captions = by_kind.get(kind, {})
        if key not in figs.captions or key in figs.ambiguous or len(captions) < 2 or key in flagged:
            continue
        sentence = _sentence(figs.text, start, end)
        words = _topic(re.sub(rf"\b{_KIND_WORD}\s*\d+\w?", " ", sentence))
        if len(words) < 4:
            continue
        named = len(words & _topic(captions[number]))
        others = sorted(
            ((len(words & _topic(text)), other) for other, text in captions.items()
             if other != number and (kind, other) not in figs.ambiguous),
            reverse=True,
        )
        if not others:
            continue
        best, other = others[0]
        if best >= MIN_TOPIC_OVERLAP and named <= 1 and best >= named + MIN_TOPIC_OVERLAP and (len(others) == 1 or others[1][0] < best):
            flagged.add(key)
            meant.add((kind, other))
            wrong += 1
            if wrong <= MAX_ITEMS_OF_A_KIND:
                out.append(_item(
                    "figure_number", f"{kind} {number} may be the wrong number", ISSUE,
                    detail=(f"This sentence describes what the caption of {kind} {other} shows "
                            f"(“{captions[other][:120]}”), while {kind} {number} is “{captions[number][:120]}”."),
                    quote=sentence, severity=MINOR,
                    fix=f"Check whether the sentence should refer to {kind} {other}.",
                ))

    # (A figure the text points to without a caption in the text is not reported: tables and schemes are often
    # printed as images, so the text cannot show that the caption is missing.)

    # 2. captioned but never referred to (one that a wrongly numbered sentence describes is already reported)
    mentioned = {(k, n) for k, n, _, _ in figs.mentions} | meant
    unreferenced = [key for key in sorted(figs.captions) if key not in mentioned]
    for kind, number in unreferenced[:MAX_ITEMS_OF_A_KIND]:
        out.append(_item(
            "figure_unreferenced", f"{kind} {number} is never referred to", ISSUE,
            detail=f"{kind} {number} (“{figs.captions[(kind, number)][:120]}”) has a caption but the text never points to it.",
            severity=MINOR, fix=f"Refer to {kind} {number} where the text discusses it, or remove it.",
        ))

    # 3. first referred to out of order (Fig. 3 before Fig. 2)
    first_seen: dict[tuple[str, int], int] = {}
    for kind, number, start, _ in figs.mentions:
        first_seen.setdefault((kind, number), start)
    for kind in sorted(by_kind):
        order = sorted((pos, n) for (k, n), pos in first_seen.items() if k == kind)
        numbers = [n for _, n in order]
        for earlier, later in zip(numbers, numbers[1:]):
            if later < earlier and later in by_kind[kind] and earlier in by_kind[kind]:
                out.append(_item(
                    "figure_order", f"{kind} {earlier} is cited before {kind} {later}", ISSUE,
                    detail=f"Most journals number figures and tables in the order the text first cites them.",
                    severity=MINOR, fix=f"Renumber so that {kind} {later} is cited first, or cite it earlier.",
                ))
                break
    return out


# --------------------------------------------------------------------------- patterns

# The statement of aim: the plainest forms first. It usually closes the Introduction, so the last one is taken.
_OBJECTIVE = re.compile(
    r"(?im)(?:^|(?<=[.!?]\s)|(?<=\n))\s*(?:in this (?:work|paper|study|article|communication|review|letter|contribution|perspective)"
    r"|here,? we\b|herein,? we\b|the (?:aim|goal|objective|purpose) of this"
    r"|this (?:work|study|paper|review) (?:aims|seeks|presents|reports|describes|introduces))"
)
_OBJECTIVE_LOOSE = re.compile(
    r"(?i)\bwe (?:present|report|propose|describe|develop|demonstrate|introduce|investigate)\b"
    r"|\bin this (?:work|paper|study|article|communication|(?:mini-?)?review|account|perspective|feature article|tutorial)\b"
    r"|\b(?:are|is|will be) (?:discussed|reviewed|summari[sz]ed|covered|highlighted|surveyed)\b"
)
_DESCRIBES_OTHERS = re.compile(r"(?i)\b\w+ed\s+$")  # "proposed in this work": a participle, not the aim
_CITATION_MARK = re.compile(r"\[\d+(?:\s*[,–—\-–]\s*\d+)*\]|\b[A-Z][A-Za-z\-]+ et al\.?,? \(?(?:19|20)\d{2}")

_SOFTWARE = re.compile(
    r"\b(Python|MATLAB|SPSS|Stata|SAS (?:software|Institute|\d)|GraphPad Prism|OriginPro|OriginLab|Origin \d{4}|ImageJ"
    r"|Fiji|FlowJo|TensorFlow|PyTorch|Keras|scikit-learn|RDKit|Gaussian ?(?:09|16|03)|ORCA|VASP|LAMMPS|GROMACS"
    r"|AMBER|CHARMM|NAMD|AutoDock(?: Vina)?|PyMOL|ChemDraw|MestReNova|MestRe-?Nova|TopSpin|Microsoft Excel|JMP|Minitab"
    r"|Mathematica|COMSOL|ANSYS|CASTEP|Quantum ESPRESSO|CP2K|Materials Studio|OLEX2|SHELX[LTS]?|CrysAlis(?:Pro)?"
    r"|Cytoscape|DESeq2|edgeR|limma|Seurat|Bowtie2?|GATK|BLAST|Clustal[WXO]?|MEGA ?(?:X|\d+)|Schr[oö]dinger|Maestro"
    r"|R (?:software|version|package|Core Team|\d+\.\d+))\b"
)
_VERSION_AFTER = re.compile(
    r"^[\s,(]*(?:version|ver\.?|v)?\s*\d+(?:\.\d+)+|^[\s,(]*(?:version|ver\.?)\s*\d+|^\s+\d+\b(?![\]\d])"
    r"|^[\s,(]*R20\d\d[ab]?\b|^[\s,(]*(?:version\s*)?(?:X|XI|\d{4})\b"
)

_REPEATS = re.compile(
    r"(?i)\bn\s*=\s*\d+|\bin (?:triplicate|duplicate|quadruplicate)\b"
    r"|\b(?:\d+|two|three|four|five|six|ten) (?:independent|biological|technical|separate) "
    r"(?:replicates?|experiments?|runs?|repeats?|samples?|trials?|batches)"
    r"|\b(?:repeated|performed|conducted|run|measured) (?:\w+ )?(?:\d+|two|three|four|five|ten) times"
    r"|\bover (?:\d+|two|three|four|five|ten) (?:separate |independent |random )?(?:runs|trials|seeds|repeats|folds)"
    r"|\b(?:\d+|five|ten)-fold cross[- ]validation|\bacross (?:\d+|three|five|ten) (?:random )?seeds"
    r"|\bmean\s*(?:±|\+/-)\s*(?:SD|SEM|s\.d\.|s\.e\.m\.|standard)"
)
# significance levels only ("p < 0.05", "P = 0.003", "p-value"): "p = 0.5" is as often a probability as a P value
_P_VALUE = re.compile(r"(?<![A-Za-z])[pP]\s*(?:<|≤|<=|=)\s*0?\.0\d|\b[pP][- ]values?\b")
_STAT_TEST = re.compile(
    r"(?i)\b(?:t[- ]tests?|student'?s? t|anova|mann[- ]whitney|wilcoxon|chi[- ]?squared?|χ2|fisher'?s exact|kruskal"
    r"|tukey|bonferroni|holm|dunnett|log[- ]rank|cox (?:proportional|regression)|(?:linear|logistic|mixed[- ]effects?) "
    r"(?:regression|model)|pearson|spearman|permutation test|bootstrap)"
)
_ML = re.compile(
    r"(?i)\b(?:neural networks?|deep learning|machine learning|epochs?|learning rate|batch size|optimi[sz]er|"
    r"trained (?:on|for|using|with)|training (?:set|data|loss)|autoencoders?|transformers?|convolutional|LSTM|GRUs?|"
    r"random forests?|gradient boosting|cross[- ]validation|fine[- ]tun(?:ed|ing))\b"
)
_SEED = re.compile(r"(?i)\b(?:random )?seeds?\b|\bseeded\b|\brandom[_ ]state\b")
_HARDWARE = re.compile(r"\b(?:GPUs?|CPUs?|TPUs?|NVIDIA|GeForce|Tesla [A-Z]?\d+|RTX ?\d+|[AHV]100|Xeon|Ryzen|Apple M\d)\b")
_HUMANS_OR_ANIMALS = re.compile(
    r"(?i)\b(?:patients?|participants?|volunteers?|human (?:subjects?|samples?|tissues?|blood|serum|plasma)"
    r"|informed consent|mice|mouse|rats?|rabbits?|zebrafish|pigs?|primates?|in vivo|xenografts?)\b"
)
_ETHICS = re.compile(
    r"(?i)\b(?:ethic(?:s|al)? (?:committee|approval|board|review|statement|clearance)|institutional review board|\bIRB\b"
    r"|IACUC|animal (?:care and use|welfare|ethics)|approved by the|informed consent|declaration of helsinki|ARRIVE guidelines)"
)
_DATA_AVAILABLE = re.compile(
    r"(?i)\b(?:data (?:and code )?availability|code availability|available (?:up)?on (?:reasonable )?request"
    r"|(?:data|datasets?|code|source code|software|scripts?|models?|structures?|coordinates|weights)\b[^.]{0,60}?"
    r"(?:is|are|will be|have been|has been) (?:made )?(?:publicly |freely |openly )?(?:available|accessible|deposited)"
    r"|deposited (?:in|at|with|under)|github\.com|gitlab\.com|zenodo|figshare|dryad|osf\.io|accession (?:numbers?|codes?)"
    r"|CCDC \d|(?:provided|available|given|included) in the (?:supporting|supplementary|electronic supplementary) information)"
)
_SPREAD = re.compile(
    r"±|\+/-|\b(?:SD|SEM|s\.d\.|s\.e\.m\.|standard (?:deviation|error)|confidence intervals?|95% CI|error bars?|interquartile)\b"
)
_DECIMAL = re.compile(r"(?<![\w.])\d+\.\d+(?![\w.])")
_LIMITATION = re.compile(
    r"(?i)\b(?:limitations?|limited (?:by|to|in)|drawbacks?|shortcomings?|caveats?|weakness(?:es)?"
    r"|(?:is|are) not (?:yet )?(?:able|suitable|applicable|as advantageous)|cannot (?:yet )?be"
    r"|does not (?:yet )?(?:capture|account|address|generali[sz]e)|remains? (?:unclear|unknown|to be (?:seen|determined|explored|established))"
    r"|future (?:work|studies|research|investigations?)|further (?:work|studies|research|investigation) (?:is|are|will be) (?:needed|required|warranted)"
    r"|sub-?optimal|not (?:as )?advantageous)\b"
)
# "techniques for overcoming dataset limitations" is about solving a limitation, not admitting one
_LIMITATION_SOLVED = re.compile(
    r"(?i)\b(?:overcom\w*|address\w*|remed\w*|solv\w*|mitigat\w*|eliminat\w*|circumvent\w*|tackl\w*|resolv\w*|alleviat\w*)"
    r"\s+(?:\w+\s+){0,3}(?:limitations?|drawbacks?|shortcomings?|weakness(?:es)?)"
    r"|\b(?:prior|previous|existing|earlier|current|traditional|conventional)\s+(?:\w+\s+){0,3}(?:limitations?|drawbacks?|shortcomings?)"
)
_SPREAD_SIGN = re.compile(r"±|\+/-")
_OVERCLAIM = re.compile(
    r"(?i)\b(?:prove[sd]? that|proven to|we prove|proof that|unprecedented|for the first time|first[- ]ever|revolutioni[sz]e[sd]?|revolutionary"
    r"|breakthrough|paradigm[- ]shift(?:ing)?|definitive(?:ly)?|conclusive(?:ly)?|undoubtedly|unequivocal(?:ly)?"
    r"|indisputabl[ey]|by a (?:large|wide) margin|dramatically (?:improves?|enhances?|outperforms?|enhance|improve)"
    r"|game[- ]chang(?:er|ing))\b"
)
_MATHEMATICAL = re.compile(r"\b(?:Theorem|Lemma|Proposition|Corollary)\s+\d")
_ABSTRACT_NUMBER = re.compile(r"(?<![\w.\-])(?<!\d,)(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?!\w|,\d|\.\d)")
# a claim the authors make about their own work (a strong word about earlier work is not this paper's claim)
_OWN_CLAIM = re.compile(r"(?i)\b(?:we|our|us|this (?:work|study|paper|method|approach|system|model)|here|herein|the proposed)\b")


# --------------------------------------------------------------------------- per-role checks

def _introduction(text: str, paper: PaperText) -> list[SectionCheck]:
    out: list[SectionCheck] = []
    loose = [m for m in _OBJECTIVE_LOOSE.finditer(text) if not _DESCRIBES_OTHERS.search(text[max(0, m.start() - 25) : m.start()])]
    quote = _last(_OBJECTIVE, text) or (_sentence(text, loose[-1].start(), loose[-1].end()) if loose else "")
    if quote:
        out.append(_item("objective", "States what this paper does", MET, quote=quote))
    else:
        out.append(_item(
            "objective", "States what this paper does", MISSING, severity=MINOR,
            detail="No sentence in the Introduction says what this work does (“Here we …”, “In this paper …”).",
            fix="End the Introduction with one sentence that states the aim and the main result.",
        ))
    marks = len(_CITATION_MARK.findall(text))
    if marks:
        out.append(_item("citations", "Cites prior work", INFO, detail=f"{marks} citation marker(s) in {_words(text)} words."))
    if paper.article_type in (pp.RESEARCH_ARTICLE,) and _words(text) < MIN_INTRODUCTION_WORDS:
        out.append(_item(
            "length", "Introduction is very short", ISSUE, severity=MINOR,
            detail=f"{_words(text)} words: too few to set out the background and the gap this work fills.",
            fix="Add the background, what is not yet known, and why it matters.",
        ))
    return out


def _methods(text: str, paper: PaperText, searched: str) -> list[SectionCheck]:
    """`searched` is the Methods and the Results: many papers report training or replicates under Results."""
    out: list[SectionCheck] = []
    research = paper.article_type in RESEARCH_TYPES

    # software and versions
    named: dict[str, tuple[bool, str]] = {}
    for match in _SOFTWARE.finditer(searched):
        name = match.group(1)
        has_version = bool(re.search(r"\d", name)) or bool(_VERSION_AFTER.match(searched[match.end() : match.end() + 30]))
        name = re.sub(r"\s*(?:\d{2,4}|software|Institute|version|package|Core Team)$", "", name).strip() or name
        if name not in named or (has_version and not named[name][0]):
            named[name] = (has_version, _sentence(searched, match.start(), match.end()))
    if named:
        without = [n for n, (v, _) in named.items() if not v]
        if without:
            out.append(_item(
                "software_version", "Software versions", ISSUE, severity=MINOR,
                detail="Named without a version: " + ", ".join(without) + ".",
                quote=named[without[0]][1],
                fix="Give the version of each package (results can change between versions).",
            ))
        else:
            out.append(_item("software_version", "Software versions", MET, quote=next(iter(named.values()))[1]))

    computational = len({m.lower() for m in _ML.findall(searched)}) >= 3
    if research and computational:
        quote = _first(_HARDWARE, searched)
        out.append(_item("hardware", "Hardware stated", MET, quote=quote) if quote else _item(
            "hardware", "Hardware stated", MISSING, severity=MINOR,
            detail="The models are trained, but the hardware is not stated.",
            fix="State the GPU/CPU and the training time.",
        ))
        quote = _first(_SEED, searched)
        out.append(_item("seed", "Random seed or repeat runs", MET, quote=quote) if quote else _item(
            "seed", "Random seed or repeat runs", MISSING, severity=MINOR,
            detail="Training and sampling are random, but no random seed is given.",
            fix="Report the random seeds, or the spread over several seeds.",
        ))

    if research:
        quote = _first(_REPEATS, searched)
        if quote:
            out.append(_item("replicates", "Replicates or independent runs", MET, quote=quote))
        elif len(_DECIMAL.findall(searched)) >= 6:
            out.append(_item(
                "replicates", "Replicates or independent runs", MISSING, severity=MINOR,
                detail="Numbers are reported, but not how many independent replicates or runs they come from.",
                fix="State n (independent replicates or runs) for every measured value.",
            ))

        p_quote = _first(_P_VALUE, paper.full_text)
        if p_quote:
            test = _first(_STAT_TEST, paper.full_text)
            out.append(_item("statistics", "Statistical test named", MET, quote=test) if test else _item(
                "statistics", "Statistical test named", MISSING, severity=MAJOR,
                detail="P values are reported but no statistical test is named, so they cannot be interpreted.",
                quote=p_quote, fix="Name the test behind each P value, and say whether it is one- or two-sided.",
            ))

        subjects = {m.lower() for m in _HUMANS_OR_ANIMALS.findall(searched)}
        if len(subjects) >= 2 or "in vivo" in subjects:
            quote = _first(_ETHICS, paper.full_text)
            out.append(_item("ethics", "Ethics approval", MET, quote=quote) if quote else _item(
                "ethics", "Ethics approval", MISSING, severity=MAJOR,
                detail="The work involves " + ", ".join(sorted(subjects)[:3]) + ", but no ethics approval or consent statement was found.",
                fix="Name the committee that approved the study and the approval number; state how consent was obtained.",
            ))

        quote = _first(_DATA_AVAILABLE, paper.full_text)
        out.append(_item("data_availability", "Data / code availability", MET, quote=quote) if quote else _item(
            "data_availability", "Data / code availability", MISSING, severity=MINOR,
            detail="No statement says where the data" + (" or code" if computational else "") + " can be found.",
            fix="Add a Data availability statement (repository and identifier, or how to request the data).",
        ))

        if paper.article_type == pp.RESEARCH_ARTICLE and _words(text) < MIN_METHODS_WORDS:
            out.append(_item(
                "length", "Methods is very short", ISSUE, severity=MINOR,
                detail=f"{_words(text)} words: too few for another lab to repeat the work.",
                fix="Describe materials, procedures and analysis in enough detail to reproduce them.",
            ))
    return out


def _results(text: str, paper: PaperText, abstract_body: str) -> list[SectionCheck]:
    out: list[SectionCheck] = []
    if paper.article_type in RESEARCH_TYPES and len(_DECIMAL.findall(text)) >= 6:
        quote = _first(_SPREAD_SIGN, abstract_body) or _first(_SPREAD, abstract_body)
        out.append(_item("spread", "Values given with their spread", MET, quote=quote) if quote else _item(
            "spread", "Values given with their spread", MISSING, severity=MINOR,
            detail="Measured values are reported without a standard deviation, error or confidence interval.",
            fix="Report mean ± SD (or a confidence interval) and n for each measured value.",
        ))

    # numbers in the abstract that the paper never gives
    body_values = [float(v.replace(",", "")) for v in _ABSTRACT_NUMBER.findall(abstract_body)]
    missing: list[tuple[str, str]] = []
    abstract = _clean(paper.abstract)
    for match in _ABSTRACT_NUMBER.finditer(abstract):
        raw = match.group(1)
        value = float(raw.replace(",", ""))
        if "." not in raw and value < 100 or re.fullmatch(r"(?:19|20)\d{2}", raw):
            continue  # small counts and years are not results
        places = len(raw.split(".")[1]) if "." in raw else 0
        if any(round(v, places) == value for v in body_values):
            continue
        if raw not in [m for m, _ in missing]:
            missing.append((raw, _sentence(abstract, match.start(), match.end())))
    for raw, quote in missing[:MAX_ITEMS_OF_A_KIND]:
        out.append(_item(
            "abstract_number", f"Abstract value {raw} is not in the paper", ISSUE, severity=MINOR,
            detail=f"The abstract gives {raw}, but no section, table or caption of the paper does.",
            quote=f"(abstract) {quote}", fix=f"Show where {raw} comes from, or correct the abstract.",
        ))
    return out


def _discussion(text: str, paper: PaperText, searched: str, results: str, cites_elsewhere: bool, title: str) -> list[SectionCheck]:
    out: list[SectionCheck] = []
    if paper.article_type in RESEARCH_TYPES:
        quote = _first(_LIMITATION, searched, unless=_LIMITATION_SOLVED)
        out.append(_item("limitations", "Acknowledges a limitation", MET, quote=quote) if quote else _item(
            "limitations", "Acknowledges a limitation", MISSING, severity=MINOR,
            detail="No sentence in the Results or Discussion names a limitation of this work.",
            fix="Say where the method or the evidence stops: conditions not tested, assumptions, failure cases.",
        ))
    if (
        cites_elsewhere and "discussion" in (title or "discussion").lower()
        and _words(text) >= MIN_DISCUSSION_WORDS_FOR_CITATIONS and not _CITATION_MARK.search(text)
    ):
        out.append(_item(
            "discussion_citations", "Compares with published work", MISSING, severity=MINOR,
            detail="The Discussion cites no other work, so the results are not set against what is known.",
            fix="Compare the main results with the closest published results, with citations.",
        ))
    if results and text is not results:
        result_shingles = _shingles(results)
        repeated = [s for s in _SENTENCE_START.split(_clean(text)) if _words(s) >= 12 and _contained(s, result_shingles)]
        if len(repeated) >= MIN_REPEATED_SENTENCES:
            out.append(_item(
                "repeats_results", "Repeats the Results", ISSUE, severity=MINOR,
                detail=f"{len(repeated)} sentences of this section repeat the Results almost word for word.",
                quote=repeated[0][:MAX_QUOTE_CHARS],
                fix="Interpret the results here instead of restating them.",
            ))
    return out


def _shingles(text: str, size: int = 6) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _clean(text).lower())
    return {" ".join(words[i : i + size]) for i in range(max(0, len(words) - size + 1))}


def _contained(sentence: str, shingles: set[str]) -> bool:
    own = _shingles(sentence)
    return bool(own) and len(own & shingles) / len(own) >= 0.8


# What to write instead of a strong word: the plain measured wording, or the number that backs it.
_OVERCLAIM_FIX = (
    (("prove", "proof"), "Write “show” or “demonstrate”, and name the result that shows it (a value, a table or a figure)."),
    (("large", "wide"), "Give the margin itself (the gain over the best baseline, with its spread) and let it speak, instead of “{word}”."),
    (("dramatic",), "State the measured improvement (value and baseline) instead of “{word}”."),
    (("first",), "Say “to our knowledge” and cite the closest earlier work you checked, or drop the claim of being first."),
    (("unprecedented", "breakthrough", "revolution", "paradigm", "game"), "Replace “{word}” with what was achieved, in numbers, and the earlier best it is compared with."),
    (("definitive", "conclusive", "undoubted", "unequivocal", "indisputab"), "Say what the data show and under which conditions; leave the certainty to the reader."),
)


def _overclaim_fix(word: str) -> str:
    lowered = word.lower()
    for stems, text in _OVERCLAIM_FIX:
        if any(stem in lowered for stem in stems):
            return text.format(word=word)
    return f"Reword “{word}” as what the data show, or add the evidence that establishes it."


def _overclaims(text: str, paper: PaperText) -> list[SectionCheck]:
    out: list[SectionCheck] = []
    mathematical = bool(_MATHEMATICAL.search(paper.full_text))
    seen: set[str] = set()
    for match in _OVERCLAIM.finditer(text):
        word = match.group(0).lower()
        if mathematical and ("prov" in word or "proof" in word):
            continue
        stem = word[:5]
        if stem in seen:
            continue
        sentence = _sentence(text, match.start(), match.end())
        if not _OWN_CLAIM.search(sentence):
            continue  # about earlier work, not this paper's claim
        seen.add(stem)
        out.append(_item(
            "overclaim", f"Strong claim: “{match.group(0)}”", ISSUE, severity=MINOR,
            detail="Editors and reviewers read this word as a claim that the evidence must fully carry.",
            quote=sentence,
            fix=_overclaim_fix(match.group(0)),
        ))
        if len(out) >= MAX_ITEMS_OF_A_KIND:
            break
    return out


# --------------------------------------------------------------------------- putting it together

def _target(order: list[str], roles: dict[str, str], *wanted: str) -> str | None:
    for role in wanted:
        for name in order:
            if roles.get(name) == role:
                return name
    return order[0] if order else None


def check_sections(paper: PaperText) -> dict[str, list[SectionCheck]]:
    """Each section's checklist, keyed by section. Sections with nothing to report get an empty list."""
    order = list(paper.sections)
    result: dict[str, list[SectionCheck]] = {name: [] for name in order}
    if not order or paper.article_type == pp.EDITORIAL:
        return result
    roles = paper.roles
    by_role: Callable[[str], list[str]] = lambda role: [n for n in order if roles.get(n) == role]
    research = paper.article_type in RESEARCH_TYPES
    texts = {name: paper.sections[name] for name in order}
    results_text = " ".join(texts[n] for n in by_role(pp.ROLE_RESULTS))
    methods_text = " ".join(texts[n] for n in by_role(pp.ROLE_METHODS))
    body_text = " ".join(texts.values())
    figs = _figures(paper.full_text)

    def add(name: str | None, items: list[SectionCheck]) -> None:
        if name is not None and items:
            result[name].extend(items)

    intro = _target(order, roles, pp.ROLE_INTRODUCTION)
    if intro is not None and roles.get(intro) == pp.ROLE_INTRODUCTION:
        add(intro, _introduction(texts[intro], paper))

    if research:
        methods_home = _target(order, roles, pp.ROLE_METHODS, pp.ROLE_RESULTS)
        if methods_home is not None and (methods_text or results_text):
            add(methods_home, _methods(methods_text or texts[methods_home], paper, f"{methods_text} {results_text}"))
        results_home = _target(order, roles, pp.ROLE_RESULTS, pp.ROLE_DISCUSSION)
        if results_home is not None and results_text:
            captions = " ".join(figs.captions.values())
            add(results_home, _results(results_text, paper, f"{body_text} {captions}"))
        discussion = by_role(pp.ROLE_DISCUSSION)
        discussion_home = discussion[0] if discussion else (by_role(pp.ROLE_RESULTS) or [None])[0]
        if discussion_home is not None:
            own = texts[discussion_home]
            searched = " ".join(texts[n] for n in by_role(pp.ROLE_DISCUSSION) + by_role(pp.ROLE_RESULTS))
            cites_elsewhere = len(_CITATION_MARK.findall(body_text)) >= 5
            add(discussion_home, _discussion(
                own, paper, searched, results_text if discussion else "", cites_elsewhere,
                paper.titles.get(discussion_home, discussion_home),
            ))

    # figures and tables: each item goes to the section that holds the sentence it quotes
    fallback = _target(order, roles, pp.ROLE_RESULTS, pp.ROLE_THEMATIC, pp.ROLE_DISCUSSION)
    for item in _figure_checks(figs):
        add((_section_of_quote(item.quote, texts) if item.quote else None) or fallback, [item])

    # strong claims, where they are made (the Methods only describe, so they are left out)
    for name in order:
        if roles.get(name) != pp.ROLE_METHODS:
            add(name, _overclaims(texts[name], paper))
    return result


def _section_of_quote(quote: str, sections: dict[str, str]) -> str | None:
    probe = re.sub(r"[^a-z0-9]+", "", quote.lower())[:40]
    if len(probe) < 20:
        return None
    for name, text in sections.items():
        if probe in re.sub(r"[^a-z0-9]+", "", text.lower()):
            return name
    return None
