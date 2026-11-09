"""What each calibration intensity may change, and code checks that the output kept the meaning.

The model is told the rules; this module checks them afterwards, because a model does not always follow
them. A failed check sends the text back once with the exact reason. What still fails after that is shown
to the user as a warning; it is never silently accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN = re.compile(r"__REJ_INV_\d{4}__")


@dataclass(frozen=True)
class ModeRules:
    name: str
    min_length_ratio: float  # words in the output / words in the input
    max_sentence_change: int | None  # None: sentences may be merged or split freely
    allowed: str
    forbidden: str


MODES: dict[str, ModeRules] = {
    "polish": ModeRules(
        "polish", 0.90, 1,
        allowed="fix grammar, spelling, wrong or weak word choices, words broken by hyphenation, and drop empty filler words "
        "(for example 'Indeed', 'It is important to note that')",
        forbidden="do NOT merge, split or reorder sentences and do NOT change how a sentence is built; keep every clause",
    ),
    "rewrite": ModeRules(
        "rewrite", 0.75, 1,
        allowed="rebuild each sentence for clarity (active voice where natural, shorter clauses, a clear subject and verb)",
        forbidden="do NOT reorder the sentences and do NOT drop any fact, item of a list, condition or qualifier",
    ),
    "restructure": ModeRules(
        "restructure", 0.65, None,
        allowed="reorder sentences within the paragraph, merge or split sentences, and tighten the argument",
        forbidden="do NOT drop any fact, item of a list, condition or qualifier, and do NOT add any",
    ),
}


def mode_rules(intensity: str) -> ModeRules:
    return MODES.get(intensity, MODES["rewrite"])


# --------------------------------------------------------------------------- sentences and words

_ABBREVIATIONS = re.compile(
    r"\b(?:e\.g|i\.e|et al|etc|vs|cf|Fig|Figs|Eq|Eqs|Ref|Refs|approx|ca|Dr|Prof|No|Vol|Ch|Sect)\.", re.IGNORECASE
)


def sentence_count(text: str) -> int:
    """Sentences in a text. Abbreviations (e.g., Fig., et al.) and decimal numbers do not end a sentence."""
    cleaned = _ABBREVIATIONS.sub("ABBR", TOKEN.sub("", text))
    cleaned = re.sub(r"(?<=\d)\.(?=\d)", "", cleaned)
    # a sentence ends at . ! ? possibly followed by a citation number ("delivery.3 In nature")
    end = r"(?<=[.!?])(?:\d{1,3}[a-z]?(?:[,–\-]\d{1,3}[a-z]?)*)?[\"')\]]*\s+(?=[A-Z(\[])"
    parts = [p for p in re.split(end, cleaned.strip()) if p and p.strip()]
    return max(1, len(parts))


def word_count(text: str) -> int:
    return len(TOKEN.sub("", text).split()) + len(TOKEN.findall(text))


# --------------------------------------------------------------------------- meaning checks

# Words that weaken a claim. If the input has one of a family and the output none, the claim got stronger.
HEDGE_FAMILIES: dict[str, tuple[str, ...]] = {
    "may/might/could": ("may", "might", "could"),
    "suggest": ("suggest", "suggests", "suggested", "suggesting"),
    "indicate": ("indicate", "indicates", "indicated", "indicating"),
    "appear/seem": ("appear", "appears", "appeared", "seem", "seems", "seemed"),
    "likely/possibly": ("likely", "possibly", "presumably", "probably", "putative", "putatively"),
    "allow/enable": ("allow", "allows", "allowed", "enable", "enables", "enabled", "permit", "permits"),
    "potential": ("potential", "potentially"),
}
NEGATIONS = ("not", "no", "never", "without", "none", "neither", "nor", "cannot", "lack", "lacks", "absence")
# Words a rewrite must not add unless the input already has them: they claim more than the text shows.
CLAIM_WORDS = ("significant", "significantly", "novel", "first", "unprecedented", "remarkable", "remarkably",
               "breakthrough", "dramatic", "dramatically", "superior", "outstanding", "revolutionary")
_LABEL = re.compile(r"\b(?:[A-Z][A-Za-z]{0,4}\d+[a-z]?|[A-Za-z]*[A-Z][a-z]*[A-Z][A-Za-z]*)\b")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+(?:'[a-z]+)?", TOKEN.sub(" ", text).lower())


def _labels(text: str) -> set[str]:
    """Compound labels and acronyms (P1, S2, MMAHDAPy, TEMPO, SEC): names the text must keep."""
    return {m.group(0) for m in _LABEL.finditer(TOKEN.sub(" ", text)) if len(m.group(0)) >= 2}


def check_output(original: str, output: str, intensity: str) -> list[str]:
    """Problems with a calibrated text, each phrased so it can be sent back to the model as an instruction."""
    rules = mode_rules(intensity)
    problems: list[str] = []

    before, after = word_count(original), word_count(output)
    if before >= 15 and after < rules.min_length_ratio * before:
        problems.append(
            f"The text was shortened from {before} to {after} words ({round(100 * after / before)}%). "
            f"In {rules.name} mode it must keep at least {round(100 * rules.min_length_ratio)}% of the length: "
            "put back what was dropped."
        )

    if rules.max_sentence_change is not None:
        s_before, s_after = sentence_count(original), sentence_count(output)
        if abs(s_before - s_after) > rules.max_sentence_change:
            problems.append(
                f"The text has {s_after} sentences instead of {s_before}. In {rules.name} mode keep the sentences as "
                "they are: do not merge or split them."
            )

    problems.extend(meaning_problems(original, output))
    return problems


def meaning_problems(original: str, output: str) -> list[str]:
    """Code-visible changes of meaning: dropped qualifiers or negations, added claim words, missing names or labels."""
    problems: list[str] = []
    words_in, words_out = _words(original), _words(output)
    set_in, set_out = set(words_in), set(words_out)
    for family, forms in HEDGE_FAMILIES.items():
        if set_in & set(forms) and not set_out & set(forms):
            kept = ", ".join(sorted(set_in & set(forms)))
            problems.append(f"The qualifier '{kept}' was removed, which makes the claim stronger than the original. Keep it.")
    negations_in = sum(words_in.count(w) for w in NEGATIONS)
    negations_out = sum(words_out.count(w) for w in NEGATIONS)
    if negations_out < negations_in:
        problems.append("A negation (not, no, without...) was removed. Check that no statement now says the opposite.")

    added = sorted(w for w in CLAIM_WORDS if w in set_out and w not in set_in)
    if added:
        problems.append(
            "These words claim more than the original does and must not be added: " + ", ".join(added) + "."
        )

    missing_labels = sorted(_labels(original) - _labels(output))
    if missing_labels:
        problems.append("These names or labels are missing and must stay: " + ", ".join(missing_labels[:10]) + ".")
    return problems


# --------------------------------------------------------------------------- citation position

_CITATION_VALUE = re.compile(r"^\d{1,3}[a-z]?(?:\s?[,–\-]\s?\d{1,3}[a-z]?)*$")
_PUNCT = ",.;:"


def fix_citation_punctuation(original_masked: str, output_masked: str, citation_tokens: set[str]) -> str:
    """Put each citation marker on the same side of the comma or full stop as in the original.

    "imaging,__T__" in the original and "imaging__T__," in the output becomes "imaging,__T__" again,
    and the other way round. Journals differ on this, so the author's own placement is kept.
    """
    fixed = output_masked
    for token in citation_tokens:
        at = original_masked.find(token)
        if at < 0:
            continue
        before = original_masked[at - 1] if at > 0 else ""
        after = original_masked[at + len(token)] if at + len(token) < len(original_masked) else ""
        escaped = re.escape(token)
        if before in _PUNCT:
            # original: "word,TOKEN" -> fix "wordTOKEN," to "word,TOKEN"
            fixed = re.sub(escaped + r"([" + re.escape(_PUNCT) + r"])", r"\1" + token, fixed, count=1)
        elif after in _PUNCT or after.isspace() or after == "":
            # original: "wordTOKEN," -> fix "word,TOKEN" to "wordTOKEN,"
            fixed = re.sub(r"([" + re.escape(_PUNCT) + r"])" + escaped, token + r"\1", fixed, count=1)
    return fixed


def citation_tokens(vault: dict[str, str], categories: dict[str, str]) -> set[str]:
    """Tokens that stand for citation numbers (glued markers, or superscript runs from a Word file)."""
    return {
        token for token, value in vault.items()
        if categories.get(token) in ("citation_marker", "format") and _CITATION_VALUE.match(value.strip())
    }
