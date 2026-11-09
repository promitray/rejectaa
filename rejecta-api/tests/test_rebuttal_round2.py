"""Rebuttal Writer round 2: soft value claims, invented work, author decisions, consistency.

Cases come from several fields on purpose, so the guard is not tuned to one review.
The chemistry cases use the exact sentences from a real run that slipped through round 1.
"""
import json

import pytest

from services.rebuttal.engine import RebuttalEngine
from services.rebuttal.grounding import check_claims
from test_rebuttal_part_a import CapturingLLM

TRANSFER_REPORT = (
    "Reviewer 1\n"
    "This manuscript describes a dual-responsive polymer system. The synthesis and characterization are properly "
    "conducted. However, there are already several examples of on-demand disassembly polymers in the literature. "
    "Although the current one is new, it does not add any significant novelty to the field. Therefore, I think that "
    "this work is suitable for publication not in JACS but rather in a more specialized journal.\n\n"
    "Reviewer 2\n"
    "The manuscript describes some very nice analytical work on the degradation mechanisms of a dually-responsive "
    "polymer system. The system is a practical extension of known reactivities, and should contribute to the "
    "ever-growing suite of functional material building blocks. There are concerns regarding the level of impact "
    "and novelty that give me reservations about publication in JACS."
)
TRANSFER_EVIDENCE = (
    "Reviewer 2 notes 'very nice analytical work on the degradation mechanisms of a dually-responsive (heat and "
    "light) polymer system' and that it 'should contribute to the ever-growing suite of functional material building "
    "blocks available to polymer chemists and materials scientists', and that it 'certainly warrants publication, "
    "but in a more specialized journal', which we will meet with Macromolecules."
)


# ---------------------------------------------------------------------------
# Grounding: soft value claims
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    "Our work aims to contribute, and we believe it offers valuable insights into the degradation mechanisms of such systems.",
    "We chose Chemical Communication due to its broad readership and focus on significant advances in chemical sciences.",
    "We believe our findings will be of interest to a wide audience, including polymer chemists and materials scientists.",
    "Our study adds a valuable dimension to the understanding of responsive polymer systems.",
    "While the system may appear incremental, the dual-responsiveness offers unique insights into the interplay of stimuli.",
    # From the second real run: an adjective between the two claim words.
    "We believe that our work contributes valuable analytical insights into the degradation mechanisms.",
])
def test_soft_value_claims_from_real_run_are_removed(sentence):
    result = check_claims(sentence, TRANSFER_EVIDENCE, "")
    assert result.stripped is True, sentence
    assert result.text == ""


@pytest.mark.parametrize("sentence, field", [
    ("This framework fills an important gap in clinical decision support.", "medicine"),
    ("We introduce a novel framework that is of broad interest to the community.", "computer science"),
    ("The approach provides deeper understanding of migrant labour networks.", "social science"),
    ("This design represents a significant advance for bridge monitoring.", "engineering"),
    # Third real run: the adjective with a noun that was not in any list.
    ("Our work focuses on the unique degradation mechanisms involving a benzophenone trigger.", "chemistry"),
    ("The cohort offers a valuable window on rural care pathways.", "public health"),
    # Invented reasons for choosing a venue.
    ("The interdisciplinary nature of our study aligns with the readership of Chemical Communications.", "chemistry"),
    ("Our findings are well suited to the audience of this journal.", "any"),
    # Fourth real run: applied value and hedged judgements.
    ("While it builds on known reactivities, we believe it provides insights into the practical applications of such materials.", "chemistry"),
    ("We believe it adds value to the field by providing a new combination of UV and thermal responsiveness.", "chemistry"),
    ("We believe our work will be well-received by the target audience.", "any"),
    ("Our intention in submitting was to reach an audience interested in responsive polymers.", "any"),
    ("The tool has clear clinical relevance for emergency departments.", "medicine"),
    ("Our work introduces a combination of mechanisms that we believe offers a distinct contribution to the field.", "chemistry"),
    ("Our study integrates known reactivities in a novel configuration.", "chemistry"),
    ("We propose an entirely novel sampling design.", "social science"),
])
def test_soft_value_claims_are_removed_in_other_fields(sentence, field):
    assert check_claims(sentence, None, "").stripped is True, field


def test_topic_overlap_alone_does_not_support_a_value_claim():
    # The evidence is about degradation mechanisms but never calls the insights unique.
    result = check_claims(
        "The system offers unique insights into the degradation mechanisms.",
        "We studied the degradation mechanisms of the polymer.",
        "",
    )
    assert result.stripped is True


def test_value_claim_is_kept_when_the_author_makes_it():
    evidence = "The dual responsiveness offers unique insights into stimulus interplay (Figure 4)."
    result = check_claims("The dual responsiveness offers unique insights into stimulus interplay.", evidence, "")
    assert result.stripped is False


# ---------------------------------------------------------------------------
# Grounding: invented completed work
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    "The choice of 90 C for 50 hours was based on preliminary optimization studies.",
    "We have conducted thermal stability assessments, which indicated no significant degradation.",
    "Our preliminary data showed no significant bias in the recruitment.",
    "We have interviewed six additional participants to address saturation.",
    "We re-checked the cohort and found no significant difference.",
    # Third real run: an invented rationale for a reaction condition.
    "The choice of 90 C for 50 hours was based on optimizing the reaction yield.",
    "The sample size was chosen to detect a moderate effect.",
])
def test_invented_completed_work_is_removed(sentence):
    result = check_claims(sentence, "Cohort of 412 patients; interviews with 24 participants.", "")
    assert result.stripped is True, sentence
    assert any(f.startswith("unsupported_claim") for f in result.flags)


def test_completed_work_is_kept_when_the_author_says_it_was_done():
    evidence = "Cell lines were authenticated by STR profiling, as stated in the Methods."
    result = check_claims("We have verified the cell lines by STR profiling.", evidence, "")
    assert result.stripped is False


def test_rationale_is_kept_when_the_author_gives_it():
    evidence = "The sample size of 412 was chosen to detect a hazard ratio of 0.6 with 80% power."
    result = check_claims("The sample size was chosen to detect a hazard ratio of 0.6 with 80% power.", evidence, "")
    assert result.stripped is False


def test_neutral_sentences_are_not_touched():
    text = (
        "We thank the reviewer for this assessment. We acknowledge the concern about novelty. "
        "Following the reviewer's suggestion, we will submit the revised work to Macromolecules. "
        "We will assign each sample a unique identifier in the supplementary table. "
        "We understand the reviewer's view that the system is not sufficiently novel for this journal. "
        "We propose to sharpen the statement of contribution in the Introduction."
    )
    result = check_claims(text, None, "")
    assert result.stripped is False
    assert result.text == text


# ---------------------------------------------------------------------------
# Engine: author decisions, earlier responses, consistency pass
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_author_decision_rule_and_earlier_responses_reach_the_drafter():
    llm = CapturingLLM("We will submit to Macromolecules as the reviewers suggest.")
    await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, "Chemical Communication")
    assert len(llm.user_prompts) == 2
    assert all("THE AUTHOR'S DECISIONS ARE FINAL" in p for p in llm.system_prompts)
    assert all("NO INVENTED JUSTIFICATIONS" in p for p in llm.system_prompts)
    # The second response is drafted knowing what the first one said.
    assert "Earlier responses" not in llm.user_prompts[0]
    assert "Earlier responses" in llm.user_prompts[1] and "R1C1" in llm.user_prompts[1]
    # The consistency pass sees the author's decision and every response.
    assert "Macromolecules" in llm.consistency_prompts[0]
    assert "R1C1" in llm.consistency_prompts[0] and "R2C1" in llm.consistency_prompts[0]


@pytest.mark.asyncio
async def test_contradiction_found_by_consistency_check_is_flagged_on_both_comments():
    llm = CapturingLLM("We propose to clarify the contribution in the Introduction.")
    llm.consistency_response = json.dumps({"issues": [{
        "type": "contradiction",
        "comment_ids": ["R1C1", "R2C1"],
        "problem": "R1C1 defends Chemical Communications while R2C1 agrees to move to Macromolecules",
        # Both quotes exist: one in the drafts, one in the author's context.
        "quotes": ["We propose to clarify the contribution", "which we will meet with Macromolecules"],
    }]})
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, "Chemical Communication")
    for comment in result.comments:
        flags = [f for f in comment.tone_audit.flags if f.startswith("inconsistent")]
        assert flags, comment.comment_id
        assert comment.tone_audit.is_clean is False
    first = [f for f in result.comments[0].tone_audit.flags if f.startswith("inconsistent")][0]
    assert "(with R2C1)" in first


@pytest.mark.parametrize("quotes", [
    None,  # no quotes at all
    ["will consider a specialized journal"],  # only one
    ["We propose to clarify", "We propose to clarify"],  # the same statement twice
    ["We propose to clarify the contribution", "we will stay at Chemical Communications"],  # invented quote
])
def test_contradiction_reports_without_two_real_quotes_are_ignored(quotes):
    # Sixth real run: the checker called two responses that said the SAME thing a contradiction.
    llm = CapturingLLM("We propose to clarify the contribution in the Introduction.")
    llm.consistency_response = json.dumps({"issues": [{
        "type": "contradiction", "comment_ids": ["R1C1", "R2C1"],
        "problem": "One response agrees to consider a specialized journal, the other suggests the same",
        "quotes": quotes,
    }]})
    import asyncio
    result = asyncio.run(RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, None))
    assert not any(f.startswith("inconsistent") for c in result.comments for f in c.tone_audit.flags)


@pytest.mark.asyncio
async def test_consistency_check_failure_does_not_break_the_letter():
    llm = CapturingLLM("We propose to clarify the contribution in the Introduction.")
    llm.consistency_response = "not json at all"
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, None)
    assert len(result.comments) == 2
    assert not any(f.startswith("inconsistent") for c in result.comments for f in c.tone_audit.flags)


@pytest.mark.asyncio
async def test_unknown_comment_ids_from_consistency_check_are_ignored():
    llm = CapturingLLM("We propose to clarify the contribution in the Introduction.")
    llm.consistency_response = json.dumps({"issues": [{"comment_ids": ["R9C9"], "problem": "made up"}]})
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, None)
    assert not any(f.startswith("inconsistent") for c in result.comments for f in c.tone_audit.flags)


@pytest.mark.asyncio
async def test_llm_checker_flags_unsupported_statement_but_only_if_quote_is_real():
    llm = CapturingLLM("We acknowledge the concern. The backbone remains intact under these conditions.")
    llm.consistency_response = json.dumps({"issues": [
        {"type": "unsupported", "comment_ids": ["R1C1"], "problem": "stability is not in the evidence",
         "quote": "The backbone remains intact under these conditions"},
        # Misquote: the words are not in the draft, so it must be ignored.
        {"type": "unsupported", "comment_ids": ["R2C1"], "problem": "made up", "quote": "words that are not there"},
    ]})
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, None)
    r1, r2 = result.comments
    assert any(f.startswith("unverified_statement") for f in r1.tone_audit.flags)
    assert r1.tone_audit.is_clean is False
    assert not any(f.startswith("unverified_statement") for f in r2.tone_audit.flags)
    # Flagged, not deleted: the checker can be wrong, the author decides.
    assert "backbone remains intact" in r1.drafted_response


@pytest.mark.asyncio
async def test_checker_runs_even_for_one_comment_without_evidence():
    llm = CapturingLLM("We acknowledge the concern and will clarify it.")
    await RebuttalEngine(llm_service=llm).analyze("Reviewer 1:\n1. The novelty is limited in my view.", None, None)
    assert len(llm.consistency_prompts) == 1


@pytest.mark.asyncio
async def test_duplicate_grounding_flags_are_reported_once():
    llm = CapturingLLM("The work offers unique insights into the field. We will clarify the scope.")
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, None)
    for comment in result.comments:
        assert len(comment.tone_audit.flags) == len(set(comment.tone_audit.flags))


@pytest.mark.asyncio
async def test_real_run_response_is_cleaned_end_to_end():
    drafted = (
        "We appreciate the reviewer's recognition of the analytical work. "
        "While the system may appear as an incremental improvement, the dual-responsiveness offers unique insights "
        "into the interplay of stimuli in polymer degradation. "
        "We acknowledge the suggestion to consider a more specialized journal and are prepared to submit to Macromolecules."
    )
    llm = CapturingLLM(drafted)
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, "Macromolecules")
    for comment in result.comments:
        assert "unique insights" not in comment.drafted_response
        assert "Macromolecules" in comment.drafted_response
        assert any(f.startswith("unsupported_claim") for f in comment.tone_audit.flags)


# ---------------------------------------------------------------------------
# Round 3: repair instead of delete; venue suggestions never vanish
# ---------------------------------------------------------------------------
# The UI run that lost the journal answer: the venue sentence also carried a claim.
VENUE_SENTENCE_WITH_CLAIM = (
    "We acknowledge the concern about novelty. "
    "Given the unique aspects of our system, we would prefer to keep the manuscript at Chemical Communications "
    "rather than move to a specialized journal. "
    "We will clarify the contribution in the Introduction."
)


@pytest.mark.asyncio
async def test_repair_keeps_the_venue_answer_and_drops_only_the_claim():
    llm = CapturingLLM(VENUE_SENTENCE_WITH_CLAIM)
    llm.repair_response = json.dumps({"sentences": {
        "1": "We would prefer to keep the manuscript at Chemical Communications rather than move to a specialized journal."
    }})
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, "Chemical Communications")
    first = result.comments[0]
    assert "unique" not in first.drafted_response
    assert "keep the manuscript at Chemical Communications" in first.drafted_response
    # Sentence order is preserved around the repaired one.
    assert first.drafted_response.index("acknowledge") < first.drafted_response.index("keep the manuscript")
    assert first.drafted_response.index("keep the manuscript") < first.drafted_response.index("clarify the contribution")
    assert any(f.startswith("claim_rewritten") for f in first.tone_audit.flags)
    assert not any(f.startswith("unsupported_claim") for f in first.tone_audit.flags)
    assert first.tone_audit.is_clean is True


@pytest.mark.asyncio
async def test_repair_that_still_contains_a_claim_is_rejected():
    llm = CapturingLLM(VENUE_SENTENCE_WITH_CLAIM)
    llm.repair_response = json.dumps({"sentences": {"1": "Our novel approach is of broad interest to readers."}})
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, "Chemical Communications")
    first = result.comments[0]
    assert "broad interest" not in first.drafted_response
    assert any(f.startswith("unsupported_claim") for f in first.tone_audit.flags)


@pytest.mark.asyncio
async def test_repair_failure_falls_back_to_removal():
    llm = CapturingLLM(VENUE_SENTENCE_WITH_CLAIM)
    llm.repair_response = "the repair model is down"
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, None)
    assert "unique" not in result.comments[0].drafted_response


@pytest.mark.asyncio
async def test_unanswered_venue_suggestion_gets_an_author_decision_placeholder():
    # Exactly the UI run: no evidence, the drafts never mention the journal.
    llm = CapturingLLM("We understand the concern regarding novelty and will clarify the contribution.")
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, "Chemical communication")
    for comment in result.comments:
        assert "[AUTHOR DECISION NEEDED:" in comment.drafted_response, comment.comment_id
        assert "Chemical communication" in comment.drafted_response
        assert any(f.startswith("author_decision_needed") for f in comment.tone_audit.flags)
        assert comment.tone_audit.is_clean is False  # must be filled before sending
    assert "[AUTHOR DECISION NEEDED:" in result.export_document_md


@pytest.mark.asyncio
async def test_acknowledging_the_venue_without_deciding_still_needs_the_author():
    # Fifth real run: the draft names the suggestion but never says what the authors will do.
    llm = CapturingLLM(
        "We understand the reviewer's suggestion to consider a more specialized journal. "
        "We are committed to enhancing the manuscript."
    )
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, None, "Chemical communication")
    assert "[AUTHOR DECISION NEEDED:" in result.comments[0].drafted_response


@pytest.mark.parametrize("decision", [
    "We have decided to submit our work to Macromolecules.",
    "We would prefer to keep the manuscript at Chemical Communications.",
    "We will transfer the manuscript to a specialized polymer journal.",
    "We intend to remain with the current journal.",
])
def test_stated_venue_decisions_are_recognised(decision):
    from services.rebuttal.engine import VENUE_DECISION_RE
    assert VENUE_DECISION_RE.search(decision), decision


@pytest.mark.asyncio
async def test_no_placeholder_when_the_draft_already_answers_the_venue_point():
    llm = CapturingLLM("Following the reviewers' advice, we will submit the work to Macromolecules.")
    result = await RebuttalEngine(llm_service=llm).analyze(TRANSFER_REPORT, TRANSFER_EVIDENCE, None)
    for comment in result.comments:
        assert "AUTHOR DECISION NEEDED" not in comment.drafted_response


@pytest.mark.asyncio
async def test_no_placeholder_when_the_reviewer_does_not_raise_the_venue():
    llm = CapturingLLM("We will add the missing replicate numbers to the figure legends.")
    report = "Reviewer 1:\n1. Please report the number of biological replicates in each figure legend."
    result = await RebuttalEngine(llm_service=llm).analyze(report, None, None)
    assert "AUTHOR DECISION NEEDED" not in result.comments[0].drafted_response


@pytest.mark.asyncio
async def test_placeholder_written_by_the_model_is_flagged_for_the_author():
    llm = CapturingLLM(
        "We note the reviewer's suggestion. [AUTHOR DECISION NEEDED: whether to add the in vivo study the reviewer requests]"
    )
    report = "Reviewer 1:\n1. The authors should validate the findings in a mouse model."
    result = await RebuttalEngine(llm_service=llm).analyze(report, None, None)
    flags = result.comments[0].tone_audit.flags
    assert any("in vivo study" in f for f in flags if f.startswith("author_decision_needed"))
    assert all("DECISIONS ONLY THE AUTHOR CAN MAKE" in p for p in llm.system_prompts)
