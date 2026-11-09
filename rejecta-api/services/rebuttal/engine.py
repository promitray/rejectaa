"""services/rebuttal/engine.py
Rebuttal Writer Orchestration Engine:
Pipeline:
1. Dissect raw reviewer report into attributed, discrete CommentItems.
2. Classify every comment into a 4-category posture with ONE LLM call (regex fallback),
   then apply the evidence gate (no contesting without author evidence).
3. Synthesize a diplomatic point-by-point response via a constrained LLM call (T=0.15).
4. Ground the draft: strip novelty/priority/superiority/impact claims the author never supplied.
5. Inject safe [CITATION NEEDED: ...] hooks for CONTEST items (never hallucinating papers).
6. Audit tone, boilerplate and repetition.
7. Assemble per-reviewer summaries, revision checklist, and export markdown.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from services.llm.base import AbstractAnalysisProvider
from services.rebuttal.classifier import PostureClassification, PostureClassifier, PostureType
from services.rebuttal.dissector import CommentDissector, RawCommentItem
from services.rebuttal.grounding import (
    RemovedSentence,
    check_claims,
    first_unsupported_claim,
    unsupported_claim_flag,
)
from services.rebuttal.tone_auditor import ToneAudit, ToneAuditor

logger = logging.getLogger(__name__)

REPETITIVE_OPENING_PENALTY = 5
INCONSISTENCY_PENALTY = 15
OPENING_WORDS_COMPARED = 6
# Earlier responses shown to the drafter so later answers stay consistent with them.
MAX_EARLIER_RESPONSES = 8
EARLIER_RESPONSE_CHARS = 400

UNVERIFIED_STATEMENT_PENALTY = 10
MAX_UNVERIFIED_PER_COMMENT = 3

# "[AUTHOR DECISION NEEDED: ...]": something only the author can decide (e.g. moving journal).
DECISION_PLACEHOLDER_RE = re.compile(r"\[\s*AUTHOR\s+DECISION\s+NEEDED\s*:\s*([^\]]+)\]", re.IGNORECASE)
# Does a response actually STATE a venue decision? Mentioning "journal" while only
# acknowledging the suggestion ("we understand the suggestion to consider a specialized
# journal") is not a decision, and still needs the author.
VENUE_DECISION_RE = re.compile(
    r"\b(?:we\s+)?(?:will|shall|have\s+decided\s+to|decided\s+to|intend\s+to|plan\s+to|prefer\s+to|would\s+prefer\s+to"
    r"|would\s+like\s+to|wish\s+to|are\s+(?:submitting|transferring|moving))\s+(?:\w+\s+){0,3}?"
    r"(?:submit|resubmit|transfer|move|keep|retain|remain|stay|maintain)\b",
    re.IGNORECASE,
)

_REPAIR_SYSTEM_PROMPT = (
    "You repair sentences from a rebuttal draft. Each sentence contains a claim that the author's evidence does "
    "not support. Rewrite each sentence WITHOUT that claim and keep every other point it makes, especially "
    "answers about journal choice, planned revisions, or the reviewer's suggestions.\n"
    "Rules: do not add any new claim, result, reason, judgement or experiment. Do not use the words unique, "
    "novel, valuable, significant or impact. If nothing useful remains, return an empty string for it.\n"
    'Return ONLY JSON: {"sentences": {"<index>": "<rewritten sentence or empty string>"}}'
)

_CONSISTENCY_SYSTEM_PROMPT = (
    "You check a draft rebuttal letter. You do NOT rewrite anything. The manuscript can be from any field.\n"
    "Report only clear problems of two types:\n"
    "type 'contradiction':\n"
    "  - two responses take incompatible positions (e.g. one defends staying at a journal while another agrees "
    "to move to a different journal; one says an analysis will be added while another says it is unnecessary; "
    "different numbers for the same quantity), or\n"
    "  - a response contradicts a decision or fact the author states in their context.\n"
    "type 'unsupported':\n"
    "  - a response states a result, an experiment or check that was done, a reason for a choice, an intention, "
    "or a positive judgement of the work (novel, unique, important, adds value, practical applications, of broad "
    "interest, will be well received) that appears in NEITHER the author context NOR that reviewer's comment. "
    "Hedging with 'we believe', 'we feel' or 'may' does NOT make such a statement acceptable: report it. "
    "Commitments about future revisions ('we will add...', 'we propose to clarify...') are fine.\n"
    "Differences in wording or emphasis are NOT problems. Responses that say the same thing are NOT a "
    "contradiction. For 'contradiction' give the two exact conflicting quotes in 'quotes' (copied word for word "
    "from the responses or the author context); if you cannot quote two statements that cannot both be true, do "
    "not report it. For 'unsupported' give the exact words in 'quote'.\n"
    'Return ONLY JSON: {"issues": [{"type": "contradiction"|"unsupported", "comment_ids": [str], '
    '"problem": str, "quote": str, "quotes": [str, str]}]}. Use an empty list when there are no clear problems. '
    "Keep each problem under 30 words."
)


class ToneAuditResult(BaseModel):
    is_clean: bool
    diplomatic_score: int = Field(ge=0, le=100)
    flags: List[str] = Field(default_factory=list)


class RebuttalCommentItem(BaseModel):
    comment_id: str
    reviewer_id: str
    original_quote: str
    category: PostureType
    diplomatic_posture_label: str
    core_critique_summary: str
    drafted_response: str
    action_in_manuscript: str
    citation_hooks: List[str] = Field(default_factory=list)
    tone_audit: ToneAuditResult


class ReviewerBlock(BaseModel):
    reviewer_id: str
    total_comments: int
    category_breakdown: Dict[str, int]
    negotiation_summary: str


class RebuttalAnalyzeResponse(BaseModel):
    total_comments_identified: int
    reviewer_blocks: List[ReviewerBlock]
    category_breakdown: Dict[str, int]
    overall_negotiation_strategy: str
    comments: List[RebuttalCommentItem]
    manuscript_revision_checklist: List[str]
    export_document_md: str


class RebuttalEngine:
    """Core synthesis and orchestration engine for peer review rebuttal responses."""

    def __init__(self, llm_service: AbstractAnalysisProvider) -> None:
        self.llm = llm_service

    async def analyze(
        self,
        text: str,
        manuscript_context: str | None = None,
        journal: str | None = None,
        author_tone: Literal["collaborative", "diplomatic_firm", "formal_rigorous"] = "collaborative",
    ) -> RebuttalAnalyzeResponse:
        """Run the end-to-end rebuttal analysis and synthesis pipeline."""
        journal = (journal or "").strip() or None
        has_evidence = bool((manuscript_context or "").strip())

        # Step 1: Dissect report into structured comments
        raw_items: List[RawCommentItem] = CommentDissector.dissect(text)

        # Step 2: Classify. One LLM call for all comments; regex covers any gap or failure.
        llm_results = await PostureClassifier.classify_batch_llm(self.llm, raw_items, manuscript_context)
        classified_items: List[tuple[RawCommentItem, PostureClassification]] = []
        for item, llm_result in zip(raw_items, llm_results):
            classification = llm_result or PostureClassifier.classify(item.raw_text)
            classification = PostureClassifier.apply_evidence_gate(classification, has_evidence)
            classified_items.append((item, classification))

        # Step 3: Synthesize diplomatic responses. Drafted in order, each one seeing the
        # positions already taken, so the letter does not contradict itself.
        processed_comments: List[RebuttalCommentItem] = []
        for raw_item, classification in classified_items:
            comment_item = await self._synthesize_comment_response(
                raw_item=raw_item,
                classification=classification,
                manuscript_context=manuscript_context,
                journal=journal,
                author_tone=author_tone,
                earlier_responses=processed_comments,
            )
            processed_comments.append(comment_item)

        self._flag_repetitive_openings(processed_comments)
        await self._flag_inconsistencies(processed_comments, manuscript_context)

        # Step 4: Build category breakdown & reviewer blocks
        category_breakdown: Dict[str, int] = {
            "agree_and_amend": 0,
            "clarify_misconception": 0,
            "partial_agree_bounded": 0,
            "contest_with_evidence": 0,
        }
        for c in processed_comments:
            category_breakdown[c.category] = category_breakdown.get(c.category, 0) + 1

        reviewer_map: Dict[str, List[RebuttalCommentItem]] = {}
        for c in processed_comments:
            reviewer_map.setdefault(c.reviewer_id, []).append(c)

        reviewer_blocks: List[ReviewerBlock] = []
        for r_id, r_comments in reviewer_map.items():
            r_cats: Dict[str, int] = {}
            for rc in r_comments:
                r_cats[rc.category] = r_cats.get(rc.category, 0) + 1

            agree_count = r_cats.get("agree_and_amend", 0)
            contest_count = r_cats.get("contest_with_evidence", 0)
            clarify_count = r_cats.get("clarify_misconception", 0)
            partial_count = r_cats.get("partial_agree_bounded", 0)

            summary = (
                f"{len(r_comments)} total points: "
                f"{agree_count} revisions agreed, {clarify_count} clarified, "
                f"{partial_count} bounded, {contest_count} contested with evidence."
            )

            reviewer_blocks.append(
                ReviewerBlock(
                    reviewer_id=r_id,
                    total_comments=len(r_comments),
                    category_breakdown=r_cats,
                    negotiation_summary=summary,
                )
            )

        # Step 5: Overall negotiation strategy summary (wording follows the actual counts)
        strategy_summary = self._build_strategy_summary(category_breakdown)

        # Step 6: Derive revision checklist from AGREE and PARTIAL items only
        checklist: List[str] = []
        for c in processed_comments:
            if c.category in ("agree_and_amend", "partial_agree_bounded") and c.action_in_manuscript:
                checklist.append(f"[{c.comment_id}] {c.action_in_manuscript}")

        # Step 7: Compile publication-ready export document
        export_md = self._compile_export_document(
            journal=journal,
            strategy_summary=strategy_summary,
            reviewer_blocks=reviewer_blocks,
            comments=processed_comments,
        )

        return RebuttalAnalyzeResponse(
            total_comments_identified=len(processed_comments),
            reviewer_blocks=reviewer_blocks,
            category_breakdown=category_breakdown,
            overall_negotiation_strategy=strategy_summary,
            comments=processed_comments,
            manuscript_revision_checklist=checklist,
            export_document_md=export_md,
        )

    @staticmethod
    def _build_strategy_summary(category_breakdown: Dict[str, int]) -> str:
        """Describe the stance from the real category counts, never from a fixed template."""
        agree = category_breakdown.get("agree_and_amend", 0)
        clarify = category_breakdown.get("clarify_misconception", 0)
        bounded = category_breakdown.get("partial_agree_bounded", 0)
        contest = category_breakdown.get("contest_with_evidence", 0)

        parts: List[str] = []
        if agree:
            parts.append(f"{agree} point(s) where the authors propose a revision")
        if clarify:
            parts.append(f"{clarify} point(s) clarified")
        if bounded:
            parts.append(f"{bounded} point(s) answered with a bounded position")
        if contest:
            parts.append(f"{contest} point(s) contested with evidence")
        detail = "; ".join(parts) if parts else "no actionable points identified"

        if contest > agree:
            stance = "Firm scientific defence stance"
        elif contest == 0:
            stance = "Collaborative revision stance"
        else:
            stance = "Balanced scholarly negotiation"
        return f"{stance}: {detail}."

    @staticmethod
    def _opening_key(response: str) -> str:
        words = re.sub(r"[^a-z0-9\s]", "", response.lower()).split()
        return " ".join(words[:OPENING_WORDS_COMPARED])

    def _flag_repetitive_openings(self, comments: List[RebuttalCommentItem]) -> None:
        """Flag responses that start identically; interchangeable openings read as boilerplate."""
        groups: Dict[str, List[RebuttalCommentItem]] = {}
        for comment in comments:
            key = self._opening_key(comment.drafted_response)
            if key:
                groups.setdefault(key, []).append(comment)
        for key, group in groups.items():
            if len(group) < 2:
                continue
            for comment in group:
                comment.tone_audit.flags.append(f"repetitive_opening: '{key}'")
                comment.tone_audit.diplomatic_score = max(
                    0, comment.tone_audit.diplomatic_score - REPETITIVE_OPENING_PENALTY
                )

    async def _flag_inconsistencies(
        self, comments: List[RebuttalCommentItem], manuscript_context: str | None
    ) -> None:
        """One LLM pass over the whole letter that flags (never rewrites) contradictions.

        Catches what per-comment drafting cannot see: two answers taking opposite positions,
        or an answer arguing against a decision the author stated in their context.
        Failures are ignored; the letter is still returned.
        """
        # Runs even for one comment with no evidence: that is when invented facts are most likely.
        if not comments:
            return
        letter = [
            {"comment_id": c.comment_id, "reviewer": c.reviewer_id, "reviewer_comment": c.original_quote,
             "response": c.drafted_response, "action": c.action_in_manuscript}
            for c in comments
        ]
        user_prompt = (
            f"Author context (their own decisions and facts; may be empty):\n"
            f"{(manuscript_context or '').strip() or '[NONE]'}\n\n"
            f"Draft responses (JSON):\n{json.dumps(letter, ensure_ascii=False)}\n\n"
            "Return the JSON object now."
        )
        try:
            raw = await self.llm.generate(
                system_prompt=_CONSISTENCY_SYSTEM_PROMPT, user_prompt=user_prompt, temperature=0.0
            )
            issues = self._clean_and_parse_json(raw).get("issues", [])
        except Exception as exc:
            logger.warning("Consistency check skipped: %s", exc)
            return
        if not isinstance(issues, list):
            return

        by_id = {c.comment_id: c for c in comments}
        unverified_count: Dict[str, int] = {}
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            problem = str(issue.get("problem", "")).strip()[:200]
            ids = [str(i) for i in (issue.get("comment_ids") or []) if str(i) in by_id]
            if not problem or not ids:
                continue
            if str(issue.get("type", "contradiction")).lower() == "unsupported":
                quote = str(issue.get("quote", "")).strip()[:200]
                for cid in ids:
                    comment = by_id[cid]
                    # Only flag words that are really in the draft; the checker can misquote.
                    if quote and quote.lower() not in comment.drafted_response.lower() \
                            and quote.lower() not in comment.action_in_manuscript.lower():
                        continue
                    if unverified_count.get(cid, 0) >= MAX_UNVERIFIED_PER_COMMENT:
                        continue
                    unverified_count[cid] = unverified_count.get(cid, 0) + 1
                    detail = f"'{quote}' - {problem}" if quote else problem
                    comment.tone_audit.flags.append(f"unverified_statement: {detail}")
                    comment.tone_audit.is_clean = False
                    comment.tone_audit.diplomatic_score = max(
                        0, comment.tone_audit.diplomatic_score - UNVERIFIED_STATEMENT_PENALTY
                    )
                continue
            # A contradiction is only accepted with two distinct quotes that really are in the
            # letter (or the author's context). This stops "both responses say X" being reported.
            if not self._contradiction_is_quoted(issue.get("quotes"), [by_id[c] for c in ids], manuscript_context):
                logger.info("Ignored unquoted contradiction report: %s", problem)
                continue
            for cid in ids:
                others = [o for o in ids if o != cid]
                suffix = f" (with {', '.join(others)})" if others else ""
                audit = by_id[cid].tone_audit
                audit.flags.append(f"inconsistent: {problem}{suffix}")
                audit.is_clean = False
                audit.diplomatic_score = max(0, audit.diplomatic_score - INCONSISTENCY_PENALTY)

    @staticmethod
    def _contradiction_is_quoted(
        quotes: Any, comments: List[RebuttalCommentItem], manuscript_context: str | None
    ) -> bool:
        if not isinstance(quotes, list) or len(quotes) < 2:
            return False
        cleaned = [re.sub(r"\s+", " ", str(q)).strip().lower() for q in quotes[:2]]
        if any(len(q) < 8 for q in cleaned) or cleaned[0] == cleaned[1]:
            return False
        haystack = " ".join(
            [*(f"{c.drafted_response} {c.action_in_manuscript}" for c in comments), manuscript_context or ""]
        )
        haystack = re.sub(r"\s+", " ", haystack).lower()
        return all(q in haystack for q in cleaned)

    async def _synthesize_comment_response(
        self,
        raw_item: RawCommentItem,
        classification: PostureClassification,
        manuscript_context: str | None,
        journal: str | None,
        author_tone: str,
        earlier_responses: List[RebuttalCommentItem] | None = None,
    ) -> RebuttalCommentItem:
        """Synthesize response, ground it, audit tone, and attach citation hooks for one comment."""
        has_evidence = bool((manuscript_context or "").strip())
        system_prompt = self._build_system_prompt(
            category=classification.category,
            journal=journal,
            author_tone=author_tone,
            has_evidence=has_evidence,
        )
        user_prompt = self._build_user_prompt(
            raw_text=raw_item.raw_text,
            reviewer_id=raw_item.reviewer_id,
            manuscript_context=manuscript_context,
            suggested_strategy=classification.suggested_strategy_notes,
            reviewer_requests=classification.reviewer_requests,
            earlier_responses=earlier_responses,
        )

        try:
            raw_llm = await self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.15)
            # Parse response
            parsed = self._clean_and_parse_json(raw_llm)
            core_summary = parsed.get("core_critique_summary", raw_item.raw_text[:80] + "...")
            drafted_resp = parsed.get("drafted_response", "")
            action_text = parsed.get("action_in_manuscript", "")
        except Exception as exc:
            logger.warning("LLM response failed or invalid JSON: %s. Using deterministic fallback.", exc)
            core_summary = f"Reviewer concern regarding: {raw_item.raw_text[:60]}..."
            drafted_resp = self._deterministic_fallback_response(raw_item.raw_text, classification.category)
            action_text = self._deterministic_fallback_action(classification.category)

        # Deterministic Grounding & Anti-Fabrication Sanitizer:
        # 1. Scrub any fabricated line numbers (e.g. '(lines 310–325)', 'line 140')
        line_num_pattern = r"\s*\((?:lines?|pp\.?|pages?)\s*\d+[\s–\-to]+\d+\)"
        action_text = re.sub(line_num_pattern, "", action_text, flags=re.IGNORECASE).strip()
        drafted_resp = re.sub(line_num_pattern, "", drafted_resp, flags=re.IGNORECASE).strip()

        # 2. Take out claims the author never supplied. A sentence with such a claim is first
        #    REPAIRED (claim dropped, the rest kept, e.g. an answer about journal choice); only
        #    if the repair fails or still carries a claim is the sentence removed.
        drafted_resp, response_flags = await self._ground_and_repair(drafted_resp, manuscript_context, raw_item.raw_text)
        action_text, action_flags = await self._ground_and_repair(action_text, manuscript_context, raw_item.raw_text)
        # The same claim can be removed from both the response and the action: report it once.
        grounding_flags = list(dict.fromkeys([*response_flags, *action_flags]))
        if not drafted_resp:
            drafted_resp = self._deterministic_fallback_response(raw_item.raw_text, classification.category)
        if not action_text:
            action_text = self._deterministic_fallback_action(classification.category)

        # 3. Convert completed assertions ('Added...', 'Revised...') into proposed commitments if author hasn't finalized
        if not action_text.lower().startswith("propose"):
            action_text = re.sub(r"^(?:Added|We have added|Included)\b", "Propose to add", action_text, flags=re.IGNORECASE)
            action_text = re.sub(r"^(?:Revised|We have revised|Updated)\b", "Propose to revise", action_text, flags=re.IGNORECASE)

        # 4. Special handling for Editor administrative directives
        if raw_item.reviewer_id.lower() == "editor" and any(w in raw_item.raw_text.lower() for w in ["major revision", "point-by-point", "distinguish changes"]):
            drafted_resp = (
                "We thank the Editor for this guidance and for managing the peer-review process. "
                "In accordance with your directive, we have prepared a comprehensive, point-by-point response "
                "addressing all reviewer concerns, clearly distinguishing proposed manuscript revisions from existing findings."
            )
            action_text = "Prepared point-by-point response adhering to the Editor's formatting guidelines."
            core_summary = "Editor requested a formal point-by-point response distinguishing proposed revisions."
            grounding_flags = []
        else:
            # 5. A venue suggestion must never vanish silently. If the reviewer suggests another
            #    journal and the draft says nothing about it, ask the author to decide.
            drafted_resp = self._ensure_venue_decision(drafted_resp, raw_item.raw_text, journal)

        # Removed or rewritten claims are no longer in the text, so they are information for
        # the author, not a fault in the final draft: no score penalty, no warning mark.
        # What still needs the author (a decision placeholder) does mark the card.
        decisions = DECISION_PLACEHOLDER_RE.findall(drafted_resp)
        decision_flags = [f"author_decision_needed: {d.strip()}" for d in decisions]
        audit: ToneAudit = ToneAuditor.audit(drafted_resp, classification.reviewer_requests)
        score = audit.diplomatic_score

        # Citation hook extraction: if CONTEST or PARTIAL, ensure placeholder hooks are identified
        citation_hooks: List[str] = []
        if classification.category == "contest_with_evidence":
            # Extract any [CITATION NEEDED: ...] from text
            found_hooks = re.findall(r"\[\s*CITATION\s+NEEDED\s*:\s*([^\]]+)\]", drafted_resp, re.IGNORECASE)
            if found_hooks:
                citation_hooks = [f"[CITATION NEEDED: {h.strip()}]" for h in found_hooks]
            else:
                # Guarantee at least one safe citation slot
                citation_hooks = ["[CITATION NEEDED: Authoritative literature reference supporting this point]"]
                # Append to response if not present
                if "[CITATION NEEDED" not in drafted_resp:
                    drafted_resp += f" (see e.g., {citation_hooks[0]})."
        elif classification.category == "partial_agree_bounded":
            found_hooks = re.findall(r"\[\s*CITATION\s+NEEDED\s*:\s*([^\]]+)\]", drafted_resp, re.IGNORECASE)
            if found_hooks:
                citation_hooks = [f"[CITATION NEEDED: {h.strip()}]" for h in found_hooks]

        return RebuttalCommentItem(
            comment_id=raw_item.comment_id,
            reviewer_id=raw_item.reviewer_id,
            original_quote=raw_item.raw_text,
            category=classification.category,
            diplomatic_posture_label=classification.diplomatic_posture_label,
            core_critique_summary=core_summary,
            drafted_response=drafted_resp,
            action_in_manuscript=action_text,
            citation_hooks=citation_hooks,
            tone_audit=ToneAuditResult(
                is_clean=audit.is_clean and not decision_flags,
                diplomatic_score=score,
                flags=[*audit.flags, *grounding_flags, *decision_flags],
            ),
        )

    async def _ground_and_repair(
        self, text: str, manuscript_context: str | None, reviewer_text: str
    ) -> tuple[str, List[str]]:
        """Ground `text`; rewrite (not delete) sentences whose only problem is an unsupported claim.

        Returns the cleaned text and flags describing what happened to each claim.
        """
        result = check_claims(text, manuscript_context, reviewer_text)
        other_flags = [f for f in result.flags if not f.startswith("unsupported_claim")]
        if not result.removed:
            return result.text, other_flags

        repaired = await self._repair_sentences(result.removed, manuscript_context)
        replacement: Dict[int, str] = {}
        flags: List[str] = []
        for item in result.removed:
            candidate = repaired.get(item.index, "").strip()
            if candidate and first_unsupported_claim(candidate, manuscript_context) is None:
                replacement[item.index] = candidate
                flags.append(f"claim_rewritten ({item.kind}): '{item.snippet}' taken out, rest of the sentence kept")
            else:
                flags.append(unsupported_claim_flag(item.kind, item.snippet))

        removed_idx = {item.index for item in result.removed}
        rebuilt = [
            replacement.get(i, "") if i in removed_idx else sentence
            for i, sentence in enumerate(result.sentences)
        ]
        return " ".join(s for s in rebuilt if s).strip(), [*flags, *other_flags]

    async def _repair_sentences(
        self, removed: List[RemovedSentence], manuscript_context: str | None
    ) -> Dict[int, str]:
        """Ask the model to drop only the unsupported claim from each sentence. {} on any failure."""
        payload = [{"index": r.index, "sentence": r.sentence, "remove_claim": r.snippet} for r in removed]
        user_prompt = (
            f"Author context (the only allowed source of facts; may be empty):\n"
            f"{(manuscript_context or '').strip() or '[NONE]'}\n\n"
            f"Sentences to repair (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return the JSON object now."
        )
        try:
            raw = await self.llm.generate(system_prompt=_REPAIR_SYSTEM_PROMPT, user_prompt=user_prompt, temperature=0.0)
            data = self._clean_and_parse_json(raw).get("sentences", {})
        except Exception as exc:
            logger.warning("Claim repair skipped: %s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[int, str] = {}
        for key, value in data.items():
            try:
                out[int(key)] = str(value or "")
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _ensure_venue_decision(response: str, reviewer_text: str, journal: str | None) -> str:
        """Add a decision placeholder when a venue suggestion is otherwise left unanswered."""
        suggests_venue = any(re.search(p, reviewer_text.lower()) for p in PostureClassifier._VENUE_PATTERNS)
        if not suggests_venue or VENUE_DECISION_RE.search(response) or DECISION_PLACEHOLDER_RE.search(response):
            return response
        current = journal or "the current journal"
        placeholder = (
            f"[AUTHOR DECISION NEEDED: state whether you will keep the submission at {current} "
            "or move to a more specialized journal, as the reviewer suggests]"
        )
        return f"{response} {placeholder}".strip()

    def _build_system_prompt(
        self,
        category: PostureType,
        journal: str | None,
        author_tone: str,
        has_evidence: bool = True,
    ) -> str:
        """Build precise system instructions based on diplomatic category."""
        venue = f"for a submission to '{journal}'" if journal else "for a journal submission"
        field_note = (
            f"Use the terminology and conventions of the reviewer's own field as they appear in the comment, "
            f"and of {journal}." if journal
            else "Use the terminology and conventions of the reviewer's own field as they appear in the comment."
        )
        evidence_rule = (
            "5. AUTHOR EVIDENCE IS THE ONLY SOURCE OF FACTS: state results, comparisons and numbers ONLY if they appear "
            "in the author's evidence. Do not add novelty, priority, superiority or application claims "
            "(e.g. 'not previously reported', 'first demonstration', 'outperforms', 'potential applications') "
            "unless the evidence says so in those words.\n"
            if has_evidence else
            "5. NO AUTHOR EVIDENCE WAS SUPPLIED: make NO claims about results, novelty, priority, superiority, "
            "performance or applications. Only acknowledge the concern and commit to clarifications, additions or "
            "scope statements in the manuscript. Never describe what the work 'demonstrates', 'offers' or 'enables'.\n"
        )
        base = (
            f"You are a Senior Academic Peer-Review Negotiation Specialist drafting an author's formal rebuttal letter "
            f"{venue}. Tone directive: '{author_tone}'. The manuscript can be from any field; do not assume one. "
            f"{field_note}\n\n"
            "STRICT SCIENTIFIC INVARIANTS & GROUNDING CONSTRAINTS (NON-NEGOTIABLE):\n"
            "1. PROPOSED COMMITMENT ONLY, NEVER COMPLETED ACTION: The author is preparing a rebuttal draft. "
            "NEVER claim that additional experiments have already been conducted or that the manuscript is already revised. "
            "Use proposed/future commitment phrasing: 'We propose to clarify...', 'We will incorporate...', 'We commit to presenting...'.\n"
            "2. ZERO FABRICATION OF LINE NUMBERS OR SECTIONS: NEVER invent specific line numbers (e.g. 'lines 230–245') "
            "or fabricated section numbers unless explicitly provided in the author's context. Refer generally to sections ('in the Methods section').\n"
            "3. STRICT CONTEXT BOUNDS: NEVER invent unverified empirical findings, mechanisms, parameters or conditions. "
            "State only facts that appear in the author's context.\n"
            "4. SAFE CITATION PLACEHOLDERS ONLY: For contested points, NEVER invent author names, paper titles, or years. "
            "You MUST insert an exact placeholder slot: '[CITATION NEEDED: <exact evidence description>]'.\n"
            f"{evidence_rule}"
            "6. ADDRESS EVERY REQUEST: respond to each item under 'Concrete reviewer requests', including suggestions "
            "such as submitting to a different journal. Never reduce a point to a generic apology.\n"
            "7. VARY THE OPENING: do not start with an apology unless the manuscript wording was genuinely unclear, and "
            "do not open every response with the same sentence.\n"
            "8. THE AUTHOR'S DECISIONS ARE FINAL: anything the author states in their context (target venue, what they "
            "will or will not change, how they position the work) overrides everything else, including the target "
            "journal named above. Never argue against it or contradict it. If the context names a different journal "
            "than the one above, follow the context.\n"
            "9. NO INVENTED JUSTIFICATIONS: do not invent reasons for choosing a journal (readership, scope, impact), "
            "do not describe the work as offering 'unique' or 'valuable' insights, a 'novel' approach or 'broad "
            "interest', and do not say experiments, checks or studies were done, unless the author's context says so.\n"
            "10. STAY CONSISTENT: never take a position that conflicts with the earlier responses listed in the prompt.\n"
            "11. DECISIONS ONLY THE AUTHOR CAN MAKE: if the reviewer asks for something only the authors can decide "
            "(moving to another journal, adding major new experiments or data collection) and the author's context does "
            "not say what they decided, do not decide for them and do not skip it: write "
            "'[AUTHOR DECISION NEEDED: <the choice to make>]' in the response.\n\n"
            "OUTPUT FORMAT: You MUST return a single valid JSON object with keys: "
            "'core_critique_summary', 'drafted_response', 'action_in_manuscript'. No preamble, no fences.\n\n"
        )

        if category == "agree_and_amend":
            rules = (
                "POSTURE: AGREE AND AMEND (Valid critique).\n"
                "1. Briefly thank the reviewer for the specific point raised.\n"
                "2. Agree with the scientific observation without excuses.\n"
                "3. Commit to adding the requested detail, comparison or correction, or to presenting existing results more prominently.\n"
                "4. In 'action_in_manuscript', state the proposed action clearly without fabricated line numbers "
                "(e.g. 'Propose to add the requested characterization details to the Methods section')."
            )
        elif category == "clarify_misconception":
            rules = (
                "POSTURE: CLARIFY MISCONCEPTION (Reviewer overlooked or misunderstood existing material).\n"
                "1. NEVER blame the reviewer (do NOT write 'the reviewer missed' or 'clearly failed').\n"
                "2. Take responsibility for any ambiguity in a natural way, only where it truly applies.\n"
                "3. Provide a clear explanation relying STRICTLY on supplied context facts.\n"
                "4. In 'action_in_manuscript', state the proposed clarifying text (e.g. 'Propose to clarify this point in the Methods section')."
            )
        elif category == "partial_agree_bounded":
            rules = (
                "POSTURE: PARTIAL AGREE BOUNDED (Critique has merit but cannot be fully met: out-of-scope or infeasible "
                "request, a novelty or significance judgement, or a journal-fit suggestion).\n"
                "1. Respectfully acknowledge the value of the reviewer's concern.\n"
                "2. For an infeasible request, explain the scope limit and offer a concrete bounded alternative.\n"
                "3. For a novelty, significance or journal-fit concern, position the contribution using ONLY facts from the "
                "author's evidence and give the authors' reasons for the target venue, without comparative or priority claims.\n"
                "4. In 'action_in_manuscript', propose the concrete change (e.g. 'Propose to sharpen the statement of "
                "contribution in the Introduction' or 'Propose a scope note in the Discussion')."
            )
        else:  # contest_with_evidence
            rules = (
                "POSTURE: CONTEST WITH EVIDENCE (Reviewer's comment relies on an incorrect assumption or factual error).\n"
                "1. Validate the reviewer's critical perspective first in 1 polite sentence.\n"
                "2. Respectfully defend the authors' position using ONLY the author's supplied evidence.\n"
                "3. For anything that needs an external source, insert an exact placeholder: "
                "'[CITATION NEEDED: <description of the reference needed>]'.\n"
                "4. In 'action_in_manuscript', propose clarifying the point and citing the relevant source."
            )

        return base + rules

    def _build_user_prompt(
        self,
        raw_text: str,
        reviewer_id: str,
        manuscript_context: str | None,
        suggested_strategy: str,
        reviewer_requests: List[str] | None = None,
        earlier_responses: List[RebuttalCommentItem] | None = None,
    ) -> str:
        earlier_str = ""
        if earlier_responses:
            shown = earlier_responses[-MAX_EARLIER_RESPONSES:]
            listed = "\n".join(
                f"- {c.comment_id} ({c.reviewer_id}): {c.drafted_response[:EARLIER_RESPONSE_CHARS]}" for c in shown
            )
            earlier_str = f"\nEarlier responses in this letter (stay consistent with these):\n{listed}\n"
        ctx_str = (
            f"\nAuthor's Manuscript Context & Key Findings:\n{manuscript_context}\n"
            if (manuscript_context or "").strip()
            else "\nAuthor's Manuscript Context & Key Findings: [NONE SUPPLIED]\n"
        )
        requests_str = ""
        if reviewer_requests:
            listed = "\n".join(f"- {r}" for r in reviewer_requests)
            requests_str = f"\nConcrete reviewer requests (address each one):\n{listed}\n"
        return (
            f"Reviewer / Editor: {reviewer_id}\n"
            f"Original Reviewer Comment:\n\"{raw_text}\"\n"
            f"{ctx_str}"
            f"{requests_str}"
            f"{earlier_str}"
            f"\nStrategy Directive:\n{suggested_strategy}\n\n"
            "Generate the formal JSON response block now:"
        )

    def _clean_and_parse_json(self, raw_str: str) -> Dict[str, Any]:
        """Strip markdown fences and parse JSON safely."""
        text = raw_str.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)

    def _deterministic_fallback_response(self, text: str, category: PostureType) -> str:
        """Safe, field-neutral responses if LLM output fails or is removed by the grounding check."""
        if category == "agree_and_amend":
            return (
                "We thank the reviewer for this constructive observation and agree with the assessment. "
                "We propose to address it in the revised manuscript."
            )
        elif category == "clarify_misconception":
            return (
                "We appreciate the reviewer raising this point. "
                "We propose to make it explicit in the revised manuscript."
            )
        elif category == "partial_agree_bounded":
            return (
                "We appreciate the reviewer's concern and acknowledge its importance. "
                "We propose to state clearly in the revised manuscript what the present work does and does not address."
            )
        else:
            return (
                "We appreciate the reviewer's perspective on this point. "
                "However, the standard approach in the field supports our formulation "
                "[CITATION NEEDED: authoritative reference supporting the standard approach]. "
                "We propose to clarify this in the revised text."
            )

    def _deterministic_fallback_action(self, category: PostureType) -> str:
        if category == "agree_and_amend":
            return "Propose to revise the relevant section to address this point."
        elif category == "clarify_misconception":
            return "Propose to clarify this point in the relevant section."
        elif category == "partial_agree_bounded":
            return "Propose to add a note on the scope of the present work in the Discussion."
        else:
            return "Propose to clarify the standard approach and cite the relevant literature."

    def _compile_export_document(
        self,
        journal: str | None,
        strategy_summary: str,
        reviewer_blocks: List[ReviewerBlock],
        comments: List[RebuttalCommentItem],
    ) -> str:
        """Compile complete academic point-by-point rebuttal letter in clean markdown."""
        has_editor = any(c.reviewer_id.lower() == "editor" for c in comments)
        salutation = "Dear Editor and Reviewers," if has_editor else "Dear Reviewers,"
        thanked = "the Editor and the Reviewers" if has_editor else "the Reviewers"

        lines: List[str] = [
            "# Point-by-Point Response to Reviewers",
            f"**Target Journal**: {journal or 'not specified'}",
            "",
            salutation,
            "",
            (
                f"We thank {thanked} for the time and care they gave our manuscript. "
                "Below we respond to each comment in turn and describe the revisions we propose to make."
            ),
            "",
            f"*{strategy_summary}*",
            "",
            "---",
            "",
        ]

        # Group comments by reviewer
        rev_map: Dict[str, List[RebuttalCommentItem]] = {}
        for c in comments:
            rev_map.setdefault(c.reviewer_id, []).append(c)

        for rev_id, rev_comments in rev_map.items():
            lines.extend([
                f"## Response to {rev_id}",
                "",
            ])
            for c in rev_comments:
                lines.extend([
                    f"### Comment {c.comment_id} ({c.diplomatic_posture_label})",
                    f"> *\"{c.original_quote.strip()}\"*",
                    "",
                    "**Author Response:**",
                    c.drafted_response.strip(),
                    "",
                    "**Action in Revised Manuscript:**",
                    c.action_in_manuscript.strip(),
                    "",
                ])
                if c.citation_hooks:
                    hooks_str = ", ".join(f"`{h}`" for h in c.citation_hooks)
                    lines.extend([
                        f"*Pending Evidence Verification:* {hooks_str}",
                        "",
                    ])
                lines.append("---")
                lines.append("")

        return "\n".join(lines)
