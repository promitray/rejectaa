"""Words a PDF broke at the end of a line, joined back: "re-cently" -> "recently", "hy-perparameter" -> "hyperparameter".

A justified PDF breaks long words with a hyphen at the line end. When the lines are joined into a paragraph the
hyphen stays ("ad-vances"), which the model then reads, quotes and embeds as two pieces. A real compound broken at
its own hyphen ("non-deep", "data-driven") must keep it.

The decision uses only the paper itself, so the same file always gives the same text:
1. the hyphenated form printed whole on one line somewhere in the paper ("non-deep") -> keep the hyphen;
2. the joined word printed elsewhere in the paper ("recently") -> join;
3. both halves are words the paper uses on their own ("fast" and "growing") -> keep;
4. otherwise -> join (a typesetter's break: "comprehen" and "sive" are not words).
Only a break between two lower-case letters is touched, and never a part of a longer chain ("state-of-the-art").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z]+")
# a hyphen with white space after it (the line break the paragraph join turned into a space), between two
# lower-case letters, and not part of a chain of hyphens
# the first half may start with a capital ("Ma- chine", "Ta- ble 2" at the start of a sentence or a name)
_LINE_BREAK_HYPHEN = re.compile(r"(?<![A-Za-z]-)\b([A-Za-z][a-z]*)-\s+([a-z]+)\b(?!-[a-z])")
_INLINE_COMPOUND = re.compile(r"(?<![a-z]-)\b([a-z]+-[a-z]+)\b(?!-)")
MIN_PART = 2  # "x-ray", "p-value": a one-letter half is never a line break
# prefixes that make compounds ("self-supervised", "non-smooth"): kept unless the paper prints the joined word
_COMPOUND_PREFIXES = frozenset("self non semi anti cross multi inter intra well ill quasi pseudo meta post".split())
# second halves that make compounds ("sequence-based", "data-driven", "solvent-free")
_COMPOUND_SUFFIXES = frozenset(
    "based driven free like wise level scale specific dependent independent related aware oriented rich poor "
    "sized shaped fold induced mediated derived assisted catalyzed catalysed resistant sensitive responsive "
    "containing bearing modified functionalized functionalised coated doped loaded".split()
)


@dataclass(frozen=True)
class Vocabulary:
    """The words and the hyphenated compounds a paper prints whole on one line."""

    words: frozenset[str] = field(default_factory=frozenset)
    compounds: frozenset[str] = field(default_factory=frozenset)


def vocabulary(text: str) -> Vocabulary:
    """What the paper prints: every lower-case word and every compound printed on one line."""
    lowered = text.lower()
    words: set[str] = set()
    compounds: set[str] = set()
    for line in lowered.split("\n"):
        stripped = line.rstrip()
        # a compound at the very end of a line may be a break ("ad-" + "vances" printed as "ad-vances" nowhere)
        for match in _INLINE_COMPOUND.finditer(stripped):
            compounds.add(match.group(1))
        if stripped.endswith("-"):
            stripped = stripped[:-1]
        # a word at the start of a line can be the second half of a break, at the end the first half: both still
        # count as words only when they appear elsewhere too, so take the inner words of each line first
        tokens = _WORD.findall(stripped)
        words.update(tokens[1:-1] if len(tokens) > 2 else tokens)
    return Vocabulary(frozenset(words), frozenset(compounds))


def joined(first: str, second: str, vocab: Vocabulary) -> bool:
    """Whether "first-<line break>second" is one word broken by the typesetter (compared in lower case)."""
    first, second = first.lower(), second.lower()
    if len(first) < MIN_PART or len(second) < MIN_PART:
        return False
    if f"{first}-{second}" in vocab.compounds:
        return False
    if f"{first}{second}" in vocab.words:
        return True
    if first in _COMPOUND_PREFIXES or second in _COMPOUND_SUFFIXES:
        return False
    if first in vocab.words and second in vocab.words:
        return False
    return True


def dehyphenate(text: str, vocab: Vocabulary) -> str:
    """The text with each line-end break ("re- cently", as a paragraph join leaves it) resolved."""

    def repl(match: re.Match[str]) -> str:
        first, second = match.group(1), match.group(2)
        return f"{first}{second}" if joined(first, second, vocab) else f"{first}-{second}"

    return _LINE_BREAK_HYPHEN.sub(repl, text)


_VOCAB: ContextVar[Vocabulary | None] = ContextVar("paper_vocabulary", default=None)


def use_vocabulary(text: str):  # noqa: ANN201 - a ContextVar token
    """Make the paper's vocabulary visible to `dehyphenate_current` while the paper is being read."""
    return _VOCAB.set(vocabulary(text))


def reset_vocabulary(token) -> None:  # noqa: ANN001
    _VOCAB.reset(token)


def dehyphenate_current(text: str) -> str:
    """`dehyphenate` with the vocabulary of the paper being read; without one the text is returned unchanged
    (the parser then keeps every hyphen, as it always did)."""
    vocab = _VOCAB.get()
    return text if vocab is None else dehyphenate(text, vocab)


_COLLAPSED = re.compile(r"(?<![A-Za-z]-)\b([A-Za-z][a-z]*)-([a-z]+)\b(?!-[a-z])")


def rejoin_current(text: str | None) -> str | None:
    """For text whose line breaks were already joined without a space ("re-cently", as the PDF abstract readers
    leave it): every single-hyphen word that the paper never prints whole on one line goes through `joined`."""
    vocab = _VOCAB.get()
    if not text or vocab is None:
        return text

    def repl(match: re.Match[str]) -> str:
        first, second = match.group(1), match.group(2)
        return f"{first}{second}" if joined(first, second, vocab) else match.group(0)

    return _COLLAPSED.sub(repl, text)


def words_in(texts: Iterable[str]) -> set[str]:
    return {w for text in texts for w in _WORD.findall(text.lower())}
