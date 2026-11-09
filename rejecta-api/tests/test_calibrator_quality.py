"""Voice Calibrator: the three intensities, meaning checks, citation position, one targeted retry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from config import settings
from services.calibrator import VoiceCalibratorService
from services.calibrator.corpus import JournalCorpusService
from services.calibrator.quality import (
    check_output,
    citation_tokens,
    fix_citation_punctuation,
    mode_rules,
    sentence_count,
)
from services.llm.base import AbstractAnalysisProvider

# The user's own test paragraph and the calibrated text it came back as.
ORIGINAL = (
    "The design and synthesis of self-reporting delivery systems has attracted intense research interest due to its "
    "applications in fields ranging from degradable medical materials1 to imaging,2 and drug delivery.3 In nature, "
    "self-reporting neuro-nal networks allow for triggering repair processes, and thus assessing damage/repair events "
    "and their locations, as well as the health status of structures over large areas.4 Similarly, self-reporting "
    "capabilities in synthetic macromolecular systems enable a wide range of technological possibilities. Indeed, the "
    "recent successful synthesis of self-reporting sensors has been realized by embedding mechano-5 and/or "
    "(pro)fluorophores6 into polymers."
)
BAD = (
    "Self-reporting delivery systems are of significant interest for applications in degradable medical materials1, "
    "imaging2, and drug delivery3. In nature, self-reporting neuronal networks trigger repair processes, assess "
    "damage/repair events, and monitor structural health over large areas4. Analogously, synthetic macromolecular "
    "systems with self-reporting capabilities offer technological potential. Recent advancements include the "
    "synthesis of self-reporting sensors by embedding mechano-5 and/or (pro)fluorophores6 into polymers."
)
GOOD = (
    "The design and synthesis of self-reporting delivery systems has attracted intense research interest for "
    "applications in degradable medical materials1 to imaging,2 and drug delivery.3 In nature, self-reporting neuronal "
    "networks allow repair processes to be triggered and damage/repair events, their locations and the health status "
    "of structures over large areas to be assessed.4 Similarly, self-reporting capabilities in synthetic macromolecular "
    "systems enable a wide range of technological possibilities. Self-reporting sensors have recently been "
    "synthesized by embedding mechano-5 and/or (pro)fluorophores6 into polymers."
)


# ------------------------------------------------------------------ the three modes

def test_the_three_modes_have_different_limits() -> None:
    assert [mode_rules(m).min_length_ratio for m in ("polish", "rewrite", "restructure")] == [0.90, 0.75, 0.65]
    assert mode_rules("restructure").max_sentence_change is None
    assert mode_rules("unknown").name == "rewrite"


def test_sentences_are_counted_through_citations_abbreviations_and_decimals() -> None:
    assert sentence_count(ORIGINAL) == 4
    assert sentence_count("See Fig. 2 and e.g. the 2.5 nm gap. Then P1 was made.") == 2


def test_the_users_bad_output_is_caught_on_every_point_code_can_see() -> None:
    problems = " | ".join(check_output(ORIGINAL, BAD, "rewrite"))
    assert "shortened from" in problems  # 92 -> 62 words
    assert "allow" in problems  # "allow for triggering" became "trigger"
    assert "significant" in problems


def test_a_faithful_rewrite_passes() -> None:
    assert check_output(ORIGINAL, GOOD, "rewrite") == []


def test_polish_does_not_allow_merging_sentences_but_restructure_does() -> None:
    two_fewer = ORIGINAL.replace(".4 Similarly,", ",4 while").replace(" Indeed, the", ", and the")
    assert any("sentences" in p for p in check_output(ORIGINAL, two_fewer, "polish"))
    assert not any("sentences" in p for p in check_output(ORIGINAL, two_fewer, "restructure"))


def test_dropped_negations_and_labels_are_caught() -> None:
    problems = " | ".join(check_output("P2 does not show an EPR signal of TEMPO.", "P2 shows an EPR signal.", "polish"))
    assert "negation" in problems and "TEMPO" in problems


# ------------------------------------------------------------------ citation position

def test_citation_markers_go_back_to_the_authors_side_of_the_punctuation() -> None:
    original = "materials__REJ_INV_0000__ to imaging,__REJ_INV_0001__ and delivery.__REJ_INV_0002__ In"
    output = "materials__REJ_INV_0000__, imaging__REJ_INV_0001__, and delivery__REJ_INV_0002__. In"
    tokens = {"__REJ_INV_0000__", "__REJ_INV_0001__", "__REJ_INV_0002__"}
    assert fix_citation_punctuation(original, output, tokens) == (
        "materials__REJ_INV_0000__, imaging,__REJ_INV_0001__ and delivery.__REJ_INV_0002__ In"
    )


def test_only_citation_numbers_are_moved() -> None:
    vault = {"__REJ_INV_0000__": "2,3", "__REJ_INV_0001__": "8700 g", "__REJ_INV_5000__": "10a", "__REJ_INV_5001__": "P1"}
    categories = {"__REJ_INV_0000__": "citation_marker", "__REJ_INV_0001__": "unit", "__REJ_INV_5000__": "format", "__REJ_INV_5001__": "format"}
    assert citation_tokens(vault, categories) == {"__REJ_INV_0000__", "__REJ_INV_5000__"}


# ------------------------------------------------------------------ journal register

def test_chemistry_journals_get_a_chemistry_register_and_unknown_names_are_flagged() -> None:
    known = asyncio.run(JournalCorpusService.get_journal_register_profile("Polymer Chemistry"))
    assert known["recognised"] and "characterization" in known["hedging_level"]
    unknown = asyncio.run(JournalCorpusService.get_journal_register_profile("Some Journal of Things"))
    assert unknown["recognised"] is False


# ------------------------------------------------------------------ the calibrator end to end

class ScriptedLLM(AbstractAnalysisProvider):
    """Returns the scripted rewrites in order, and answers meaning checks with `meaning`."""

    def __init__(self, rewrites: list[str], meaning: list[dict[str, list[str]]] | None = None) -> None:
        self.rewrites = list(rewrites)
        self.meaning = list(meaning or [])
        self.prompts: list[tuple[str, str]] = []

    async def analyse(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append((system_prompt, user_prompt))
        if user_prompt.startswith("ORIGINAL:"):
            answer = self.meaning.pop(0) if self.meaning else {"lost": [], "stronger": [], "added": []}
            return json.dumps(answer)
        masked = user_prompt.split("\n\n", 1)[1].split("\n\nCRITICAL FIX", 1)[0]
        template = self.rewrites.pop(0)
        return json.dumps({"calibrated_masked_text": template.format(masked=masked), "modifications": ["edited"]})


def _rewrite_calls(llm: ScriptedLLM) -> list[tuple[str, str]]:
    return [p for p in llm.prompts if not p[1].startswith("ORIGINAL:")]


def _masked_of(text: str) -> str:
    from services.calibrator.vault import ScientificVault

    return ScientificVault.mask(text).masked_text


def calibrate(llm: ScriptedLLM, text: str = ORIGINAL, **kwargs: Any):
    return asyncio.run(
        VoiceCalibratorService(llm).calibrate(text=text, journal_name=kwargs.pop("journal", "Polymer Chemistry"), field="Polymer chemistry", **kwargs)
    )


@pytest.fixture
def meaning_on(monkeypatch):
    monkeypatch.setattr(settings, "CALIBRATOR_MEANING_CHECK", True)


@pytest.fixture
def meaning_off(monkeypatch):
    monkeypatch.setattr(settings, "CALIBRATOR_MEANING_CHECK", False)


def test_a_faithful_first_draft_is_accepted_with_one_rewrite_and_one_check(meaning_on) -> None:
    llm = ScriptedLLM(["{masked}"])
    result = calibrate(llm)
    assert result.calibrated_text == ORIGINAL
    assert len(_rewrite_calls(llm)) == 1 and len(llm.prompts) == 2
    assert not any(note.startswith("Check:") for note in result.audit_notes)


def test_a_shortened_draft_is_sent_back_once_with_the_reason(meaning_off) -> None:
    # first answer: drop most of the text but keep the tokens
    first = _masked_of(ORIGINAL)
    tokens = " ".join(t for t in first.split() if "__REJ_INV_" in t)
    llm = ScriptedLLM(["Short text " + tokens, "{masked}"])
    result = calibrate(llm)
    calls = _rewrite_calls(llm)
    assert len(calls) == 2
    assert "CRITICAL FIX" in calls[1][1] and "shortened from" in calls[1][1] and "YOUR PREVIOUS VERSION" in calls[1][1]
    assert result.calibrated_text == ORIGINAL  # the retry was faithful
    assert not any(note.startswith("Check:") for note in result.audit_notes)


def test_problems_left_after_the_retry_are_shown_not_hidden(meaning_off) -> None:
    llm = ScriptedLLM(["{masked} It is significant.", "{masked} It is significant."])
    result = calibrate(llm)
    assert result.calibrated_text is not None
    assert any(note.startswith("Check:") and "significant" in note for note in result.audit_notes)


def test_the_meaning_check_finds_what_code_cannot_and_feeds_the_retry(meaning_on) -> None:
    llm = ScriptedLLM(["{masked}", "{masked}"], meaning=[{"lost": ["and their locations"], "stronger": [], "added": []}])
    result = calibrate(llm)
    calls = _rewrite_calls(llm)
    assert len(calls) == 2 and "and their locations" in calls[1][1]
    assert not any(note.startswith("Check:") for note in result.audit_notes)  # the second check found nothing


def test_with_the_meaning_check_off_only_one_call_is_made(meaning_off) -> None:
    llm = ScriptedLLM(["{masked}"])
    calibrate(llm)
    assert len(llm.prompts) == 1


def test_a_failing_meaning_check_never_stops_the_calibration(meaning_on) -> None:
    class Broken(ScriptedLLM):
        async def generate(self, system_prompt, user_prompt, temperature=0.2):
            if user_prompt.startswith("ORIGINAL:"):
                raise RuntimeError("checker down")
            return await super().generate(system_prompt, user_prompt, temperature)

    result = calibrate(Broken(["{masked}"]))
    assert result.calibrated_text == ORIGINAL


def test_citations_end_up_on_the_authors_side_of_the_punctuation(meaning_off) -> None:
    def move(masked: str) -> str:
        import re

        return re.sub(r"([,.])(__REJ_INV_\d{4}__)", r"\2\1", masked)

    class Mover(ScriptedLLM):
        async def generate(self, system_prompt, user_prompt, temperature=0.2):
            self.prompts.append((system_prompt, user_prompt))
            masked = user_prompt.split("\n\n", 1)[1].split("\n\nCRITICAL FIX", 1)[0]
            return json.dumps({"calibrated_masked_text": move(masked), "modifications": []})

    result = calibrate(Mover([]))
    assert "imaging,2 and drug delivery.3 In nature" in result.calibrated_text


def test_each_mode_puts_its_own_rules_in_the_prompts(meaning_off) -> None:
    prompts = {}
    for mode in ("polish", "rewrite", "restructure"):
        llm = ScriptedLLM(["{masked}"])
        calibrate(llm, intensity=mode)
        prompts[mode] = llm.prompts[0]
    assert "MODE: POLISH" in prompts["polish"][0] and "do NOT merge, split or reorder" in prompts["polish"][0]
    assert "MODE: RESTRUCTURE" in prompts["restructure"][0] and "reorder sentences" in prompts["restructure"][0]
    assert "at least 90%" in prompts["polish"][0] and "at least 65%" in prompts["restructure"][0]
    assert "POLISH mode" in prompts["polish"][1] and "RESTRUCTURE mode" in prompts["restructure"][1]
    assert "STRUCTURAL COMPRESSION" not in prompts["rewrite"][0] and "removing all filler" not in prompts["rewrite"][1]


def test_an_unrecognised_journal_name_is_reported(meaning_off) -> None:
    result = calibrate(ScriptedLLM(["{masked}"]), journal="Journal of Things")
    assert any("not a journal name we recognise" in n for n in result.audit_notes)
