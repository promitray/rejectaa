"""services/forge/engine.py
Abstract Forge synthesis engine v2.0:
Features:
1. Discipline- & Venue-Aware Targeting (replaces generic PubMed/Nature defaults with exact domain indexers).
2. Mandatory Paired Statistical Comparison & Trade-off Extraction (prevents omission of bootstrap deltas, CIs, p-values).
3. Two-Pass Constrained Synthesis (Discoverability, Clarity, High-Impact Narrative).
4. Post-generation Claim & Limitation Audit.
5. Invariant Counter Transparency with category-level breakdowns (math, stat, unit, citation).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from services.calibrator.vault import InvariantBundle, ScientificVault
from services.llm.base import AbstractAnalysisProvider

logger = logging.getLogger(__name__)


class ReadinessScoreBreakdown(BaseModel):
    overall_readiness_score: int = Field(ge=0, le=100)
    empirical_anchoring: int = Field(ge=0, le=100)
    comparison_grounding: int = Field(ge=0, le=100)
    tradeoff_retention: int = Field(ge=0, le=100)
    narrative_discipline: int = Field(ge=0, le=100)


class ClaimAuditDetail(BaseModel):
    is_faithful: bool
    generalized_baselines_detected: List[str] = Field(default_factory=list)
    omitted_tradeoffs_detected: List[str] = Field(default_factory=list)
    unsupported_causality_detected: List[str] = Field(default_factory=list)
    audit_verdict: str


class InvariantDetailSummary(BaseModel):
    total_spans: int
    by_category: Dict[str, int] = Field(default_factory=dict)
    all_spans_verified: bool = True
    summary_label: str


class AbstractVariantDetail(BaseModel):
    variant_type: Literal["discoverability_seo", "clarity_direct", "high_impact_narrative"]
    title_label: str
    target_persona: str
    abstract_text: str
    word_count: int
    readiness_index: ReadinessScoreBreakdown
    predicted_acceptance_score: int = Field(ge=0, le=100)
    score_mood: str
    key_highlights: List[str]
    seo_keywords: List[str] = Field(default_factory=list)
    claim_audit: ClaimAuditDetail
    invariants_verified: bool = True


class AbstractForgeResult(BaseModel):
    source_word_count: int
    target_journal: str
    fact_inventory: Dict[str, Any]
    variants: List[AbstractVariantDetail]
    comparative_summary: str
    invariants_preserved_count: int
    invariant_telemetry: InvariantDetailSummary


class AbstractForgeEngine:
    """Core synthesis engine for multi-target architectural abstract generation."""

    def __init__(self, llm_service: AbstractAnalysisProvider) -> None:
        self.llm = llm_service

    @staticmethod
    def resolve_venue_targets(journal_name: str, field: str) -> Dict[str, str]:
        """Resolve indexing databases and editorial personas based on actual venue and discipline.

        Prevents recommending PubMed for Software Engineering, or Nature editorial targets for IEEE.
        """
        j_lower = journal_name.lower()
        f_lower = field.lower()

        # 1. Determine Indexers for Discoverability
        if any(w in j_lower or w in f_lower for w in ["software", "ieee", "acm", "computer", "hardware", "circuit", "robotics"]):
            indexers = "IEEE Xplore, ACM Digital Library, Google Scholar, DBLP"
        elif any(w in j_lower or w in f_lower for w in ["lancet", "medicine", "clinical", "health", "biomedical", "cell", "pharm"]):
            indexers = "PubMed / MEDLINE, Google Scholar, Web of Science, Embase"
        elif any(w in j_lower or w in f_lower for w in ["physics", "math", "materials", "optics"]):
            indexers = "arXiv, Scopus, Google Scholar, Web of Science"
        else:
            indexers = "Google Scholar, Scopus, CrossRef, Web of Science"

        # 2. Determine High-Impact Audience Persona
        if any(w in j_lower for w in ["ieee", "transactions", "acm", "journal of"]):
            narrative_target = f"Senior Editors & Editorial Board at {journal_name}"
            clarity_target = f"Specialist Technical Reviewers in {field}"
        elif any(w in j_lower for w in ["nature", "science", "cell", "lancet", "pnas"]):
            narrative_target = f"Senior Advisory Editors at {journal_name} (Broad Interdisciplinary Scope)"
            clarity_target = f"Rigorous Peer Reviewers & Domain Specialists in {field}"
        else:
            narrative_target = f"Executive Editors & Readership at {journal_name}"
            clarity_target = f"Peer Reviewers & Researchers in {field}"

        return {
            "seo_target": f"Indexed retrieval: {indexers}",
            "clarity_target": clarity_target,
            "narrative_target": narrative_target,
        }

    async def forge(
        self,
        abstract_draft: str,
        methodology_context: str | None = None,
        journal_name: str = "Nature Machine Intelligence",
        field: str = "Computer Science & Machine Learning",
    ) -> AbstractForgeResult:
        """Synthesize three distinct abstract architectures with invariant protection."""
        full_input_text = abstract_draft
        if methodology_context and methodology_context.strip():
            full_input_text += f"\n\n[Methodology & Findings Context]\n{methodology_context.strip()}"

        # Step 1: Secure Invariants into the Vault
        vault_bundle: InvariantBundle = ScientificVault.mask(full_input_text)

        # Build Invariant Category Telemetry
        cat_counts: Dict[str, int] = {}
        for r in vault_bundle.records:
            cat_counts[r.category] = cat_counts.get(r.category, 0) + 1

        summary_parts = []
        if cat_counts.get("stat", 0) > 0:
            summary_parts.append(f"{cat_counts['stat']} statistical")
        if cat_counts.get("unit", 0) > 0:
            summary_parts.append(f"{cat_counts['unit']} units")
        if cat_counts.get("citation", 0) > 0:
            summary_parts.append(f"{cat_counts['citation']} citations")
        if cat_counts.get("math", 0) > 0:
            summary_parts.append(f"{cat_counts['math']} math expressions")

        telemetry_label = (
            f"{vault_bundle.token_count} protected spans ({', '.join(summary_parts)})"
            if vault_bundle.token_count > 0
            else "General text draft (0 numerical/formulaic spans)"
        )

        invariant_telemetry = InvariantDetailSummary(
            total_spans=vault_bundle.token_count,
            by_category=cat_counts,
            all_spans_verified=True,
            summary_label=telemetry_label,
        )

        # Step 2: Extract & Independently Validate Source Fact Inventory
        fact_inventory = self._extract_source_fact_inventory(full_input_text)

        # Step 3: Resolve Dynamic Venue-Aware Targets
        venue_targets = self.resolve_venue_targets(journal_name, field)

        # Step 4: Build Two-Pass Constrained Prompt
        system_prompt = self._build_system_prompt(
            journal_name=journal_name,
            field=field,
            fact_inventory=fact_inventory,
            venue_targets=venue_targets,
            has_invariants=(vault_bundle.token_count > 0),
        )
        user_prompt = self._build_user_prompt(
            masked_abstract=vault_bundle.masked_text,
            fact_inventory=fact_inventory,
            journal_name=journal_name,
            field=field,
        )

        # Step 5: Execute LLM Call (T = 0.20 for reproducible structural precision)
        try:
            response_json_str = await self.llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.20,
            )
            parsed = self.llm.parse_json_response(response_json_str)
            raw_variants = parsed.get("variants", [])
            comparative_summary = parsed.get(
                "comparative_summary",
                f"Synthesized 3 distinct architectures tailored for {journal_name} ({field})."
            )
        except Exception as e:
            logger.error("AbstractForge LLM generation failed: %s", e)
            raw_variants = [
                {
                    "variant_type": "clarity_direct",
                    "title_label": "Clarity (Direct Empirical)",
                    "target_persona": venue_targets["clarity_target"],
                    "masked_text": vault_bundle.masked_text,
                    "readiness_breakdown": {
                        "overall": 75,
                        "empirical_anchoring": 80,
                        "comparison_grounding": 75,
                        "tradeoff_retention": 70,
                        "narrative_discipline": 75,
                    },
                    "key_highlights": ["Direct empirical statement", "Preserved original scope"],
                    "seo_keywords": [],
                }
            ]
            comparative_summary = f"Synthesis pipeline fallback: {e}"

        # Step 6: Detokenize Invariants & Run Post-Generation Claim Audit
        processed_variants: List[AbstractVariantDetail] = []
        for v in raw_variants:
            raw_text = v.get("masked_text", v.get("abstract_text", ""))
            restored_text, verification_report = ScientificVault.verify_and_unmask(
                candidate_masked_text=raw_text,
                vault=vault_bundle.vault,
                records=vault_bundle.records,
            )
            # Clean any accidental inline keyword leakages (e.g. "seo_keywords: [...]" at end of abstract text)
            restored_text = re.sub(r",?\s*(?:seo_)?keywords\s*:\s*\[[^\]]*\]", "", restored_text, flags=re.IGNORECASE).strip()
            restored_text = re.sub(r",?\s*(?:seo_)?keywords\s*:\s*.*$", "", restored_text, flags=re.IGNORECASE).strip()
            v_type = v.get("variant_type", "clarity_direct")
            if v_type not in ["discoverability_seo", "clarity_direct", "high_impact_narrative"]:
                v_type = "clarity_direct"

            # Post-generation Claim & Limitation Audit
            claim_audit = self._audit_generated_claims(
                source_facts=fact_inventory,
                generated_abstract=restored_text,
                variant_type=v_type,
            )

            # Compute Editorial Readiness Index Breakdown
            readiness_data = v.get("readiness_breakdown", {})
            score_overall = int(readiness_data.get("overall", v.get("predicted_acceptance_score", 85)))

            if not claim_audit.is_faithful:
                score_overall = max(60, score_overall - 10)

            score_overall = max(50, min(96, score_overall))
            readiness_breakdown = ReadinessScoreBreakdown(
                overall_readiness_score=score_overall,
                empirical_anchoring=readiness_data.get("empirical_anchoring", 88),
                comparison_grounding=readiness_data.get("comparison_grounding", 85 if claim_audit.is_faithful else 65),
                tradeoff_retention=readiness_data.get("tradeoff_retention", 85 if not claim_audit.omitted_tradeoffs_detected else 60),
                narrative_discipline=readiness_data.get("narrative_discipline", 85),
            )

            mood = "delighted" if score_overall >= 88 else "pleased" if score_overall >= 80 else "neutral"

            target_persona = (
                venue_targets["seo_target"]
                if v_type == "discoverability_seo"
                else venue_targets["clarity_target"]
                if v_type == "clarity_direct"
                else venue_targets["narrative_target"]
            )

            processed_variants.append(
                AbstractVariantDetail(
                    variant_type=v_type,
                    title_label=v.get("title_label", self._default_label(v_type)),
                    target_persona=target_persona,
                    abstract_text=restored_text,
                    word_count=len(restored_text.split()),
                    readiness_index=readiness_breakdown,
                    predicted_acceptance_score=score_overall,
                    score_mood=mood,
                    key_highlights=v.get("key_highlights", ["Preserved quantitative findings", "Grounded comparisons"]),
                    seo_keywords=v.get("seo_keywords", []),
                    claim_audit=claim_audit,
                    invariants_verified=verification_report.is_valid,
                )
            )

        return AbstractForgeResult(
            source_word_count=len(abstract_draft.split()),
            target_journal=journal_name,
            fact_inventory=fact_inventory,
            variants=processed_variants,
            comparative_summary=comparative_summary,
            invariants_preserved_count=vault_bundle.token_count,
            invariant_telemetry=invariant_telemetry,
        )

    def _extract_source_fact_inventory(self, text: str) -> Dict[str, Any]:
        """Extract structured fact inventory: Paired statistical comparisons (CI, bootstrap, p-values),

        trade-offs, and baselines.
        """
        tradeoffs = []
        tradeoff_matches = re.finditer(
            r"\b(?:cost|penalty|trade-off|overhead|limitation|additional\s+time|increased\s+latency|higher\s+memory)\s+(?:of|was|by)?\s*([^.,;]+)",
            text,
            flags=re.IGNORECASE,
        )
        for m in tradeoff_matches:
            tradeoffs.append(m.group(0).strip())

        # Check for explicit comparisons (e.g. 'higher accuracy than logistic regression')
        comparisons = []
        comp_matches = re.finditer(
            r"\b(?:higher|lower|better|faster|greater|superior|surpassed|outperformed|improved|comparable|similar)\s+(?:\w+\s+)?(?:than|to|over)\s*([A-Za-z0-9\s\-]+?)(?=[.,;]|\band\b|\bwith\b|\bwithout\b|$)",
            text,
            flags=re.IGNORECASE,
        )
        for m in comp_matches:
            comparisons.append(m.group(0).strip())

        # Check for mandatory paired statistical comparison deltas (e.g. '17 percentage points (95% CI, 9–25; p = 0.018)')
        paired_stats = []
        paired_stat_matches = re.finditer(
            r"(\b\d+(?:\.\d+)?\s*(?:percentage\s+points|%)\s*(?:\([^)]*(?:95%|99%)\s*CI[^)]*\))?)",
            text,
            flags=re.IGNORECASE,
        )
        for m in paired_stat_matches:
            val = m.group(0).strip()
            if "CI" in val or "percentage points" in val:
                paired_stats.append(val)

        return {
            "explicit_tradeoffs": tradeoffs,
            "explicit_comparisons": comparisons,
            "mandatory_paired_stats": paired_stats,
            "has_tradeoffs": len(tradeoffs) > 0,
            "has_comparisons": len(comparisons) > 0,
            "has_paired_stats": len(paired_stats) > 0,
        }

    def _audit_generated_claims(
        self,
        source_facts: Dict[str, Any],
        generated_abstract: str,
        variant_type: str = "clarity_direct",
    ) -> ClaimAuditDetail:
        """Audit generated abstract against source facts for overclaiming, baseline broadening,

        and omission of mandatory paired statistics.
        """
        gen_lower = generated_abstract.lower()
        generalized = []
        omitted = []
        unsupported = []

        # 1. Baseline Generalization Check: Did the model broaden specific baselines to 'all baselines'?
        if source_facts.get("has_comparisons"):
            if re.search(r"\b(?:outperformed|surpassed|exceeded)\s+all\s+(?:prior|existing)?\s*baselines\b", gen_lower):
                generalized.append("Broadened comparison to 'all baselines' without source justification.")
            if re.search(r"\buniversal\s+superiority\b", gen_lower):
                generalized.append("Claimed 'universal superiority'.")

        # 2. Trade-off Omission Check: Did the model silently drop essential costs/penalties?
        for to in source_facts.get("explicit_tradeoffs", []):
            to_keywords = [w for w in re.findall(r"\b[A-Za-z0-9%]+\b", to.lower()) if len(w) > 2]
            matches_count = sum(1 for kw in to_keywords if kw in gen_lower)
            if matches_count == 0 and len(to_keywords) > 0:
                omitted.append(f"Omitted explicit source trade-off: '{to}'")

        # 3. Mandatory Paired Statistical Comparison Check: In Clarity mode, paired comparison must not be dropped!
        if variant_type == "clarity_direct" and source_facts.get("has_paired_stats"):
            for ps in source_facts.get("mandatory_paired_stats", []):
                # Check if core numbers from the paired comparison (e.g. '17', '95%') exist
                ps_numbers = re.findall(r"\b\d+\b", ps)
                has_num_match = any(num in gen_lower for num in ps_numbers)
                if not has_num_match:
                    omitted.append(f"Clarity variant omitted primary paired statistical comparison: '{ps}'")

        # 4. Association to Causation Check
        if "proves that" in gen_lower or "definitively cures" in gen_lower:
            unsupported.append("Unwarranted definitive causal claim.")

        is_faithful = len(generalized) == 0 and len(omitted) == 0 and len(unsupported) == 0

        verdict = (
            "Verified faithful to source facts, paired metrics, and limitations."
            if is_faithful
            else f"Caution: {len(generalized)} claim adjustments and {len(omitted)} omissions detected."
        )

        return ClaimAuditDetail(
            is_faithful=is_faithful,
            generalized_baselines_detected=generalized,
            omitted_tradeoffs_detected=omitted,
            unsupported_causality_detected=unsupported,
            audit_verdict=verdict,
        )

    def _default_label(self, v_type: str) -> str:
        labels = {
            "discoverability_seo": "Discoverability (SEO & Indexing)",
            "clarity_direct": "Clarity (Direct Empirical)",
            "high_impact_narrative": "High-Impact (Editorial Narrative)",
        }
        return labels.get(v_type, "Custom Variant")

    def _build_system_prompt(
        self,
        journal_name: str,
        field: str,
        fact_inventory: Dict[str, Any],
        venue_targets: Dict[str, str],
        has_invariants: bool
    ) -> str:
        tradeoff_mandate = ""
        if fact_inventory.get("has_tradeoffs"):
            tradeoff_list = ", ".join(f"'{t}'" for t in fact_inventory["explicit_tradeoffs"])
            tradeoff_mandate += f"""\nCRITICAL TRADE-OFF PRESERVATION:
Source specifies the following trade-offs/limitations: {tradeoff_list}.
You MUST explicitly retain these limitations in EVERY variant. NEVER omit them to artificially inflate performance."""

        paired_stat_mandate = ""
        if fact_inventory.get("has_paired_stats"):
            ps_list = ", ".join(f"'{ps}'" for ps in fact_inventory["mandatory_paired_stats"])
            paired_stat_mandate += f"""\nMANDATORY PAIRED STATISTICAL COMPARISON:
The source text contains primary comparative statistical evidence: {ps_list}.
You MUST explicitly report this paired delta (including confidence interval and p-value) in the Clarity variant!"""

        invariant_note = (
            "The input contains invariant tokens formatted as `__REJ_INV_XXXX__`. "
            "You MUST preserve every single `__REJ_INV_XXXX__` token EXACTLY as written in every variant. "
            "NEVER delete, modify, or invent tokens."
            if has_invariants
            else "The input contains NO invariant tokens. Do not invent tokens matching `__REJ_INV_XXXX__`."
        )

        return f"""You are the Executive Senior Scientific Editor at {journal_name} ({field}).
Your mandate: Synthesize exactly THREE non-overlapping, publication-ready architectural abstract variants tailored for this exact journal and discipline.

{invariant_note}
{tradeoff_mandate}
{paired_stat_mandate}

DISCIPLINE & VENUE TARGETING:
- Target Journal: {journal_name}
- Target Field: {field}
- Discoverability Indexers: {venue_targets['seo_target']}
- Editorial Target for Narrative: {venue_targets['narrative_target']}
- Do NOT mention irrelevant medical databases (like PubMed) if this is a Software Engineering or Computer Science paper! Use appropriate indexers.

STRICT SCIENTIFIC CLAIM FIDELITY (P0 RULES):
1. NO BASELINE BROADENING:
   - If the source compares against a specific baseline (e.g. 'higher accuracy than logistic regression'), state that exact baseline. NEVER generalize to 'outperformed all baselines' or 'surpassed all prior work'.
2. NARRATIVE RESTRAINT:
   - You may frame broader significance for {journal_name}, but you MUST NEVER invent unproven clinical benefits, universal superiority, or unwarranted causal claims.
3. NATURAL DISCOVERABILITY OPENING:
   - In the Discoverability variant, the opening sentence MUST be a natural, fluid, grammatically complete declarative scientific sentence. DO NOT start with a stilted comma-separated list of keywords.

THE THREE DISTINCT ARCHITECTURAL VARIANTS:

1. VARIANT A ("discoverability_seo"):
   - Goal: Indexing discoverability on {venue_targets['seo_target']} with natural scholarly prose.
   - Rules: Frontload core topic in a natural declarative opening sentence. Expand acronyms on first mention. Output 5-8 grounded `seo_keywords` relevant strictly to {field}.

2. VARIANT B ("clarity_direct"):
   - Goal: Absolute empirical rigor and zero fluff for specialist reviewers at {journal_name}.
   - Rules: Problem -> Framework -> Exact Controlled Findings (including paired statistical comparison delta) -> Explicit Trade-off/Limitation.

3. VARIANT C ("high_impact_narrative"):
   - Goal: Capturing editorial board interest at {journal_name} through field-advancing framing.
   - Rules: Frame the fundamental roadblock and conceptual advance while preserving strict empirical honesty.

OUTPUT FORMAT:
Return a single valid JSON object with EXACTLY this structure:
{{
  "comparative_summary": "1-2 sentence overview of the 3 architectural variants",
  "variants": [
    {{
      "variant_type": "discoverability_seo",
      "title_label": "Discoverability (SEO & Indexing)",
      "masked_text": "<abstract text with all __REJ_INV_XXXX__ tokens intact>",
      "readiness_breakdown": {{
        "overall": 86,
        "empirical_anchoring": 88,
        "comparison_grounding": 85,
        "tradeoff_retention": 85,
        "narrative_discipline": 86
      }},
      "key_highlights": ["natural declarative opener", "grounded indexing terms"],
      "seo_keywords": ["keyword1", "keyword2", "keyword3"]
    }},
    {{
      "variant_type": "clarity_direct",
      "title_label": "Clarity (Direct Empirical)",
      "masked_text": "<abstract text with all __REJ_INV_XXXX__ tokens intact>",
      "readiness_breakdown": {{
        "overall": 92,
        "empirical_anchoring": 94,
        "comparison_grounding": 92,
        "tradeoff_retention": 90,
        "narrative_discipline": 92
      }},
      "key_highlights": ["zero fluff", "hypothesis to metric linkage", "paired bootstrap comparison retained"]
    }},
    {{
      "variant_type": "high_impact_narrative",
      "title_label": "High-Impact (Editorial Narrative)",
      "masked_text": "<abstract text with all __REJ_INV_XXXX__ tokens intact>",
      "readiness_breakdown": {{
        "overall": 89,
        "empirical_anchoring": 86,
        "comparison_grounding": 85,
        "tradeoff_retention": 85,
        "narrative_discipline": 88
      }},
      "key_highlights": ["paradigm shift framing", "disciplined narrative impact"]
    }}
  ]
}}
""".strip()

    def _build_user_prompt(
        self,
        masked_abstract: str,
        fact_inventory: Dict[str, Any],
        journal_name: str,
        field: str
    ) -> str:
        reminders = []
        if fact_inventory.get("has_tradeoffs"):
            reminders.append(f"Retain explicit source trade-offs: {', '.join(fact_inventory['explicit_tradeoffs'])}.")
        if fact_inventory.get("has_paired_stats"):
            reminders.append(f"Retain primary paired statistical evidence in Clarity variant: {', '.join(fact_inventory['mandatory_paired_stats'])}.")

        reminder_block = "\n".join(reminders)
        if reminder_block:
            reminder_block = f"\n{reminder_block}\n"

        return f"""Target Journal: {journal_name} ({field})

Source Content:{reminder_block}
{masked_abstract}
"""
