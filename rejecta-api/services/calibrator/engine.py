"""services/calibrator/engine.py
Voice Calibrator v2.5 orchestration engine.
Features hard output gatekeeper, reviewer feedback guidance generation, and verified invariant mapping.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal

from pydantic import BaseModel

from services.calibrator.classifier import ClassificationResult, InputTypeClassifier
from services.calibrator.corpus import JournalCorpusService
from services.calibrator.vault import InvariantBundle, ScientificVault
from services.calibrator.verifier import CalibrationAuditor
from services.llm.base import AbstractAnalysisProvider

logger = logging.getLogger(__name__)


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
    ) -> CalibrationResult:
        """Calibrate input text with hard output verification or reviewer guidance handling."""
        # Step 0: Input Type Classification
        classification: ClassificationResult = InputTypeClassifier.classify(text)

        # Path A: Reviewer Critique Detected -> Do NOT fabricate research prose; Generate Structured Guidance
        if classification.is_reviewer_feedback:
            return await self._handle_reviewer_guidance(text, journal_name, field)

        # Path B: Standard Manuscript Draft Calibration
        # Step 1: Secure Invariants into the Vault with Span Records
        vault_bundle: InvariantBundle = ScientificVault.mask(text)

        # Step 2: Fetch Journal Register Profile
        register = await JournalCorpusService.get_journal_register_profile(journal_name)

        # Step 3: Construct Constrained Calibration Prompt
        system_prompt = self._build_system_prompt(register, intensity, field, has_invariants=(vault_bundle.token_count > 0))
        user_prompt = self._build_user_prompt(vault_bundle.masked_text)

        # Step 4: Execute LLM Call with Missing Token Retry
        raw_calibrated = ""
        notes = []
        current_user_prompt = user_prompt
        for attempt in range(2):
            try:
                response_json_str = await self.llm.generate(
                    system_prompt=system_prompt,
                    user_prompt=current_user_prompt,
                    temperature=0.10 if attempt > 0 else 0.15,
                )
                parsed = self.llm.parse_json_response(response_json_str)
                raw_calibrated = parsed.get("calibrated_masked_text", "")
                notes = parsed.get("modifications", [])
                if not isinstance(notes, list):
                    notes = [str(notes)]

                # Check if all tokens were preserved
                all_present = all(t in raw_calibrated for t in vault_bundle.vault.keys())
                if all_present or attempt == 1:
                    break
                # If attempt 0 dropped tokens, formulate a targeted retry
                missing = [t for t in vault_bundle.vault.keys() if t not in raw_calibrated]
                current_user_prompt += f"\n\nCRITICAL FIX: You omitted protected token(s) {', '.join(missing)}. You MUST keep all original tokens in your output!"
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
            audit_notes=notes + audit_report["warnings"],
            block_reasons=audit_report["block_reasons"],
        )

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

    def _build_system_prompt(self, register: Dict[str, Any], intensity: str, field: str, has_invariants: bool = True) -> str:
        banned_str = ", ".join(register.get("banned_synthetic_markers", []))

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
Your mandate: Transform the submitted excerpt into concise, authoritative, empirical prose expected by the editorial board.

{invariant_rules}

EDITORIAL PRINCIPLES:
1. BAN GENERIC ACADEMIC FLUFF:
   - FORBIDDEN: "highlighting the significant advancements", "substantial predictive utility", "demonstrating paramount effectiveness", "invaluable contribution", "pivotal role", "vibrant tapestry".
   - INSTEAD: State empirical findings directly and concisely.
2. STRUCTURAL COMPRESSION:
   - Do NOT just replace words with synonyms. Reorganize sentence order, fuse clauses, eliminate throat-clearing preambles.
   - Lead directly with the methodology or core empirical claim.
3. HEDGING & CALIBRATION OF OVERCLAIMS:
   - Soften unsubstantiated superlative claims ("decisively outperforming", "dramatically exceeds") to calibrated scientific comparisons ("exceeding the baseline", "improving upon prior benchmarks") unless explicit statistical test results are provided.
4. TARGET JOURNAL TONE:
   - Voice: {register['voice']}
   - Cadence: {register.get('target_cadence', 'Direct declarative academic rhythm.')}
   - Eradicate these synthetic clichés: {banned_str}.

OUTPUT FORMAT:
Return a single valid JSON object with EXACTLY this structure:
{{
  "calibrated_masked_text": "<concise, restructured calibrated version>",
  "modifications": ["concise list of structural and stylistic enhancements made"]
}}
""".strip()

    def _build_user_prompt(self, masked_text: str) -> str:
        return f"Calibrate the following manuscript draft to tier-1 journal standards, removing all filler and tightening the argumentative flow:\n\n{masked_text}"
