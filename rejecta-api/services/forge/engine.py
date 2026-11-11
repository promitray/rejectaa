"""services/forge/engine.py
Abstract Forge: three versions of an abstract (discoverability, clarity, high-impact), checked against the draft.

How a result is made:
1. Numbers, units, statistics and citations of the draft are hidden behind tokens the model must keep.
2. One model call writes the three versions.
3. Code checks every version: all tokens kept, no qualifier or negation dropped, no claim word added, no new
   numbers, names and labels kept, length within the limit, keywords really in the text.
4. A second model call compares all three versions with the draft for meaning (lost, stronger, added).
5. Versions with problems are sent back once, with the reasons. A version that still loses a protected
   number is blocked; other remaining problems are shown as warnings.
6. Scores are computed by code from these checks. The model's own opinion of its work is not used.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from config import settings
from services.calibrator.quality import citation_tokens, fix_citation_punctuation, meaning_problems
from services.calibrator.vault import InvariantBundle, ScientificVault
from services.llm.base import AbstractAnalysisProvider

logger = logging.getLogger(__name__)

VariantType = Literal["discoverability_seo", "clarity_direct", "high_impact_narrative"]
VARIANT_TYPES: tuple[str, ...] = ("discoverability_seo", "clarity_direct", "high_impact_narrative")
DEFAULT_JOURNAL = "the target journal"
DEFAULT_FIELD = "the paper's field"
SOFT_MAX_WORDS = 300  # used when the author gives no word limit
WORD_LIMIT_SLACK = 1.10  # a version may run 10% over the limit before it counts as too long
_TOKEN = re.compile(r"__REJ_INV_\d{4}__")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class ForgeError(RuntimeError):
    """The model could not produce versions to show."""


class ReadinessScoreBreakdown(BaseModel):
    """Computed by code from the checks (not the model's opinion). 100 = nothing found wrong."""

    overall_readiness_score: int = Field(ge=0, le=100)
    empirical_anchoring: int = Field(ge=0, le=100)  # facts of the draft kept
    comparison_grounding: int = Field(ge=0, le=100)  # comparisons and claims not broadened
    tradeoff_retention: int = Field(ge=0, le=100)  # limitations, qualifiers and negations kept
    narrative_discipline: int = Field(ge=0, le=100)  # no added claims, within the word limit


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
    variant_type: VariantType
    title_label: str
    target_persona: str
    abstract_text: str
    word_count: int
    readiness_index: ReadinessScoreBreakdown
    predicted_acceptance_score: int = Field(ge=0, le=100)  # same as the overall faithfulness score; kept for older clients
    score_mood: str
    key_highlights: List[str]
    seo_keywords: List[str] = Field(default_factory=list)
    claim_audit: ClaimAuditDetail
    invariants_verified: bool = True
    blocked: bool = False  # True when a protected number or citation was lost: the text is not shown
    block_reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)  # problems still present after the retry


class AbstractForgeResult(BaseModel):
    source_word_count: int
    target_journal: str
    fact_inventory: Dict[str, Any]
    variants: List[AbstractVariantDetail]
    comparative_summary: str
    invariants_preserved_count: int
    invariant_telemetry: InvariantDetailSummary
    max_words: int | None = None


# --------------------------------------------------------------------------- venue

_CHEMISTRY = ("chem", "polym", "macromol", "langmuir", "rsc", "acs ", "angew", "dalton", "catal", "inorg", "materials")


class AbstractForgeEngine:
    """Core synthesis engine for multi-target architectural abstract generation."""

    def __init__(self, llm_service: AbstractAnalysisProvider) -> None:
        self.llm = llm_service

    @staticmethod
    def resolve_venue_targets(journal_name: str, field: str) -> Dict[str, str]:
        """Indexing databases and editorial readers for the journal and field."""
        j_lower = journal_name.lower()
        f_lower = field.lower()
        both = f"{j_lower} {f_lower}"

        if any(w in both for w in ["software", "ieee", "acm", "computer", "hardware", "circuit", "robotics"]):
            indexers = "IEEE Xplore, ACM Digital Library, Google Scholar, DBLP"
        elif any(w in both for w in ["lancet", "medicine", "clinical", "health", "biomedical", "cell", "pharm"]):
            indexers = "PubMed / MEDLINE, Google Scholar, Web of Science, Embase"
        elif any(w in both for w in _CHEMISTRY):
            indexers = "CAS SciFinder, Web of Science, Scopus, Google Scholar"
        elif any(w in both for w in ["physics", "math", "optics"]):
            indexers = "arXiv, Scopus, Google Scholar, Web of Science"
        else:
            indexers = "Google Scholar, Scopus, CrossRef, Web of Science"

        if any(w in j_lower for w in ["nature", "science", "cell", "lancet", "pnas"]):
            narrative_target = f"Senior Advisory Editors at {journal_name} (Broad Interdisciplinary Scope)"
            clarity_target = f"Rigorous Peer Reviewers & Domain Specialists in {field}"
        elif any(w in j_lower for w in ["ieee", "transactions", "acm"]):
            narrative_target = f"Senior Editors & Editorial Board at {journal_name}"
            clarity_target = f"Specialist Technical Reviewers in {field}"
        else:
            narrative_target = f"Editors & Readership at {journal_name}"
            clarity_target = f"Peer Reviewers & Researchers in {field}"

        return {
            "seo_target": f"Indexed retrieval: {indexers}",
            "seo_indexers": indexers,
            "clarity_target": clarity_target,
            "narrative_target": narrative_target,
        }

    # ----------------------------------------------------------------------- main flow

    async def forge(
        self,
        abstract_draft: str,
        methodology_context: str | None = None,
        journal_name: str | None = None,
        field: str | None = None,
        max_words: int | None = None,
    ) -> AbstractForgeResult:
        """Write three versions of the abstract and check each one against the draft."""
        journal_name = (journal_name or "").strip() or DEFAULT_JOURNAL
        field = (field or "").strip() or DEFAULT_FIELD
        context = (methodology_context or "").strip()

        # Only the draft's numbers are required in every version; the context may be used but need not be.
        vault_bundle: InvariantBundle = ScientificVault.mask(abstract_draft)
        categories = {r.token_id: r.category for r in vault_bundle.records}
        cite_tokens = citation_tokens(vault_bundle.vault, categories)
        invariant_telemetry = self._telemetry(vault_bundle)
        fact_inventory = self._extract_source_fact_inventory(abstract_draft)
        venue_targets = self.resolve_venue_targets(journal_name, field)
        limit = max_words or SOFT_MAX_WORDS
        allowed_numbers = set(_NUMBER.findall(f"{abstract_draft} {context}"))

        system_prompt = self._build_system_prompt(
            journal_name=journal_name,
            field=field,
            fact_inventory=fact_inventory,
            venue_targets=venue_targets,
            has_invariants=(vault_bundle.token_count > 0),
            max_words=limit,
        )
        user_prompt = self._build_user_prompt(vault_bundle.masked_text, fact_inventory, journal_name, field, context)

        parsed = await self._generate(system_prompt, user_prompt, temperature=0.20)
        drafts: dict[str, dict[str, Any]] = {}
        for raw in parsed.get("variants", []) if isinstance(parsed.get("variants"), list) else []:
            if isinstance(raw, dict) and raw.get("variant_type") in VARIANT_TYPES and raw["variant_type"] not in drafts:
                drafts[raw["variant_type"]] = raw
        if not drafts:
            raise ForgeError("The model returned no usable versions.")
        comparative_summary = str(parsed.get("comparative_summary") or "").strip()

        def masked_of(raw: dict[str, Any]) -> str:
            text = str(raw.get("masked_text") or raw.get("abstract_text") or "")
            return fix_citation_punctuation(vault_bundle.masked_text, text, cite_tokens)

        def problems_of(v_type: str, masked: str, meaning: dict[str, list[str]]) -> tuple[list[str], list[str]]:
            """(blocking problems, other problems) for one version."""
            blocking = [f"Protected value '{vault_bundle.vault[t]}' ({t}) is missing." for t in vault_bundle.vault if t not in masked]
            extra = sorted(set(_TOKEN.findall(masked)) - set(vault_bundle.vault))
            if extra:
                blocking.append(f"Unknown placeholder token(s) {', '.join(extra)} must not appear.")
            text = self._plain(masked, vault_bundle.vault)
            other = meaning_problems(abstract_draft, text)
            new_numbers = sorted(set(_NUMBER.findall(text)) - allowed_numbers)
            if new_numbers:
                other.append("These numbers are not in the draft or the context and must not appear: " + ", ".join(new_numbers[:8]) + ".")
            words = len(text.split())
            if words > limit * WORD_LIMIT_SLACK:
                other.append(f"The version has {words} words; the limit is {limit}. Shorten it without dropping facts.")
            other.extend(meaning.get(v_type, []))
            return blocking, other

        # first check of all versions, one targeted retry for the ones with problems
        meaning = await self._meaning_check(abstract_draft, context, {k: self._plain(masked_of(v), vault_bundle.vault) for k, v in drafts.items()})
        failing = {k: problems_of(k, masked_of(v), meaning) for k, v in drafts.items()}
        failing = {k: b + o for k, (b, o) in failing.items() if b or o}
        if failing:
            retry = await self._retry(system_prompt, user_prompt, drafts, failing, masked_of)
            if retry:
                drafts.update(retry)
                rechecked = await self._meaning_check(
                    abstract_draft, context, {k: self._plain(masked_of(drafts[k]), vault_bundle.vault) for k in retry}
                )
                meaning.update(rechecked)
                for k in retry:
                    if k not in rechecked:
                        meaning.pop(k, None)

        variants: List[AbstractVariantDetail] = []
        for v_type in VARIANT_TYPES:
            if v_type not in drafts:
                continue
            raw = drafts[v_type]
            masked = masked_of(raw)
            blocking, other = problems_of(v_type, masked, meaning)
            text = self._plain(masked, vault_bundle.vault)
            text = re.sub(r",?\s*(?:seo_)?keywords\s*:\s*\[[^\]]*\]", "", text, flags=re.IGNORECASE).strip()
            claim_audit = self._audit_generated_claims(fact_inventory, text, v_type, other)
            keywords, dropped = self._grounded_keywords(raw.get("seo_keywords", []), text, abstract_draft)
            warnings = list(other)
            if dropped:
                warnings.append("Keywords not found in the abstract or the draft were left out: " + ", ".join(dropped) + ".")
            readiness = self._score(vault_bundle, masked, other, len(text.split()), limit, claim_audit)
            target_persona = (
                venue_targets["seo_target"] if v_type == "discoverability_seo"
                else venue_targets["clarity_target"] if v_type == "clarity_direct"
                else venue_targets["narrative_target"]
            )
            variants.append(
                AbstractVariantDetail(
                    variant_type=v_type,  # type: ignore[arg-type]
                    title_label=self._default_label(v_type),
                    target_persona=target_persona,
                    abstract_text="" if blocking else text,
                    word_count=0 if blocking else len(text.split()),
                    readiness_index=readiness,
                    predicted_acceptance_score=readiness.overall_readiness_score,
                    score_mood="blocked" if blocking else self._mood(readiness.overall_readiness_score),
                    key_highlights=[str(h)[:80] for h in raw.get("key_highlights", [])][:4] if isinstance(raw.get("key_highlights"), list) else [],
                    seo_keywords=keywords if v_type == "discoverability_seo" else [],
                    claim_audit=claim_audit,
                    invariants_verified=not blocking,
                    blocked=bool(blocking),
                    block_reasons=blocking,
                    warnings=warnings,
                )
            )

        invariant_telemetry.all_spans_verified = all(not v.blocked for v in variants)
        return AbstractForgeResult(
            source_word_count=len(abstract_draft.split()),
            target_journal=journal_name,
            fact_inventory=fact_inventory,
            variants=variants,
            comparative_summary=comparative_summary or f"Three versions of the abstract for {journal_name}, each checked against your draft.",
            invariants_preserved_count=vault_bundle.token_count,
            invariant_telemetry=invariant_telemetry,
            max_words=max_words,
        )

    # ----------------------------------------------------------------------- model calls

    async def _generate(self, system_prompt: str, user_prompt: str, temperature: float) -> dict[str, Any]:
        try:
            raw = await self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature)
            parsed = self.llm.parse_json_response(raw)
        except Exception as exc:
            logger.error("AbstractForge generation failed: %s", exc)
            raise ForgeError("The abstract versions could not be generated. Try again.") from exc
        return parsed if isinstance(parsed, dict) else {}

    async def _retry(self, system_prompt, user_prompt, drafts, failing, masked_of) -> dict[str, dict[str, Any]]:
        """Write the failing versions again, each with its own list of problems and its previous text."""
        lines = []
        for v_type, problems in failing.items():
            lines.append(
                f"VERSION {v_type}:\nProblems:\n- " + "\n- ".join(problems) + f"\nYour previous text:\n{masked_of(drafts[v_type])}"
            )
        retry_prompt = (
            f"{user_prompt}\n\nCRITICAL FIX: some versions had problems. Write ONLY these versions again, from the source "
            "above, fixing exactly these problems and keeping everything else. Return the same JSON structure with only "
            "these versions in \"variants\".\n\n" + "\n\n".join(lines)
        )
        try:
            parsed = await self._generate(system_prompt, retry_prompt, temperature=0.10)
        except ForgeError:
            return {}
        fixed: dict[str, dict[str, Any]] = {}
        for raw in parsed.get("variants", []) if isinstance(parsed.get("variants"), list) else []:
            if isinstance(raw, dict) and raw.get("variant_type") in failing and raw.get("masked_text"):
                fixed[raw["variant_type"]] = {**drafts[raw["variant_type"]], **raw}
        return fixed

    async def _meaning_check(self, draft: str, context: str, versions: dict[str, str]) -> dict[str, list[str]]:
        """One call for all versions: what each one dropped, made stronger or added compared with the draft."""
        if not settings.FORGE_MEANING_CHECK or not versions:
            return {}
        system_prompt = (
            "You check rewritten versions of a scientific abstract against the author's draft. Report ONLY real differences "
            "in meaning; ignore wording, order and structure. A version may leave out minor detail to fit an abstract, but "
            "must not drop a main finding, a limitation or a qualifier.\n"
            "- lost: main findings, limitations, conditions or qualifiers of the draft that the version does not have\n"
            "- stronger: claims stated more strongly, more generally or as more novel than in the draft\n"
            "- added: facts, results, applications or claims that are in neither the draft nor the context\n"
            "Quote each as a short phrase. Return JSON only: "
            '{"versions": {"<version id>": {"lost": [], "stronger": [], "added": []}}}'
        )
        body = "\n\n".join(f"VERSION {k}:\n{v}" for k, v in versions.items())
        user_prompt = f"DRAFT:\n{draft}\n\nCONTEXT (may be used):\n{context or '(none)'}\n\n{body}"
        try:
            raw = await self.llm.generate(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.0)
            parsed = self.llm.parse_json_response(raw)
        except Exception:  # the check is a help, not a gate
            logger.warning("AbstractForge meaning check failed", exc_info=True)
            return {}
        found = parsed.get("versions", {}) if isinstance(parsed, dict) else {}
        templates = {
            "lost": "This is in the draft and missing here: '{}'. Put it back.",
            "stronger": "This claim is stronger than in the draft: '{}'. Keep the draft's strength.",
            "added": "This is not in the draft or the context and must not be added: '{}'.",
        }
        result: dict[str, list[str]] = {}
        for v_type in versions:
            entry = found.get(v_type, {}) if isinstance(found, dict) else {}
            problems: list[str] = []
            if isinstance(entry, dict):
                for key, template in templates.items():
                    items = entry.get(key, [])
                    for item in items[:4] if isinstance(items, list) else []:
                        phrase = re.sub(r"\s+", " ", str(item)).strip()[:160]
                        if phrase:
                            problems.append(template.format(phrase))
            result[v_type] = problems
        return result

    # ----------------------------------------------------------------------- checks and scores

    @staticmethod
    def _plain(masked: str, vault: Dict[str, str]) -> str:
        text = _TOKEN.sub(lambda m: vault.get(m.group(0), ""), masked)
        return ScientificVault.normalize_unit_typography(re.sub(r"[ \t]+([,.;:])", r"\1", text)).strip()

    @staticmethod
    def _grounded_keywords(keywords: Any, text: str, draft: str) -> tuple[list[str], list[str]]:
        """Keywords whose words all appear in the version or the draft; the rest are dropped."""
        haystack = f"{text} {draft}".lower()
        kept: list[str] = []
        dropped: list[str] = []
        for keyword in keywords if isinstance(keywords, list) else []:
            phrase = re.sub(r"\s+", " ", str(keyword)).strip()
            if not phrase or len(phrase) > 60:
                continue
            words = re.findall(r"[a-z0-9]+", phrase.lower())
            (kept if words and all(w in haystack for w in words) else dropped).append(phrase)
        return kept[:8], dropped

    @staticmethod
    def _score(vault_bundle: InvariantBundle, masked: str, problems: list[str], words: int, limit: int, audit: ClaimAuditDetail) -> ReadinessScoreBreakdown:
        """Scores from the checks: each starts at 100 and loses points for what was found."""
        kept = sum(1 for t in vault_bundle.vault if t in masked)
        anchoring = round(100 * kept / len(vault_bundle.vault)) if vault_bundle.vault else 100
        lost = sum(1 for p in problems if "missing" in p or "Put it back" in p)
        anchoring = max(0, anchoring - 15 * lost)

        stronger = sum(1 for p in problems if "stronger" in p or "claim more" in p) + len(audit.generalized_baselines_detected)
        grounding = max(0, 100 - 25 * stronger - 25 * len(audit.unsupported_causality_detected))

        qualifiers = sum(1 for p in problems if "qualifier" in p or "negation" in p) + len(audit.omitted_tradeoffs_detected)
        tradeoff = max(0, 100 - 25 * qualifiers)

        added = sum(1 for p in problems if "must not be added" in p or "must not appear" in p)
        over = max(0.0, words / limit - 1) if limit else 0.0
        discipline = max(0, 100 - 25 * added - round(100 * over))

        overall = round(0.35 * anchoring + 0.25 * grounding + 0.2 * tradeoff + 0.2 * discipline)
        return ReadinessScoreBreakdown(
            overall_readiness_score=overall, empirical_anchoring=anchoring, comparison_grounding=grounding,
            tradeoff_retention=tradeoff, narrative_discipline=discipline,
        )

    @staticmethod
    def _mood(score: int) -> str:
        return "delighted" if score >= 90 else "pleased" if score >= 75 else "neutral"

    @staticmethod
    def _telemetry(bundle: InvariantBundle) -> InvariantDetailSummary:
        counts: Dict[str, int] = {}
        for r in bundle.records:
            counts[r.category] = counts.get(r.category, 0) + 1
        names = {"stat": "statistical", "unit": "units", "citation": "citations", "citation_marker": "citation numbers", "math": "math expressions"}
        parts = [f"{counts[k]} {label}" for k, label in names.items() if counts.get(k)]
        label = (
            f"{bundle.token_count} protected spans ({', '.join(parts)})"
            if bundle.token_count
            else "No numbers, units or citations in the draft to protect"
        )
        return InvariantDetailSummary(total_spans=bundle.token_count, by_category=counts, all_spans_verified=True, summary_label=label)

    def _extract_source_fact_inventory(self, text: str) -> Dict[str, Any]:
        """Trade-offs, explicit comparisons and paired statistics stated in the draft."""
        tradeoffs = [
            m.group(0).strip()
            for m in re.finditer(
                r"\b(?:cost|penalty|trade-off|overhead|limitation|additional\s+time|increased\s+latency|higher\s+memory)\s+(?:of|was|by)?\s*([^.,;]+)",
                text,
                flags=re.IGNORECASE,
            )
        ]
        comparisons = [
            m.group(0).strip()
            for m in re.finditer(
                r"\b(?:higher|lower|better|faster|greater|superior|surpassed|outperformed|improved|comparable|similar)\s+(?:\w+\s+)?(?:than|to|over)\s*([A-Za-z0-9\s\-]+?)(?=[.,;]|\band\b|\bwith\b|\bwithout\b|$)",
                text,
                flags=re.IGNORECASE,
            )
        ]
        paired_stats = [
            m.group(0).strip()
            for m in re.finditer(r"(\b\d+(?:\.\d+)?\s*(?:percentage\s+points|%)\s*(?:\([^)]*(?:95%|99%)\s*CI[^)]*\))?)", text, flags=re.IGNORECASE)
            if "CI" in m.group(0) or "percentage points" in m.group(0)
        ]
        return {
            "explicit_tradeoffs": tradeoffs,
            "explicit_comparisons": comparisons,
            "mandatory_paired_stats": paired_stats,
            "has_tradeoffs": bool(tradeoffs),
            "has_comparisons": bool(comparisons),
            "has_paired_stats": bool(paired_stats),
        }

    def _audit_generated_claims(
        self,
        source_facts: Dict[str, Any],
        generated_abstract: str,
        variant_type: str = "clarity_direct",
        problems: List[str] | None = None,
    ) -> ClaimAuditDetail:
        """Overclaiming, broadened comparisons and dropped limitations, from fixed rules and the checks above."""
        gen_lower = generated_abstract.lower()
        generalized: list[str] = []
        omitted: list[str] = []
        unsupported: list[str] = []

        if re.search(r"\b(?:outperformed|surpassed|exceeded|outperforms|surpasses)\s+all\s+(?:prior|existing|previous)?\s*(?:baselines|methods|approaches|systems)\b", gen_lower):
            generalized.append("Broadened a comparison to 'all' prior work.")
        if re.search(r"\buniversal(?:ly)?\s+superior", gen_lower):
            generalized.append("Claimed universal superiority.")

        for to in source_facts.get("explicit_tradeoffs", []):
            keywords = [w for w in re.findall(r"\b[A-Za-z0-9%]+\b", to.lower()) if len(w) > 2]
            if keywords and not any(kw in gen_lower for kw in keywords):
                omitted.append(f"Omitted a limitation stated in the draft: '{to}'")

        if variant_type == "clarity_direct":
            for ps in source_facts.get("mandatory_paired_stats", []):
                if not any(num in gen_lower for num in re.findall(r"\b\d+\b", ps)):
                    omitted.append(f"Clarity version omitted the main statistical comparison: '{ps}'")

        if re.search(r"\b(?:proves?|proven)\s+that\b|\bdefinitively\b|\bcures?\b", gen_lower):
            unsupported.append("Definitive or causal claim the draft does not make.")

        for problem in problems or []:
            if "stronger" in problem or "claim more" in problem:
                generalized.append(problem)
            elif "qualifier" in problem or "negation" in problem or "missing here" in problem:
                omitted.append(problem)
            elif "must not be added" in problem:
                unsupported.append(problem)

        is_faithful = not (generalized or omitted or unsupported)
        verdict = (
            "No change of meaning found against your draft."
            if is_faithful
            else f"{len(generalized)} stronger claim(s), {len(omitted)} omission(s) and {len(unsupported)} addition(s) found. Read before using."
        )
        return ClaimAuditDetail(
            is_faithful=is_faithful,
            generalized_baselines_detected=generalized,
            omitted_tradeoffs_detected=omitted,
            unsupported_causality_detected=unsupported,
            audit_verdict=verdict,
        )

    def _default_label(self, v_type: str) -> str:
        return {
            "discoverability_seo": "Discoverability (SEO & Indexing)",
            "clarity_direct": "Clarity (Direct Empirical)",
            "high_impact_narrative": "High-Impact (Editorial Narrative)",
        }.get(v_type, "Custom Variant")

    # ----------------------------------------------------------------------- prompts

    def _build_system_prompt(
        self,
        journal_name: str,
        field: str,
        fact_inventory: Dict[str, Any],
        venue_targets: Dict[str, str],
        has_invariants: bool,
        max_words: int = SOFT_MAX_WORDS,
    ) -> str:
        tradeoff_mandate = ""
        if fact_inventory.get("has_tradeoffs"):
            tradeoff_list = ", ".join(f"'{t}'" for t in fact_inventory["explicit_tradeoffs"])
            tradeoff_mandate = (
                f"\nLIMITATIONS IN THE DRAFT: {tradeoff_list}. Keep them in EVERY version; never drop them to look stronger."
            )
        paired_stat_mandate = ""
        if fact_inventory.get("has_paired_stats"):
            ps_list = ", ".join(f"'{ps}'" for ps in fact_inventory["mandatory_paired_stats"])
            paired_stat_mandate = f"\nMAIN STATISTICAL COMPARISON: {ps_list}. Report it, with its interval and p-value, in the Clarity version."

        invariant_note = (
            "The draft contains tokens like `__REJ_INV_0000__` that stand for numbers, units, statistics and citations. "
            "Keep EVERY token EXACTLY as written in EVERY version. Never delete, change or invent a token. "
            "A version that loses a token is rejected."
            if has_invariants
            else "The draft contains no placeholder tokens. Do not write any token like `__REJ_INV_0000__`."
        )

        return f"""You are a senior scientific editor at {journal_name} ({field}).
Write exactly THREE versions of the author's abstract, each for a different reader. All three report the same science.

{invariant_note}
{tradeoff_mandate}
{paired_stat_mandate}

FIDELITY RULES (apply to all three versions; code checks every one):
1. Use only facts from the DRAFT, and optionally from the CONTEXT. No new results, numbers, applications, comparisons or claims.
2. Keep every main finding, limitation, condition and qualifier ("may", "suggest", "allow for", "potential") at its original strength.
3. Name the same baselines the draft names. Never write "outperforms all", "first", "novel", "unprecedented", "significant" unless the draft says so.
4. Keep compound labels, abbreviations and names exactly as written.
5. Each version has at most {max_words} words.

THE THREE VERSIONS:
1. "discoverability_seo": for readers searching {venue_targets['seo_indexers']}. Open with a natural, complete sentence stating the topic and the main result (not a list of keywords). Spell out acronyms on first use. Give 5-8 "seo_keywords" made only of terms used in the draft.
2. "clarity_direct": for specialist reviewers in {field}. Problem, approach, exact findings with every number of the draft, then the limitation. No framing or adjectives.
3. "high_impact_narrative": for editors at {journal_name}. Open with the problem the work addresses and why it matters, using only the significance the draft itself states. Then the approach and findings, with the same numbers and limitations. Do not inflate.

OUTPUT FORMAT: one JSON object, exactly:
{{
  "comparative_summary": "<1-2 sentences on how the three versions differ>",
  "variants": [
    {{"variant_type": "discoverability_seo", "masked_text": "<the version, tokens intact>", "key_highlights": ["<what this version does, 2-4 short items>"], "seo_keywords": ["<term from the draft>"]}},
    {{"variant_type": "clarity_direct", "masked_text": "<the version, tokens intact>", "key_highlights": ["..."]}},
    {{"variant_type": "high_impact_narrative", "masked_text": "<the version, tokens intact>", "key_highlights": ["..."]}}
  ]
}}
""".strip()

    def _build_user_prompt(
        self,
        masked_abstract: str,
        fact_inventory: Dict[str, Any],
        journal_name: str,
        field: str,
        context: str = "",
    ) -> str:
        reminders = []
        if fact_inventory.get("has_tradeoffs"):
            reminders.append(f"Keep these limitations: {', '.join(fact_inventory['explicit_tradeoffs'])}.")
        if fact_inventory.get("has_paired_stats"):
            reminders.append(f"Clarity version must report: {', '.join(fact_inventory['mandatory_paired_stats'])}.")
        reminder_block = ("\n" + "\n".join(reminders)) if reminders else ""
        context_block = f"\n\nCONTEXT (facts you MAY use; not required):\n{context}" if context else ""
        return f"Target journal: {journal_name} ({field}){reminder_block}\n\nDRAFT:\n{masked_abstract}{context_block}"
