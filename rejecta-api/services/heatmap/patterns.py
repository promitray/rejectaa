"""Catalogue of formulaic phrasing that is over-represented in machine-written and template-like scientific prose.

None of these is wrong on its own; a human may write any of them. What makes text read as formulaic is
several of them close together, so the heatmap scores their density, not single words. Each entry is a
regular expression (lower case, word-bounded) in a named group so every flag can say why it was raised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PATTERN_GROUPS: dict[str, tuple[str, ...]] = {
    # stock phrases that frame instead of say
    "filler phrase": (
        r"in the realm of", r"in the (?:ever-)?evolving (?:field|landscape|world) of", r"in today'?s (?:world|landscape)",
        r"it is (?:worth|important to) (?:noting|note|highlight|mention|emphasi[sz]e)", r"it should be noted that",
        r"it is important to remember", r"plays? an? (?:crucial|pivotal|vital|key|significant|important) role in",
        r"a testament to", r"serves? as a testament", r"in the context of this", r"when it comes to",
        r"at the forefront of", r"a wide (?:range|array|variety) of", r"a myriad of", r"a plethora of",
        r"the (?:intricate|complex) interplay (?:between|of)", r"shed(?:s|ding)? (?:new )?light on",
        r"delve(?:s|d)? (?:deeper )?into", r"embark(?:s|ed)? on", r"navigat(?:e|es|ing) the (?:complex|intricate)? ?(?:landscape|terrain|challenges)",
        r"(?:the )?(?:ever-)?(?:changing|evolving) landscape", r"in (?:summary|conclusion), this (?:work|study|paper)",
    ),
    # verbs that inflate an ordinary action
    "inflated verb": (
        r"leverag(?:e|es|ed|ing)", r"harness(?:es|ed|ing)?", r"foster(?:s|ed|ing)?", r"showcas(?:e|es|ed|ing)",
        r"underscor(?:e|es|ed|ing)", r"elucidat(?:e|es|ed|ing)", r"spearhead(?:s|ed|ing)?", r"revolutioni[sz](?:e|es|ed|ing)",
        r"unlock(?:s|ed|ing)? (?:the )?(?:full )?potential", r"pav(?:e|es|ed|ing) the way", r"bridg(?:e|es|ed|ing) the gap",
        r"empower(?:s|ed|ing)?", r"streamlin(?:e|es|ed|ing)", r"bolster(?:s|ed|ing)?",
    ),
    # adjectives and adverbs that claim importance without evidence
    "hype word": (
        r"pivotal", r"paramount", r"crucial", r"invaluable", r"intricate", r"multifaceted", r"groundbreaking",
        r"cutting-edge", r"state-of-the-art", r"unprecedented", r"transformative", r"remarkabl[ey]", r"seamless(?:ly)?",
        r"robust and (?:comprehensive|scalable|efficient)", r"holistic", r"meticulous(?:ly)?", r"profound(?:ly)?",
        r"tapestry", r"synerg(?:y|ies|istic)", r"novel and innovative", r"innovative",
    ),
    # sentence shapes that recur in templated prose
    "formulaic structure": (
        r"not only\b.{3,80}?\bbut also", r"this (?:work|study|paper|approach) (?:not only )?(?:highlights|demonstrates|underscores|showcases)",
        r"(?:by|through) (?:harnessing|leveraging|exploiting|integrating) .{3,60}?, (?:we|this|researchers)",
        r"(?:ultimately|overall), (?:this|these|our)", r"offers? (?:valuable|new|deep|key) insights? into",
        r"(?:provides?|offers?) a (?:robust|powerful|versatile|promising) (?:platform|framework|tool|approach)",
    ),
    # endings that promise impact instead of reporting it
    "empty conclusion": (
        r"(?:transformative|immense|enormous|tremendous|significant) potential", r"shap(?:e|es|ing) the future of",
        r"open(?:s|ing)? (?:up )?new (?:avenues|horizons|possibilities|frontiers)", r"holds? (?:great|immense|significant) promise",
        r"far-reaching implications", r"(?:a )?(?:new|exciting) (?:era|chapter|paradigm)", r"paradigm shift",
        r"for years to come", r"(?:wide|broad) range of applications",
    ),
}


@dataclass(frozen=True)
class PhraseMatch:
    group: str
    text: str
    start: int
    end: int


_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (group, re.compile(r"\b(?:" + pattern + r")\b", re.IGNORECASE))
    for group, patterns in PATTERN_GROUPS.items()
    for pattern in patterns
]


def find_phrases(text: str) -> list[PhraseMatch]:
    """Every catalogue match in `text`, longest first where two overlap."""
    found: list[PhraseMatch] = []
    for group, pattern in _COMPILED:
        for m in pattern.finditer(text):
            found.append(PhraseMatch(group, m.group(0), m.start(), m.end()))
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[PhraseMatch] = []
    for match in found:
        if not kept or match.start >= kept[-1].end:
            kept.append(match)
    return kept


# "X, Y, and Z" lists of three noun phrases: one is normal, several in a paragraph is a template habit.
TRICOLON = re.compile(r"\b\w+(?:\s\w+){0,2}, \w+(?:\s\w+){0,2},? and \w+", re.IGNORECASE)
EM_DASH = re.compile(r"\s?—\s?|\s--\s")
