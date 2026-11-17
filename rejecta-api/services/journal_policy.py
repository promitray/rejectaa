"""How the target journal decides, from knowledge/journals.yaml: soundness-only, selective, or standard.

The model is told what the journal's decision rests on (so it does not reject a sound paper at a soundness-only
journal for "insufficient novelty", nor wave a modest advance through at Nature), and code weights the scored
criteria to match: at a soundness-only journal novelty and journal fit weigh much less, rigor and evidence more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

POLICY_FILE = Path(__file__).resolve().parents[1] / "knowledge" / "journals.yaml"
SOUNDNESS, SELECTIVE, STANDARD = "soundness", "selective", "standard"

# Criterion weight multipliers per tier; the weights are renormalised by the weighted mean anyway.
WEIGHT_FACTORS = {
    SOUNDNESS: {"novelty": 0.3, "journal_fit": 0.5, "rigor": 1.3, "evidence_support": 1.3},
    SELECTIVE: {"novelty": 1.2, "journal_fit": 1.2},
    STANDARD: {},
}


@dataclass(frozen=True)
class Policy:
    journal: str
    tier: str
    basis: str
    matched: str  # the entry's name, or "" for the default

    def prompt_line(self) -> str:
        if self.tier == SOUNDNESS:
            return (
                f"HOW {self.journal.upper()} DECIDES: {self.basis} Judge soundness: methods, evidence, conclusions that "
                "follow from the data. Do not reject or mark down for limited novelty or impact; novelty is noted, not decisive."
            )
        if self.tier == SELECTIVE:
            return (
                f"HOW {self.journal.upper()} DECIDES: {self.basis} A sound but incremental paper is usually declined "
                "before review here; say how far the advance goes beyond the closest earlier work."
            )
        return ""

    def weight_note(self) -> str:
        if self.tier == SOUNDNESS:
            return f"Weighted for {self.journal}: it reviews for soundness, so novelty and journal fit count less."
        if self.tier == SELECTIVE:
            return f"Weighted for {self.journal}: it selects for significance, so novelty and journal fit count more."
        return ""


@lru_cache(maxsize=1)
def _entries(path: str | None = None) -> tuple[tuple[tuple[str, ...], str, str, str], ...]:
    raw = yaml.safe_load(Path(path or POLICY_FILE).read_text(encoding="utf-8")) or {}
    out = []
    for item in raw.get("journals") or []:
        patterns = tuple(str(p) for p in item.get("match") or [])
        out.append((patterns, str(item.get("tier") or STANDARD), str(item.get("basis") or ""), str(item.get("name") or "")))
    return tuple(out)


def policy_version() -> str:
    raw = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) or {}
    return str(raw.get("version") or "")


def policy_for(journal: str) -> Policy:
    name = re.sub(r"\s+", " ", (journal or "").strip())
    key = name.lower()
    for patterns, tier, basis, matched in _entries():
        if any(re.search(p, key) for p in patterns):
            return Policy(name, tier if tier in WEIGHT_FACTORS else STANDARD, basis, matched)
    return Policy(name, STANDARD, "", "")
