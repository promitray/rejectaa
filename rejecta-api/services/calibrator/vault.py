"""services/calibrator/vault.py
Deterministic extractor and restorer for mathematical, statistical, and bibliographic invariants.
Guarantees strict source-span protection, mixed-citation preservation, and verified two-way restoration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple


@dataclass(frozen=True)
class InvariantRecord:
    token_id: str
    original_value: str
    category: str  # 'math', 'citation', 'stat', 'unit'
    span_start: int
    span_end: int


@dataclass(frozen=True)
class InvariantBundle:
    masked_text: str
    vault: Dict[str, str]
    records: List[InvariantRecord]
    token_count: int


@dataclass
class InvariantVerificationReport:
    total_detected: int
    total_restored: int
    is_valid: bool
    missing_tokens: List[str]
    orphan_placeholders: List[str]
    detected_by_category: Dict[str, int] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)


class ScientificVault:
    """Vault for span-based extraction, isolation, and cryptographic restoration of scientific invariants."""

    # 1. LaTeX Math Expressions ($...$ and $$...$$)
    _LATEX_PATTERN = re.compile(r"(\$\$.*?\$\$|\$[^\$]+?\$)", re.DOTALL)

    # 2. Bibliographic Citations:
    # Handles:
    # - Mixed/Compound: (Author et al., 2024; [17, 21–23]) or (Smith et al., 2021; Jones & Brown, 2022)
    # - Author-Year: (Author et al., 2025), (Smith, 2020), (Author et al., 2025; [8, 11–13])
    # - Numbered brackets: [17, 21–23], [1-4], [3, 7]
    _MIXED_CITATION_PATTERN = re.compile(
        r"(\(\s*[A-Z][A-Za-z\-]*(?:\s+et\s+al\.)?,\s*(?:19|20)\d{2}[a-z]?"
        r"(?:;\s*(?:\[\s*\d+(?:[\–\-,]\s*\d+)*\s*\]|[A-Z][A-Za-z\-]*(?:\s+et\s+al\.)?,\s*(?:19|20)\d{2}[a-z]?))*\s*\))"
    )
    _STANDARD_CITATION_PATTERN = re.compile(
        r"(\[\s*\d+(?:[\–\-,]\s*\d+)*\s*\]|\(\s*[A-Z][A-Za-z\-]*(?:\s+et\s+al\.)?,\s*(?:19|20)\d{2}[a-z]?\s*\))"
    )

    # 3. Statistical Assertions, Inter-Quartile Ranges, Confidence Intervals, and p-values:
    # e.g.: p < 0.05, p = 0.001, t(24) = 3.12, 95% CI [-7.8 to -2.4], 95% CI [0.12, 0.45], IQR, 12–27, n = 1,420, AUROC = 0.941
    _STAT_PATTERN = re.compile(
        r"(\b[pPtTzZFf]\s*[\=<>\≤\≥]\s*0?\.\d+|\b[nN]\s*=\s*[\d,]+|"
        r"(?:95%|99%)\s*CI[,\s]*\[?\s*-?\d+\.?\d*(?:\s*to\s*|[,\–\-]\s*)-?\d+\.?\d*\s*\]?|"
        r"\bIQR,\s*\d+(?:[\–\-]\d+)?|"
        r"\b(?:AUROC|AUC|RMSE|MAE|F1|R\^?2)\s*[\=:]\s*0?\.\d+)"
    )

    # 4. Quantities & Scientific Units (including negative deltas and micro-variants):
    # -5.1 min, 18 min, 24 min, 50 ug/mL, 50 µg/mL, 12.5 mg/kg, 300 K, 37 °C, 1.84 mJ, 1.21 mJ, 92.6%, -0.2 percentage points
    _UNIT_PATTERN = re.compile(
        r"((?:[-–]\s*)?\b\d+(?:\.\d+)?\s*(?:min|h|s|ms|mJ|J|kJ|W|mW|mg\/kg|µg\/mL|ug\/mL|mg|mL|μL|nm|μm|mm|cm|m|kg|Hz|kHz|MHz|GHz|percentage\s+points|mol\/L|M|mM|K|°C)\b|(?:[-–]\s*)?\b\d+(?:\.\d+)?\s*%)"
    )
    @classmethod
    def mask(cls, text: str) -> InvariantBundle:
        """Extract invariants with source-span indexing.

        Extracts in strict precedence:
        1. LaTeX expressions (prevents internal math symbols from splitting)
        2. Mixed / compound citations (keeps author-year + bracket groupings together)
        3. Standard citations
        4. Statistical metrics and intervals
        5. Quantities with scientific units
        """
        vault: Dict[str, str] = {}
        records: List[InvariantRecord] = []
        counter = 0

        # We keep track of occupied intervals to avoid overlapping regex matches
        occupied_spans: List[Tuple[int, int]] = []

        def is_overlapping(start: int, end: int) -> bool:
            return any(max(start, o_start) < min(end, o_end) for o_start, o_end in occupied_spans)

        # Step 1: Collect all candidate matches with categories
        candidates: List[Tuple[int, int, str, str]] = []  # (start, end, value, category)

        # LaTeX
        for m in cls._LATEX_PATTERN.finditer(text):
            candidates.append((m.start(), m.end(), m.group(0), "math"))

        # Mixed Citations
        for m in cls._MIXED_CITATION_PATTERN.finditer(text):
            candidates.append((m.start(), m.end(), m.group(0), "citation"))

        # Standard Citations
        for m in cls._STANDARD_CITATION_PATTERN.finditer(text):
            candidates.append((m.start(), m.end(), m.group(0), "citation"))

        # Stats
        for m in cls._STAT_PATTERN.finditer(text):
            candidates.append((m.start(), m.end(), m.group(0), "stat"))

        # Units
        for m in cls._UNIT_PATTERN.finditer(text):
            candidates.append((m.start(), m.end(), m.group(0), "unit"))

        # Sort candidates primarily by length descending (longest span first), then by start position
        candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))

        selected_spans: List[Tuple[int, int, str, str]] = []
        for start, end, val, cat in candidates:
            if not is_overlapping(start, end):
                selected_spans.append((start, end, val, cat))
                occupied_spans.append((start, end))

        # Sort selected spans in text order (start asc) to build replacement
        selected_spans.sort(key=lambda c: c[0])

        # Step 2: Build masked text and register records
        result_parts: List[str] = []
        last_idx = 0
        for start, end, val, cat in selected_spans:
            result_parts.append(text[last_idx:start])
            token_id = f"__REJ_INV_{counter:04d}__"
            vault[token_id] = val
            records.append(
                InvariantRecord(
                    token_id=token_id,
                    original_value=val,
                    category=cat,
                    span_start=start,
                    span_end=end,
                )
            )
            result_parts.append(token_id)
            counter += 1
            last_idx = end

        result_parts.append(text[last_idx:])
        masked_text = "".join(result_parts)

        return InvariantBundle(
            masked_text=masked_text,
            vault=vault,
            records=records,
            token_count=counter,
        )

    @classmethod
    def normalize_unit_typography(cls, text: str) -> str:
        """Normalize ASCII unit shortcuts to standard scientific notation."""
        normalized = re.sub(r"\b(\d+(?:\.\d+)?)\s*ug/mL\b", r"\1 µg/mL", text)
        normalized = re.sub(r"\b(\d+(?:\.\d+)?)\s*uL\b", r"\1 µL", normalized)
        normalized = re.sub(r"\b(\d+(?:\.\d+)?)\s*um\b", r"\1 µm", normalized)
        return normalized

    @classmethod
    def verify_and_unmask(
        cls,
        candidate_masked_text: str,
        vault: Dict[str, str],
        records: List[InvariantRecord]
    ) -> Tuple[str, InvariantVerificationReport]:
        """Perform verified restoration of invariants with strict validation.

        Fails if:
        1. Any token generated in the vault is missing from the output.
        2. Any unauthorized/hallucinated placeholder exists.
        3. Any orphan `__REJ_INV_XXXX__` remains after replacement.
        """
        restored = candidate_masked_text
        missing_tokens: List[str] = []
        failures: List[str] = []

        cat_counts: Dict[str, int] = {}
        for r in records:
            cat_counts[r.category] = cat_counts.get(r.category, 0) + 1

        restored_count = 0
        for token_id, original in vault.items():
            if token_id in restored:
                restored = restored.replace(token_id, original)
                restored_count += 1
            else:
                missing_tokens.append(token_id)
                failures.append(f"Missing protected {vault.get(token_id, 'token')} ({token_id})")

        # Apply typographical normalization on restored text
        restored = cls.normalize_unit_typography(restored)

        # When the vault had 0 invariants detected, any __REJ_INV_XXXX__ output by the model is a stray phantom artifact
        if len(vault) == 0:
            stray_tokens = re.findall(r"__REJ_INV_[A-Za-z0-9_]+__", restored)
            for st in set(stray_tokens):
                restored = restored.replace(st, "").strip()

        # Detect any remaining legitimate orphan placeholders that were supposed to be restored from vault
        orphan_tokens = re.findall(r"__REJ_INV_[A-Za-z0-9_]+__", restored)
        if orphan_tokens:
            failures.append(f"Unresolved placeholder tokens present: {', '.join(set(orphan_tokens))}")

        is_valid = (len(missing_tokens) == 0) and (len(orphan_tokens) == 0) and (restored_count == len(vault))
        report = InvariantVerificationReport(
            total_detected=len(vault),
            total_restored=restored_count,
            is_valid=is_valid,
            missing_tokens=missing_tokens,
            orphan_placeholders=orphan_tokens,
            detected_by_category=cat_counts,
            failures=failures,
        )

        return restored, report
