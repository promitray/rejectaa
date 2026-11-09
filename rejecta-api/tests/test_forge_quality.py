"""Abstract Forge: computed scores, checks of every version, one targeted retry, blocking, keywords, word limit."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from config import settings
from services.calibrator.vault import ScientificVault
from services.forge import AbstractForgeEngine, ForgeError
from services.llm.base import AbstractAnalysisProvider

DRAFT = (
    "We pioneer a self-reporting profluorescent release system in which pyrene fluorescence may be silenced by "
    "nitroxide radicals. The copolymer P2 released 13 pyrene units at 90 °C, and the fluorescence increase was "
    "visible to the naked eye. The release is not reversible."
)
MASKED = ScientificVault.mask(DRAFT).masked_text  # "... at __REJ_INV_0000__ ..." for 90 °C


def version(v_type: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"variant_type": v_type, "masked_text": text, "key_highlights": ["h"], **extra}


FAITHFUL = MASKED
GOOD = {
    "comparative_summary": "Three versions.",
    "variants": [
        version("discoverability_seo", FAITHFUL, seo_keywords=["pyrene fluorescence", "nitroxide radicals", "quantum dots"]),
        version("clarity_direct", FAITHFUL),
        version("high_impact_narrative", FAITHFUL),
    ],
}


class ScriptedLLM(AbstractAnalysisProvider):
    """Answers generation calls from `generations` in order and meaning checks from `checks` in order."""

    def __init__(self, generations: list[Any], checks: list[dict[str, Any]] | None = None) -> None:
        self.generations = list(generations)
        self.checks = list(checks or [])
        self.prompts: list[str] = []

    async def analyse(self, *args: Any, **kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        self.prompts.append(user_prompt)
        if user_prompt.startswith("DRAFT:"):
            return json.dumps(self.checks.pop(0) if self.checks else {"versions": {}})
        answer = self.generations.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)


def forge(llm: ScriptedLLM, **kwargs: Any):
    return asyncio.run(AbstractForgeEngine(llm).forge(abstract_draft=kwargs.pop("draft", DRAFT), journal_name="Polymer Chemistry", field="Polymer chemistry", **kwargs))


def generation_calls(llm: ScriptedLLM) -> list[str]:
    return [p for p in llm.prompts if not p.startswith("DRAFT:")]


@pytest.fixture(autouse=True)
def meaning_check_on(monkeypatch):
    monkeypatch.setattr(settings, "FORGE_MEANING_CHECK", True)


# ------------------------------------------------------------------ scores come from the checks

def test_a_faithful_set_scores_100_with_one_generation_and_one_check() -> None:
    llm = ScriptedLLM([GOOD])
    result = forge(llm)
    assert len(result.variants) == 3 and len(llm.prompts) == 2
    for v in result.variants:
        assert v.readiness_index.overall_readiness_score == 100 and v.predicted_acceptance_score == 100
        assert v.claim_audit.is_faithful and not v.blocked
        assert "90 °C" in v.abstract_text


def test_the_models_own_scores_are_ignored() -> None:
    bad = json.loads(json.dumps(GOOD))
    for v in bad["variants"]:
        v["readiness_breakdown"] = {"overall": 12}
        v["masked_text"] = FAITHFUL
    assert all(v.predicted_acceptance_score == 100 for v in forge(ScriptedLLM([bad])).variants)


# ------------------------------------------------------------------ checks, retry, blocking

def test_a_version_with_problems_is_rewritten_once_with_its_reasons() -> None:
    weak = FAITHFUL.replace("may be silenced", "is silenced").replace(" The release is not reversible.", "")
    first = json.loads(json.dumps(GOOD))
    first["variants"][2]["masked_text"] = weak + " This is a novel breakthrough."
    fixed = {"variants": [version("high_impact_narrative", FAITHFUL)]}
    llm = ScriptedLLM([first, fixed])
    result = forge(llm)
    calls = generation_calls(llm)
    assert len(calls) == 2
    assert "VERSION high_impact_narrative" in calls[1] and "VERSION clarity_direct" not in calls[1]
    assert "novel" in calls[1] and "may" in calls[1]
    impact = next(v for v in result.variants if v.variant_type == "high_impact_narrative")
    assert impact.readiness_index.overall_readiness_score == 100 and not impact.warnings


def test_problems_left_after_the_retry_lower_the_score_and_are_shown() -> None:
    first = json.loads(json.dumps(GOOD))
    first["variants"][1]["masked_text"] = FAITHFUL + " It is the first such system."
    llm = ScriptedLLM([first, {"variants": [version("clarity_direct", FAITHFUL + " It is the first such system.")]}])
    clarity = next(v for v in forge(llm).variants if v.variant_type == "clarity_direct")
    assert clarity.warnings and any("first" in w for w in clarity.warnings)
    assert clarity.readiness_index.narrative_discipline < 100 and clarity.predicted_acceptance_score < 100
    assert not clarity.claim_audit.is_faithful


def test_a_version_that_loses_a_protected_value_is_blocked() -> None:
    lost = FAITHFUL.replace("__REJ_INV_0000__", "high temperature")
    first = json.loads(json.dumps(GOOD))
    first["variants"][0]["masked_text"] = lost
    llm = ScriptedLLM([first, {"variants": [version("discoverability_seo", lost)]}])
    seo = next(v for v in forge(llm).variants if v.variant_type == "discoverability_seo")
    assert seo.blocked and seo.abstract_text == "" and "90 °C" in seo.block_reasons[0]
    assert seo.score_mood == "blocked" and not seo.invariants_verified


def test_new_numbers_are_caught_but_numbers_from_the_context_are_allowed() -> None:
    with_context = FAITHFUL + " The polymer had a molar mass of 8700 g mol."
    invented = FAITHFUL + " The yield was 95."
    first = json.loads(json.dumps(GOOD))
    first["variants"][1]["masked_text"] = with_context
    first["variants"][2]["masked_text"] = invented
    llm = ScriptedLLM([first, {"variants": [version("high_impact_narrative", invented)]}])
    result = forge(llm, methodology_context="Mn = 8700 g mol−1 by SEC.")
    by_type = {v.variant_type: v for v in result.variants}
    assert not any("numbers" in w for w in by_type["clarity_direct"].warnings)
    assert any("95" in w for w in by_type["high_impact_narrative"].warnings)


def test_the_meaning_check_finds_what_code_cannot_and_drives_the_retry() -> None:
    llm = ScriptedLLM(
        [GOOD, {"variants": [version("clarity_direct", FAITHFUL)]}],
        checks=[{"versions": {"clarity_direct": {"lost": ["visible to the naked eye"], "stronger": [], "added": []}}}],
    )
    result = forge(llm)
    assert "visible to the naked eye" in generation_calls(llm)[1]
    clarity = next(v for v in result.variants if v.variant_type == "clarity_direct")
    assert not clarity.warnings and clarity.claim_audit.is_faithful  # the second check found nothing


def test_with_the_meaning_check_off_no_check_call_is_made(monkeypatch) -> None:
    monkeypatch.setattr(settings, "FORGE_MEANING_CHECK", False)
    llm = ScriptedLLM([GOOD])
    forge(llm)
    assert len(llm.prompts) == 1


# ------------------------------------------------------------------ keywords, length, failure, prompt

def test_keywords_not_in_the_text_or_draft_are_dropped() -> None:
    seo = next(v for v in forge(ScriptedLLM([GOOD])).variants if v.variant_type == "discoverability_seo")
    assert seo.seo_keywords == ["pyrene fluorescence", "nitroxide radicals"]
    assert any("quantum dots" in w for w in seo.warnings)


def test_a_version_over_the_word_limit_is_flagged() -> None:
    long_text = FAITHFUL + " " + "The release system is described in more words here. " * 6
    first = json.loads(json.dumps(GOOD))
    first["variants"][0]["masked_text"] = long_text
    llm = ScriptedLLM([first, {"variants": [version("discoverability_seo", long_text)]}])
    seo = next(v for v in forge(llm, max_words=40).variants if v.variant_type == "discoverability_seo")
    assert any("limit is 40" in w for w in seo.warnings)
    assert seo.readiness_index.narrative_discipline < 100


def test_a_failed_generation_raises_instead_of_showing_fake_versions() -> None:
    with pytest.raises(ForgeError):
        forge(ScriptedLLM([RuntimeError("model down")]))
    with pytest.raises(ForgeError):
        forge(ScriptedLLM([{"variants": []}]))


def test_the_prompt_has_no_example_scores_or_inflating_hints() -> None:
    engine = AbstractForgeEngine(ScriptedLLM([]))
    prompt = engine._build_system_prompt("Polymer Chemistry", "Polymer chemistry", engine._extract_source_fact_inventory(DRAFT),
                                         engine.resolve_venue_targets("Polymer Chemistry", "Polymer chemistry"), True, 120)
    assert "readiness" not in prompt and "paradigm" not in prompt.lower()
    assert "at most 120 words" in prompt and "CAS SciFinder" in prompt


def test_empty_journal_and_field_do_not_fall_back_to_a_computer_science_journal() -> None:
    result = asyncio.run(AbstractForgeEngine(ScriptedLLM([GOOD])).forge(abstract_draft=DRAFT, journal_name="", field=None))
    assert result.target_journal == "the target journal"
    assert "Machine Intelligence" not in result.variants[0].target_persona


# ------------------------------------------------------------------ the older suite, run without pytest-asyncio

def test_the_original_forge_tests_still_pass() -> None:
    import test_forge

    asyncio.run(test_forge.test_paired_bootstrap_comparison_and_category_telemetry())
    test_forge.test_venue_and_discipline_aware_targeting_resolution()
