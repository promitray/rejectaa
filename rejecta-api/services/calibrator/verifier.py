"""services/calibrator/verifier.py
Deterministic audit, drift verification, and hard output gatekeeper for calibrated manuscripts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from services.calibrator.vault import InvariantVerificationReport


class CalibrationAuditor:
    """Performs post-generation audits to guarantee zero empirical drift and transparent telemetry."""

    UNSUBSTANTIATED_OVERCLAIMS = [
        "decisively outperforming",
        "decisively outperforms",
        "dramatically improves",
        "revolutionary",
        "unprecedented",
        "groundbreaking progress",
        "unrivaled",
        "decisively",
    ]

    @classmethod
    def audit(
        cls,
        original_text: str,
        calibrated_text: str,
        banned_markers: List[str],
        verification_report: InvariantVerificationReport,
    ) -> Dict[str, Any]:
        """Perform comprehensive deterministic audits."""
        warnings: List[str] = []
        is_blocked = False
        block_reasons: List[str] = []

        # 1. Strict Invariant Gatekeeper Check
        if not verification_report.is_valid:
            is_blocked = True
            block_reasons.extend(verification_report.failures)
            warnings.append(
                f"Quarantine Enforcement: Blocked release because {verification_report.total_detected - verification_report.total_restored} of {verification_report.total_detected} protected invariants failed verification."
            )

        # 2. Synthetic Marker / Cliché Audit
        orig_lower = original_text.lower()
        calib_lower = calibrated_text.lower()

        eliminated_count = 0
        detected_cliches_in_orig = 0
        for marker in banned_markers:
            orig_matches = len(re.findall(rf"\b{re.escape(marker)}\b", orig_lower))
            calib_matches = len(re.findall(rf"\b{re.escape(marker)}\b", calib_lower))
            if orig_matches > 0:
                detected_cliches_in_orig += orig_matches
            if orig_matches > calib_matches:
                eliminated_count += (orig_matches - calib_matches)
            elif calib_matches > 0:
                warnings.append(f"Residual synthetic marker retained: '{marker}'.")

        # 3. Scientific Overclaim & Baseline Citation Check
        for overclaim in cls.UNSUBSTANTIATED_OVERCLAIMS:
            if re.search(rf"\b{re.escape(overclaim)}\b", orig_lower):
                if not re.search(rf"\b{re.escape(overclaim)}\b", calib_lower):
                    warnings.append(
                        f"Editorial Improvement: Calibrated unsupported overclaim '{overclaim}' into measured academic tone."
                    )
                else:
                    warnings.append(
                        f"Statistical Caution: '{overclaim}' retained. Ensure comparative statistical test is explicitly reported."
                    )

        if "vaswani et al." in orig_lower:
            warnings.append(
                "Bibliographic Advisory: 'Vaswani et al. (2017)' is a foundational architecture reference. If claiming outperformance in clinical prognosis, ensure a domain-specific clinical baseline is also evaluated."
            )

        # 4. Contextual & Semantic Coherence Check (Loss function vs biological dosage)
        has_loss_eqn = bool(re.search(r"\\mathcal\{L\}|loss|objective", orig_lower))
        has_dosage = bool(re.search(r"\b\d+\s*(?:µg/ml|ug/ml|mg/kg)\b", orig_lower))
        if has_loss_eqn and has_dosage:
            warnings.append(
                "Semantic Coherence Advisory: The relationship between the loss function equation and the pharmacotherapy dosage is unarticulated in the draft. Verify whether dosage acts as a stratification variable, feature input, or cohort filter."
            )

        # 5. Length compression calculation (Review warning, NOT automatic failure)
        len_orig = len(original_text.split())
        len_calib = len(calibrated_text.split())
        compression_pct = round(((len_orig - len_calib) / max(len_orig, 1)) * 100, 1)

        if len_orig > 0:
            ratio = len_calib / len_orig
            if ratio < 0.60:
                warnings.append(
                    f"Length Compression Warning: Text reduced by {compression_pct}% (>40% compression). Review to ensure no secondary qualitative nuance was omitted."
                )
            elif ratio > 1.35:
                warnings.append("Length Inflation Warning: Text suffered verbosity inflation (>35% increase).")

        return {
            "is_blocked": is_blocked,
            "block_reasons": block_reasons,
            "invariants_intact": verification_report.is_valid,
            "total_detected_invariants": verification_report.total_detected,
            "total_restored_invariants": verification_report.total_restored,
            "detected_cliches_in_orig": detected_cliches_in_orig,
            "markers_eliminated_count": eliminated_count,
            "compression_pct": compression_pct,
            "warnings": warnings,
        }
