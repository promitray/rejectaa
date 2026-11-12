"""services/calibrator/engine.py
Voice Calibrator v2.5 orchestration engine.
Features hard output gatekeeper, reviewer feedback guidance generation, and verified invariant mapping.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel

from services.calibrator.classifier import ClassificationResult, InputTypeClassifier
from services.calibrator.corpus import JournalCorpusService
from services.calibrator.vault import InvariantBundle, InvariantRecord, ScientificVault
from services.calibrator.verifier import CalibrationAuditor
from services.calibrator.quality import check_output, citation_tokens, fix_citation_punctuation, mode_rules
from config import settings
from services.llm.base import AbstractAnalysisProvider

logger = logging.getLogger(__name__)

# What the editor's note says of an issue about something the text lacks (the app writes it; see rejecta-frontend/src/focus.ts)
MISSING_MARK = "missing from the text"


class CalibrationResult(BaseModel):
    """Execution output from VoiceCalibratorService."""
    calibrated_text: str | None
    original_text: str
    status: Literal["verified", "blocked", "guidance", "no_invariants"]
    input_type: Literal["manuscript_draft", "reviewer_critique"]
    invariants_verified: bool
    total_detected_invariants: int
    total_restored_invariants: int
    word_count_original: int
    word_count_calibrated: int
    synthetic_markers_removed: int
    intensity_applied: str
    audit_notes: List[str]
    block_reasons: List[str] = []
    guidance_items: List[str] = []
    # For writing the result back into a Word file: the model's output with every protected token still in it,
    # and what each token stands for.
    calibrated_masked_text: str | None = None
    vault_values: Dict[str, str] = {}


PLACEHOLDER_OPEN = "[AUTHOR TO ADD"
_PLACEHOLDER = re.compile(r"\s*\[AUTHOR TO ADD[^\]]*\][.,;]?")  # with the space before it and the stop after it
_ISSUE_LINE = re.compile(r"^(\d+)\. \[([^\]]*)\]", re.MULTILINE)


def _without_placeholders(text: str) -> str:
    """The text with each [AUTHOR TO ADD: ...] taken out: a placeholder asks for content, it is not content."""
    return re.sub(r"\s{2,}", " ", _PLACEHOLDER.sub("", text))


def _issues(focus: str | None) -> list[tuple[int, str]]:
    """(number, its tags) of each numbered issue in the editor's note."""
    return [(int(n), tags) for n, tags in _ISSUE_LINE.findall(focus or "")]


def _missing_items(focus: str | None) -> int:
    return sum(1 for _, tags in _issues(focus) if MISSING_MARK in tags)


def _unreported_issues(focus: str | None, notes: list[Any]) -> list[int]:
    """Issue numbers the Calibrator gave no "Issue N: ..." entry for."""
    reported = {int(m.group(1)) for n in notes if (m := re.match(r"\s*Issues?\s*(\d+)", str(n), re.IGNORECASE))}
    return [number for number, _ in _issues(focus) if number not in reported]


class VoiceCalibratorService:
    """Orchestrates scientific invariant extraction, LLM register transformation, and audit."""

    def __init__(self, llm_service: AbstractAnalysisProvider) -> None:
        self.llm = llm_service

    async def calibrate(
        self,
        text: str,
        journal_name: str,
        field: str = "Multidisciplinary",
        intensity: Literal["polish", "rewrite", "restructure"] = "rewrite",
        focus: str | None = None,
        extra_invariants: Dict[str, str] | None = None,
    ) -> CalibrationResult:
        """Calibrate input text with hard output verification or reviewer guidance handling.

        `focus` is the editor's note on a manuscript section (issue and fix from the review). When it is given the
        text is known to be a manuscript section, so it is never read as a reviewer's critique.

        `extra_invariants` are tokens already placed in `text` by the caller (formatted runs of a Word paragraph,
        such as superscript citations). They are protected and verified like the vault's own tokens.
        """
        # the note keeps its lines (a numbered list of issues); runs of spaces inside a line are collapsed
        focus = "\n".join(" ".join(line.split()) for line in focus.splitlines() if line.strip()) if focus else None
        # Step 0: Input Type Classification (skipped for a section: we know what it is)
        classification: ClassificationResult | None = None if focus else InputTypeClassifier.classify(text)

        # Path A: Reviewer Critique Detected -> Do NOT fabricate research prose; Generate Structured Guidance
        if classification is not None and classification.is_reviewer_feedback:
            return await self._handle_reviewer_guidance(text, journal_name, field)

        # Path B: Standard Manuscript Draft Calibration
        # Step 1: Secure Invariants into the Vault with Span Records
        vault_bundle: InvariantBundle = ScientificVault.mask(text)
        if extra_invariants:
            vault_bundle = InvariantBundle(
                masked_text=vault_bundle.masked_text,
                vault={**vault_bundle.vault, **extra_invariants},
                records=vault_bundle.records
                + [InvariantRecord(token, value, "format", -1, -1) for token, value in extra_invariants.items()],
                token_count=vault_bundle.token_count + len(extra_invariants),
            )

        # Step 2: Fetch Journal Register Profile
        register = await JournalCorpusService.get_journal_register_profile(journal_name)

        # Step 3: Construct Constrained Calibration Prompt
        system_prompt = self._build_system_prompt(
            register, intensity, field, has_invariants=(vault_bundle.token_count > 0), focus=focus
        )
        user_prompt = self._build_user_prompt(vault_bundle.masked_text, intensity)
        categories = {r.token_id: r.category for r in vault_bundle.records}
        cite_tokens = citation_tokens(vault_bundle.vault, categories)

        def plain(masked: str) -> str:
            return re.sub(r"__REJ_INV_\d{4}__", lambda m: vault_bundle.vault.get(m.group(0), ""), masked)

        # Step 4: Execute LLM Call; check it in code (and with a meaning check); send it back once with the reasons
        raw_calibrated = ""
        notes: List[str] = []
        problems: List[str] = []
        current_user_prompt = user_prompt
        for attempt in range(2):
            try:
                response_json_str = await self.llm.generate(
                    system_prompt=system_prompt,
                    user_prompt=current_user_prompt,
                    temperature=0.10 if attempt > 0 else 0.15,
                )
                parsed = self.llm.parse_json_response(response_json_str)
                raw_calibrated = str(parsed.get("calibrated_masked_text", "") or "")
                notes = parsed.get("modifications", [])
                if not isinstance(notes, list):
                    notes = [str(notes)]
            except Exception as e:
                logger.error("VoiceCalibrator LLM generation failed: %s", e)
                return CalibrationResult(
                    calibrated_text=None,
                    original_text=text,
                    status="blocked",
                    input_type="manuscript_draft",
                    invariants_verified=False,
                    total_detected_invariants=vault_bundle.token_count,
                    total_restored_invariants=0,
                    word_count_original=len(text.split()),
                    word_count_calibrated=0,
                    synthetic_markers_removed=0,
                    intensity_applied=intensity,
                    audit_notes=[f"Generation error: {e}"],
                    block_reasons=[f"Model execution error: {e}"],
                )

            # citation numbers go back to the side of the punctuation the author put them on
            raw_calibrated = fix_citation_punctuation(vault_bundle.masked_text, raw_calibrated, cite_tokens)
            missing = [t for t in vault_bundle.vault.keys() if t not in raw_calibrated]
            problems = []
            if missing:
                problems.append(
                    f"You omitted protected token(s) {', '.join(missing)}. You MUST keep all original tokens in your output!"
                )
            else:
                problems.extend(check_output(plain(vault_bundle.masked_text), _without_placeholders(plain(raw_calibrated)), intensity))
                # a second model call compares the meaning (off with CALIBRATOR_MEANING_CHECK=false)
                if settings.CALIBRATOR_MEANING_CHECK:
                    problems.extend(await self._meaning_check(plain(vault_bundle.masked_text), _without_placeholders(plain(raw_calibrated))))
            # an issue the note marks as missing from the text needs a placeholder where it belongs; saying "it is not
            # in the text" is not an answer (the author would never see what to add)
            wanted = _missing_items(focus)
            have = raw_calibrated.count(PLACEHOLDER_OPEN)
            if wanted and not missing and have < wanted:
                problems.append(
                    f"The note lists {wanted} issue(s) marked as {MISSING_MARK}, but your version has {have} "
                    "[AUTHOR TO ADD: ...] placeholder(s). Insert one for each of them, at the place in the text where the "
                    "missing item belongs (for example \"[AUTHOR TO ADD: random seed used for training and sampling]\"), "
                    "without digits in it."
                )
            if not problems or attempt == 1:
                break
            current_user_prompt = (
                f"{user_prompt}\n\nCRITICAL FIX: your previous version had these problems. Write it again from the "
                "original text above and fix exactly these, keeping everything else:\n- "
                + "\n- ".join(problems)
                + f"\n\nYOUR PREVIOUS VERSION:\n{raw_calibrated}"
            )

        # what is still wrong after the retry is shown to the user, never hidden
        quality_warnings = [f"Check: {problem}" for problem in problems if "protected token" not in problem]
        if not register.get("recognised", True):
            quality_warnings.append(
                f"'{journal_name}' is not a journal name we recognise, so a general academic register was used. "
                "Enter the exact journal name (for example 'Polymer Chemistry') for its house style."
            )

        # Step 5: Detokenize & Verify Every Single Protected Invariant
        restored_text, verification_report = ScientificVault.verify_and_unmask(
            candidate_masked_text=raw_calibrated,
            vault=vault_bundle.vault,
            records=vault_bundle.records,
        )
        # Step 6: Post-Transformation Audit & Gatekeeper Check
        audit_report = CalibrationAuditor.audit(
            original_text=text,
            calibrated_text=restored_text,
            banned_markers=register["banned_synthetic_markers"],
            verification_report=verification_report,
        )

        # Determine Final Status
        if audit_report["is_blocked"]:
            final_status = "blocked"
            safe_calibrated_text = None  # HARD BLOCK: Never expose corrupted or un-restored output!
        elif vault_bundle.token_count == 0:
            final_status = "no_invariants"
            safe_calibrated_text = restored_text
        else:
            final_status = "verified"
            safe_calibrated_text = restored_text

        unreported = _unreported_issues(focus, notes)
        quality_warnings.extend(
            f"Issue {number}: the Calibrator did not say what it did for this issue. Check the text for it." for number in unreported
        )
        return CalibrationResult(
            calibrated_text=safe_calibrated_text,
            original_text=text,
            status=final_status,
            input_type="manuscript_draft",
            invariants_verified=verification_report.is_valid,
            total_detected_invariants=verification_report.total_detected,
            total_restored_invariants=verification_report.total_restored,
            word_count_original=len(text.split()),
            word_count_calibrated=len(safe_calibrated_text.split()) if safe_calibrated_text else 0,
            synthetic_markers_removed=audit_report["markers_eliminated_count"],
            intensity_applied=intensity,
            audit_notes=quality_warnings + notes + audit_report["warnings"],
            block_reasons=audit_report["block_reasons"],
            calibrated_masked_text=raw_calibrated if safe_calibrated_text is not None else None,
            vault_values=dict(vault_bundle.vault),
        )

    async def _meaning_check(self, original: str, rewritten: str) -> List[str]:
        """A second, strict reading: what did the rewrite drop, strengthen or add? Returns problems as instructions."""
        system_prompt = (
            "You check a rewrite of a scientific passage against the original. Report ONLY real differences in meaning. "
            "Ignore wording, style, order and sentence structure.\n"
            "- lost: facts, list items, conditions, qualifiers or the subject of a claim that the original has and the rewrite does not\n"
            "- stronger: claims stated more strongly or more generally than in the original (for example 'allow for' becoming a direct claim)\n"
            "- added: facts or claims in the rewrite that are not in the original\n"
            "Quote each as a short phrase from the original (lost, stronger) or the rewrite (added). Return JSON only: "
            '{"lost": [], "stronger": [], "added": []}. Use empty lists when the meaning is the same.'
        )
        try:
            raw = await self.llm.generate(
                system_prompt=system_prompt,
                user_prompt=f"ORIGINAL:\n{original}\n\nREWRITE:\n{rewritten}",
                temperature=0.0,
            )
            parsed = self.llm.parse_json_response(raw)
        except Exception:  # the check is a help, not a gate
            logger.warning("Calibrator meaning check failed", exc_info=True)
            return []
        templates = {
            "lost": "This was in the original and is missing: '{}'. Put it back.",
            "stronger": "This claim is now stronger than in the original: '{}'. Keep the original strength.",
            "added": "This is not in the original and must not be added: '{}'.",
        }
        problems: List[str] = []
        for key, template in templates.items():
            items = parsed.get(key, [])
            if isinstance(items, list):
                for item in items[:5]:
                    phrase = re.sub(r"\s+", " ", str(item)).strip()[:160]
                    if phrase and PLACEHOLDER_OPEN.lower() not in phrase.lower():  # a placeholder is not a claim
                        problems.append(template.format(phrase))
        return problems

    async def _handle_reviewer_guidance(
        self,
        reviewer_text: str,
        journal_name: str,
        field: str,
    ) -> CalibrationResult:
        """Handle reviewer comments safely by providing actionable revision strategy rather than fabricating claims."""
        system_prompt = f"""You are a Senior Editorial Peer Review Strategist at {journal_name} ({field}).
The user has provided reviewer feedback or editorial revision instructions.

STRICT PROTOCOL (NON-NEGOTIABLE):
1. Do NOT fabricate or generate hypothetical scientific results, fake experimental baselines, or unverified claims.
2. Dissect the critique into concrete, numbered author action items. State exactly what empirical evidence, ablation tables, or baseline comparisons the authors must gather to satisfy the reviewer.

OUTPUT FORMAT:
Return a single valid JSON object:
{{
  "strategy_summary": "1-2 sentence high-level assessment of reviewer's core concern",
  "action_items": [
    "Specific empirical evidence or analysis required",
    "Clarification required in methodology/framing",
    "Evidence boundary warning (what not to claim without data)"
  ]
}}
""".strip()

        user_prompt = f"Analyze this reviewer feedback and produce an actionable revision strategy:\n\n{reviewer_text}"

        try:
            response_json_str = await self.llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.10,
            )
            parsed = self.llm.parse_json_response(response_json_str)
            summary = parsed.get("strategy_summary", "Reviewer feedback analyzed.")
            items = parsed.get("action_items", [])
            guidance_text = f"**Reviewer Critique Assessment:**\n{summary}\n\n**Required Empirical Evidence & Action Items:**\n" + "\n".join(f"• {it}" for it in items)
        except Exception as e:
            logger.error("Reviewer guidance generation failed: %s", e)
            items = [
                "Dissect critique into specific experimental requests.",
                "Provide direct empirical comparisons against established baselines before claiming superiority.",
                "Verify whether architectural mechanism or data distribution explains performance delta.",
            ]
            guidance_text = "Reviewer feedback detected. Empirical claims cannot be generated without your experimental data.\n\n" + "\n".join(f"• {it}" for it in items)

        return CalibrationResult(
            calibrated_text=guidance_text,
            original_text=reviewer_text,
            status="guidance",
            input_type="reviewer_critique",
            invariants_verified=True,
            total_detected_invariants=0,
            total_restored_invariants=0,
            word_count_original=len(reviewer_text.split()),
            word_count_calibrated=len(guidance_text.split()),
            synthetic_markers_removed=0,
            intensity_applied="guidance",
            audit_notes=[
                "Input Type: Reviewer Feedback detected. Mode: Revision Guidance activated.",
                "Empirical Boundary Check: No new experimental results or hypothetical baselines generated.",
                "Author Action Required: Synthesize requested empirical comparisons before resubmission.",
            ],
        )

    def _build_system_prompt(
        self,
        register: Dict[str, Any],
        intensity: str,
        field: str,
        has_invariants: bool = True,
        focus: str | None = None,
    ) -> str:
        banned_str = ", ".join(register.get("banned_synthetic_markers", []))
        rules = mode_rules(intensity)
        focus_block = ""
        if focus:
            focus_block = f"""EDITOR'S NOTE FOR THIS SECTION (from the pre-submission review):
{focus}

HOW TO USE THE NOTE:
- Resolve EVERY issue in the note that the text you are given can resolve, using the fix described for it. Where an issue quotes the words it is about, change those words; leave the rest of the section as it is.
- An issue marked "{MISSING_MARK}" is about something the text lacks. Do not answer it with "not present": insert the placeholder [AUTHOR TO ADD: what is needed] at the place in the text where it belongs (a seed or version in the sentence that describes the training or the software, a data statement at the end of the section) and report "Issue N: added a placeholder for ...".
- Rewording and restructuring only. If an issue needs content that is not in the text (a new experiment, measurement, comparison, version number, statement or claim), do NOT write that content: put a bracketed placeholder at the place where it belongs, in the form [AUTHOR TO ADD: what is needed], and never put digits in a placeholder.
- If the fix is a question for the author ("check whether ..."), do not change the text for it; report it in "modifications".
- An issue about words that are not in the text you were given is not yours to fix: say so in "modifications".
- Report one entry per issue in "modifications", in the form "Issue N: <what you changed, or why you could not>", before any other entry.

"""

        if has_invariants:
            invariant_rules = """CRITICAL INVARIANTS (NON-NEGOTIABLE):
1. The text contains invariant placeholder tokens formatted as `__REJ_INV_<4_DIGIT_ID>__` (for example: `__REJ_INV_0000__`).
2. You MUST preserve every `__REJ_INV_<4_DIGIT_ID>__` token EXACTLY as given in the input.
3. NEVER invent, hallucinate, or insert fake placeholder tokens. Only preserve the ones present in the user text.
4. NEVER fabricate empirical claims, causal assertions, or study parameters that are not in the input."""
        else:
            invariant_rules = """CRITICAL INTEGRITY (NON-NEGOTIABLE):
1. The text contains NO invariant placeholder tokens. Do NOT invent, hallucinate, or output any token matching `__REJ_INV_XXXX__`.
2. NEVER fabricate empirical claims, causal assertions, or study parameters that are not in the input."""

        return f"""You are the Executive Senior Scientific Editor at {register['target_journal']} in {field}.
Your mandate: bring the submitted excerpt into the register expected by the editorial board, changing only what the mode allows and never the science.

{invariant_rules}

EDITORIAL PRINCIPLES:
1. BAN GENERIC ACADEMIC FLUFF:
   - FORBIDDEN: "highlighting the significant advancements", "substantial predictive utility", "demonstrating paramount effectiveness", "invaluable contribution", "pivotal role", "vibrant tapestry".
   - INSTEAD: State empirical findings directly and concisely.
2. MODE: {rules.name.upper()}
   - You may: {rules.allowed}.
   - You must not: {rules.forbidden}.
   - Keep at least {round(100 * rules.min_length_ratio)}% of the original length.
3. KEEP THE MEANING EXACTLY (all modes):
   - Keep the subject of every claim (if the original says "the design and synthesis of X attracted interest", do not make it "X attracted interest").
   - Keep every qualifier at its strength: "may", "suggest", "allow for", "potential" must not become a direct claim.
   - Keep every item of every list, every condition, and every compound label, abbreviation and name exactly as written.
   - Do not add "significant", "novel", "first", "remarkable" or any claim the original does not make.
4. HEDGING & CALIBRATION OF OVERCLAIMS:
   - Soften unsubstantiated superlative claims ("decisively outperforming", "dramatically exceeds") to calibrated scientific comparisons ("exceeding the baseline", "improving upon prior benchmarks") unless explicit statistical test results are provided.
5. TARGET JOURNAL TONE:
   - Voice: {register['voice']}
   - Cadence: {register.get('target_cadence', 'Direct declarative academic rhythm.')}
   - Eradicate these synthetic clichés: {banned_str}.

{focus_block}OUTPUT FORMAT:
Return a single valid JSON object with EXACTLY this structure:
{{
  "calibrated_masked_text": "<the calibrated version>",
  "modifications": ["concise list of structural and stylistic enhancements made"]
}}
""".strip()

    def _build_user_prompt(self, masked_text: str, intensity: str = "rewrite") -> str:
        rules = mode_rules(intensity)
        return (
            f"Calibrate the following manuscript text in {rules.name.upper()} mode ({rules.allowed}; {rules.forbidden}):"
            f"\n\n{masked_text}"
        )
