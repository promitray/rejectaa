"""services/rebuttal/grounding.py
Deterministic fabrication guard for drafted rebuttal text.

The model is told not to invent findings, but prompts alone are not enough: with an
empty evidence box it still wrote claims such as "this combination has not been
previously reported". This module checks every drafted sentence against what the
author actually supplied (the manuscript context) and removes unsupported
claims of three kinds:

- priority / novelty   ("not previously reported", "for the first time")
- superiority          ("outperforms", "significantly faster")
- application / impact ("potential applications", "paves the way")

It is domain-agnostic: it looks at claim wording, never at field-specific terms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Claim vocabularies
# ---------------------------------------------------------------------------
_PRIORITY_PATTERNS = [
    r"\b(?:has|have|had)\s+(?:not|never)\s+(?:yet\s+)?been\s+(?:previously\s+)?(?:reported|demonstrated|described|explored|shown|achieved)\b",
    r"\bnot\s+(?:yet\s+)?(?:been\s+)?previously\s+(?:reported|demonstrated|described|explored|shown)\b",
    r"\bfor\s+the\s+first\s+time\b",
    r"\bfirst\s+(?:report|demonstration|example|study|time|of\s+its\s+kind)\b",
    r"\bnever\s+(?:before\s+)?(?:been\s+)?(?:reported|demonstrated|achieved|shown)\b",
    r"\bunprecedented\b",
    r"\bno\s+prior\s+(?:work|report|study|studies)\b",
    r"\bto\s+the\s+best\s+of\s+our\s+knowledge\b",
]

_SUPERIORITY_PATTERNS = [
    r"\boutperform(?:s|ed|ing)?\b",
    r"\b(?:significantly|considerably|substantially|markedly)\s+(?:faster|better|higher|lower|improved|more|less|superior)\b",
    r"\bsuperior\s+to\b",
    r"\bstate[-\s]of[-\s]the[-\s]art\b",
    r"\bbetter\s+than\s+(?:existing|previous|all)\b",
]

_IMPACT_PATTERNS = [
    r"\b(?:offers?|opens?)\s+(?:\w+\s+){0,3}(?:potential\s+)?applications?\b",
    r"\bpotential\s+applications?\b",
    r"\bpaves?\s+the\s+way\b",
    r"\benhanced\s+control\s+over\b",
    r"\bbroad(?:er)?\s+(?:impact|applicability)\b",
    r"\bwill\s+(?:enable|revolutionize|transform)\b",
]

# Soft value / significance claims: the usual way a draft answers "insufficient novelty"
# without evidence ("offers unique insights", "adds a valuable dimension").
_VALUE_PATTERNS = [
    # In a rebuttal, "unique" or "valuable" about the authors' own work is a claim whatever
    # noun follows ("unique degradation mechanisms", "valuable analytical insights"), so
    # these match the adjective itself rather than a fixed list of nouns.
    r"\bunique(?:ly)?\b(?!\s+(?:identifiers?|ids?|codes?|keys?|to\s+each))",
    r"\bvaluable\b",
    r"\binterdisciplinary\s+(?:nature|appeal|relevance|scope)\b",
    # Invented reasons for a venue ("aligns with the readership of X").
    r"\b(?:aligns?|aligned|fits?|suits?|suited|matches?|appeals?)\s+(?:well\s+|closely\s+|more\s+closely\s+)?(?:with\s+|to\s+)?(?:the\s+)?(?:[\w\-]+\s+){0,3}(?:readership|audience|aims\s+and\s+scope)\b",
    r"\b(?:adds?|provides?|offers?|brings?|yields?|delivers?)\s+(?:\w+\s+){0,2}(?:new|deeper|important|valuable|fresh|significant|novel|key)\s+(?:dimension|understanding|insights?|perspectives?|knowledge)\b",
    # Attributive "novel" ("a novel configuration", "our novel approach") is a claim about the
    # work whatever the noun. Predicative use ("is not sufficiently novel") is an
    # acknowledgement and does not match.
    r"\b(?:a|an|this|our|the|its|their)\s+(?:[\w\-]+\s+)?novel\s+[a-z]",
    r"\bsignificant\s+(?:advances?|contributions?|step|progress|improvements?|breakthrough)\b",
    r"\b(?:potential|broad|wide|significant|considerable|important|high)\s+impact\b",
    r"\bof\s+(?:broad|wide|general|great|considerable|significant)\s+interest\b",
    r"\b(?:wide|wider|broad|broader)\s+(?:audience|readership)\b",
    r"\badvances?\s+the\s+(?:field|state\s+of\s+knowledge|understanding)\b",
    r"\bfills?\s+(?:a|an|the)\s+(?:\w+\s+)?gap\b",
    r"\badds?\s+(?:\w+\s+)?value\b",
    r"\b(?:distinct|distinctive|meaningful|substantial|notable|important|key)\s+(?:contributions?|advances?|addition)\b",
    r"\b(?:a|an|the)\s+(?:new|fresh)\s+(?:combination|perspective|angle|direction)\b",
    r"\bwell[-\s]received\b",
    r"\breach\s+(?:a|an|the)\s+(?:[\w\-]+\s+){0,3}(?:audience|readership)\b",
    # Practical / applied value the evidence never mentions.
    r"\b(?:practical|real[-\s]world|industrial|commercial|clinical|biomedical|technological)\s+(?:applications?|implications?|relevance|utility|uses?|value)\b",
]

# Work the authors never said they did ("we have conducted stability tests",
# "based on preliminary optimization studies"). Only the author's evidence can
# establish that an experiment, analysis or check exists.
_COMPLETED_WORK_PATTERNS = [
    r"\bwe\s+(?:have\s+)?(?:already\s+)?(?:conducted|performed|carried\s+out|ran|measured|tested|verified|confirmed|validated|repeated|assessed|evaluated|analy[sz]ed|checked|optimi[sz]ed|surveyed|interviewed|recruited|collected)\b",
    r"\b(?:preliminary|previous|prior|our|initial|internal|additional|further)\s+(?:optimi[sz]ation\s+|stability\s+|control\s+|pilot\s+)?(?:studies|study|experiments?|tests?|trials?|assessments?|measurements?|screening|data|analys[ie]s|results)\s+(?:showed|show|shows|indicated|indicate|indicates|revealed|reveal|reveals|confirmed|confirm|confirms|suggested|suggest|suggests|demonstrated|demonstrate)\b",
    r"\bbased\s+on\s+(?:preliminary|prior|previous|our|initial|internal|extensive)\s+(?:optimi[sz]ation\s+|pilot\s+)?(?:studies|study|experiments?|work|tests?|screening|data|analys[ie]s|results)\b",
    r"\b(?:indicated|showed|revealed|confirmed|found)\s+no\s+(?:significant\s+|measurable\s+|detectable\s+)?(?:degradation|change|difference|effect|loss|decomposition|bias|toxicity)\b",
    # Invented rationale for a choice the authors made ("90 C was chosen to maximise yield").
    r"\b(?:was|were)\s+(?:chosen|selected|determined|based\s+on)\b",
]

_CLAIM_KINDS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("priority", [re.compile(p, re.IGNORECASE) for p in _PRIORITY_PATTERNS]),
    ("superiority", [re.compile(p, re.IGNORECASE) for p in _SUPERIORITY_PATTERNS]),
    ("impact", [re.compile(p, re.IGNORECASE) for p in _IMPACT_PATTERNS]),
    ("value", [re.compile(p, re.IGNORECASE) for p in _VALUE_PATTERNS]),
    ("completed_work", [re.compile(p, re.IGNORECASE) for p in _COMPLETED_WORK_PATTERNS]),
]

# Function words only. Claim words ("unique", "valuable", "conducted") are deliberately
# NOT here: a claim is supported only if the author's evidence uses those words too.
_STOPWORDS = frozenset(
    """
    this that these those with from have has had been being were was are will would
    could should their there which while where when also into than then them they
    such only more most some other about over under between within without upon
    our we the and for not but can may might shall each both either neither very
    believe believes think understand acknowledge appreciate reviewer reviewers
    manuscript paper work study
    """.split()
)

# Prefix length used as a light stem, so "authenticated" matches "authenticity"
# and "lines" matches "line" without a stemming library.
_STEM_CHARS = 5

# Abbreviations that end in "." but do not end a sentence.
_ABBREVIATIONS = ("e.g.", "i.e.", "et al.", "fig.", "figs.", "eq.", "eqs.", "vs.", "cf.", "ref.", "refs.", "no.")

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)")
_NUMBER_REF_WORDS = (
    "section", "table", "figure", "fig", "eq", "equation", "scheme", "reviewer",
    "comment", "point", "supplementary", "appendix", "step", "page", "line", "ref", "r",
)


@dataclass
class RemovedSentence:
    """A sentence taken out because it carried an unsupported claim."""

    index: int  # position in `GroundingResult.sentences`
    sentence: str
    kind: str
    snippet: str


@dataclass
class GroundingResult:
    """Outcome of grounding one drafted response."""

    text: str
    flags: List[str] = field(default_factory=list)
    stripped: bool = False
    # The response split into sentences, and which of them were removed, so the caller
    # can try to repair a removed sentence (drop only the claim, keep the rest of it).
    sentences: List[str] = field(default_factory=list)
    removed: List[RemovedSentence] = field(default_factory=list)


def unsupported_claim_flag(kind: str, snippet: str) -> str:
    return f"unsupported_claim ({kind}): '{snippet}' removed, no supporting evidence provided"


def split_sentences(text: str) -> List[str]:
    """Split into sentences without breaking on common abbreviations."""
    pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z\[\"'(])", text.strip())
    sentences: List[str] = []
    for piece in pieces:
        if sentences and sentences[-1].lower().endswith(_ABBREVIATIONS):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return [s for s in sentences if s.strip()]


def _content_words(text: str) -> set[str]:
    """Lower-cased content-word stems (hyphenated words split into parts)."""
    stems: set[str] = set()
    for word in re.findall(r"[a-z][a-z\-]{2,}", text.lower()):
        for part in word.split("-"):
            if len(part) >= 4 and part not in _STOPWORDS:
                stems.add(part.rstrip("s")[:_STEM_CHARS] if len(part) > 4 else part)
    return stems


def _claim_kind(sentence: str) -> tuple[str, str] | None:
    """Return (kind, matched snippet) for the first unsupported-claim cue, else None."""
    claims = _claims_in(sentence)
    return claims[0] if claims else None


def _claims_in(sentence: str) -> list[tuple[str, str]]:
    """Every (kind, snippet) claim cue in a sentence. One unsupported cue sinks the sentence."""
    found: list[tuple[str, str]] = []
    for kind, patterns in _CLAIM_KINDS:
        for pattern in patterns:
            for match in pattern.finditer(sentence):
                found.append((kind, match.group(0)))
    return found


def _overlap(words: set[str], context_words: set[str]) -> float:
    return len(words & context_words) / len(words) if words else 0.0


def _context_supports(kind: str, sentence: str, context: str, snippet: str = "") -> bool:
    """Is this claim backed by what the author actually wrote?

    - priority: the author must make a priority statement themselves.
    - completed_work: the work described (the rest of the sentence) must appear in the
      evidence, e.g. "we verified the cell lines by STR profiling" when the evidence
      says "cell lines were authenticated by STR profiling".
    - value / superiority / impact: the claim words themselves ("unique insights",
      "considerably faster") must appear in the evidence, and the sentence must be
      about what the evidence describes. Topic overlap alone is not support.
    """
    if not context.strip():
        return False
    if kind == "priority":
        return any(p.search(context) for p in _CLAIM_KINDS[0][1])
    context_words = _content_words(context)
    sentence_words = _content_words(sentence)
    if kind == "completed_work":
        return _overlap(sentence_words - _content_words(snippet), context_words) >= 0.6
    snippet_words = _content_words(snippet)
    if snippet_words and not snippet_words <= context_words:
        return False
    return _overlap(sentence_words, context_words) >= 0.5


def _numbers_in(text: str) -> list[tuple[str, int]]:
    """Numbers worth checking, skipping 'Section 3.2' / 'Table 4' style references."""
    found: list[tuple[str, int]] = []
    for match in _NUMBER_RE.finditer(text):
        number = match.group(1)
        words_before = text[max(0, match.start() - 14):match.start()].lower().rstrip(" .#").split()
        if words_before and words_before[-1].rstrip(".") in _NUMBER_REF_WORDS:
            continue
        if len(number) < 2 and not match.group(2):
            continue
        found.append((number + match.group(2), match.start()))
    return found


def check_claims(response: str, manuscript_context: str | None, reviewer_text: str = "") -> GroundingResult:
    """Remove unsupported claim sentences from a drafted response and report what changed.

    Args:
        response: The drafted author response.
        manuscript_context: What the author supplied (key findings, methods, numbers).
        reviewer_text: The reviewer comment being answered (numbers it quotes are allowed).
    """
    context = (manuscript_context or "").strip()
    flags: List[str] = []
    kept: List[str] = []
    removed: List[RemovedSentence] = []
    sentences = split_sentences(response)

    for index, sentence in enumerate(sentences):
        unsupported = first_unsupported_claim(sentence, context)
        if unsupported:
            kind, snippet = unsupported
            flags.append(unsupported_claim_flag(kind, snippet))
            removed.append(RemovedSentence(index=index, sentence=sentence, kind=kind, snippet=snippet))
            continue
        kept.append(sentence)

    cleaned = " ".join(kept).strip()

    # Numbers that appear nowhere in the author's evidence or the reviewer's text.
    allowed = f"{context} {reviewer_text}"
    for number, _ in _numbers_in(cleaned):
        if number not in allowed:
            flags.append(f"unverified_number: '{number}' is not in the supplied context")
            break  # one flag is enough; the UI does not need a list

    return GroundingResult(
        text=cleaned, flags=flags, stripped=bool(removed), sentences=sentences, removed=removed
    )


def first_unsupported_claim(sentence: str, manuscript_context: str | None) -> tuple[str, str] | None:
    """(kind, snippet) of the first claim in `sentence` the evidence does not support, else None."""
    context = (manuscript_context or "").strip()
    for kind, snippet in _claims_in(sentence):
        if not _context_supports(kind, sentence, context, snippet):
            return kind, snippet
    return None
