"""Provider-agnostic base utilities for paper analysis."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from models import AnalysisResult
from services import paper_profile as pp
from services.analysis_guard import AnalysisContext, current_context, finalize_payload

MODE_A = "a"
MODE_B = "b"
MAX_USER_MESSAGE_CHARS = 36000
BODY_BUDGET_CHARS = 26000  # paper text sent to the model; the rest of the message is small
OMITTED_MARKER = " [... text omitted to fit ...] "
JSON_CODE_FENCE = "```json"
GENERIC_CODE_FENCE = "```"


_GROUNDING_RULES = """
GROUNDING RULES. Code checks your report against the manuscript and deletes whatever it cannot find there:
- Use only the text you were given. If something is not shown, say it is not shown. Never fill a gap with what is typical for the field.
- Quotation marks are only for words copied exactly from the manuscript, and only when you need the exact wording. Otherwise paraphrase without quotation marks. A quotation that is not in the manuscript is deleted from your report.
- Do not invent numbers, yields, years, references, authors, instruments, reagents or compounds. Use a number only if it appears in the input.
- Do NOT reference page numbers. Do NOT use phrases like 'as shown in Figure X' or 'Table Y demonstrates' unless that figure or table is described in the text you were given.
- The Supporting Information (ESI) is NOT provided. A paper that points to it is not hiding that content. Do not score a section down, and do not list a rejection reason, for content that the article type normally puts in the Supporting Information.
- A section the article type is not expected to have (see the user message) is never a weakness.
- Score only sections that exist in the input. Do not create a section for text that is not there.
""".strip()

_MOOD_AND_SEVERITY = """
rejection_reasons: identify ALL significant issues, not just one. For a competitive journal expect 2-4 reasons. Every reason MUST have all four parts, and code deletes a reason that lacks one:
1. problem: exactly what the weakness is.
2. evidence: the exact words of the manuscript where this concern shows (for a missing control or an unsupported claim, copy the sentence that makes the claim). Code works out the section and figure from where those words sit, so do not describe the location yourself.
3. impact: how it affects novelty, scientific validity or publication potential, and why a reviewer would act on it.
4. solution: the specific change, tied to the evidence and the problem. Do not write a solution such as 'strengthen the discussion' or 'clarify the implications' that would fit any paper; a solution that shares nothing specific with the problem and the evidence is deleted.
Derive every reason from this paper's own claims and data. Never write a reason you cannot tie to a specific passage. If the paper is strong you may have only 1-2 minor reasons.
severity must be one of: desk_reject, major, minor.
Calibrate severity to the TARGET JOURNAL's standards:
- For top-tier journals (Nature, Science, Cell, JACS, Angewandte and equivalent) be strict: small datasets, limited controls or unverified citations that would be minor at a domain journal become major or desk_reject there.
- For mid-tier journals use standard calibration.
- desk_reject: the paper would not pass initial editorial screening: scope mismatch, fatal methodological flaw, or insufficient novelty for this journal.
- major: acceptable after substantial revision, but reviewers would flag it as a core concern.
- minor: a real issue that would not prevent acceptance if addressed in revision.

mood_score guidance (the final score is recomputed from your criteria, so make the criteria honest):
- 0-20 furious, 21-40 skeptical, 41-55 unimpressed, 56-70 neutral, 71-80 interested, 81-100 delighted.
The mood field MUST match the mood_score range.

WRITING STYLE: Be explanatory, not terse. An author should understand what the problem is, why it matters and how to fix it. Stay within the input, avoid generic advice, and keep the whole JSON compact enough to complete.
""".strip()

_SECTION_FIELDS_A = '''"issue": "<one sentence describing the specific problem in THIS section>",
      "fix": "<one concrete action the author should take>"'''
_SECTION_FIELDS_B = '''"strength": "<2 sentences, specific: what this section does well>",
      "weakness": "<2 sentences, specific: what it fails to do>"'''

_SCHEMA_TEMPLATE = """
The JSON must match this schema EXACTLY. Wrong field names or value types cause a system error:

{
  "paper_format": "<exactly one of: research_article, communication, perspective, review, mini_review>",
  "mood": "<exactly one of: furious, skeptical, unimpressed, neutral, interested, delighted>",
  "mood_score": <integer 0-100, your overall impression>,
  "verdict": "<one sentence __VERDICT__>",
  "oracle_letter": "<150-200 words, specific to this paper: real section names, real claims, real numbers that appear in the input. Generic advice is unacceptable.>",
  "criteria": {
    "<criterion key exactly as listed in the user message>": {
      "score": <integer 0-100>,
      "reason": "<one sentence naming specific content of this paper>"
    }
  },
  "sections": [
    {
      "name": "<section name, see the user message>",
      "role": "<exactly one of: introduction, methods, results, discussion, thematic, other>",
      "source": "<detected or derived>",
      "anchor": "<derived sections only: 8-14 words copied EXACTLY from the manuscript at the start of this section. Empty string for detected sections>",
      "score": <integer 0-100>,
      __SECTION_FIELDS__
    }
  ],
  "rejection_reasons": [
    {
      "problem": "<exactly what the weakness is, specific to this paper: 1-2 sentences naming the claim, result, method or comparison concerned>",
      "evidence": "<8-30 words copied EXACTLY from the manuscript: the sentence or claim this problem is about. Required. A reason with no exact passage to point to is deleted by code.>",
      "impact_area": "<exactly one of: novelty, scientific_validity, publication_potential>",
      "impact": "<1-2 sentences: how this weakness affects that area for THIS paper, and what a reviewer at the target journal would do about it>",
      "solution": "<the exact change the author should make: name the experiment, measurement, analysis, comparison, rewrite or cut, and what it should show. Not advice that would fit any paper>",
      "severity": "<exactly one of: desk_reject, major, minor>"
    }
  ],
  "novelty_assessment": "<one paragraph of 3-4 sentences, honest, specific to what this paper actually claims as new>",
  "journal_fit": "<one paragraph of 3-4 sentences on why this paper does or does not fit this journal, referencing the journal's scope and the paper's actual contribution>"
}
""".strip()


def _build_system_prompt(mode: str) -> str:
    """System prompt for mode "a" (the author's own draft) or "b" (reader mode)."""
    if mode == MODE_A:
        role = (
            "A researcher has submitted their manuscript for pre-submission feedback.\n\n"
            "Your job: give a brutally honest, specific assessment of whether this paper will survive desk review. "
            "Reference specific sections and claims. Do not write generic advice that could apply to any paper."
        )
        verdict = "as the editor in first person, e.g. 'I would desk reject this for insufficient novelty'"
        fields = _SECTION_FIELDS_A
    else:
        role = (
            "A researcher has submitted a manuscript for reader-mode feedback.\n\n"
            "Your job: provide a direct, specific quality assessment grounded in this manuscript's actual content and claims."
        )
        verdict = "overall assessment of the paper's quality"
        fields = _SECTION_FIELDS_B
    schema = _SCHEMA_TEMPLATE.replace("__VERDICT__", verdict).replace("__SECTION_FIELDS__", fields)
    return (
        "You are Professor Aldric Frowns, a journal editor with 34 years on editorial boards at leading "
        "scientific journals.\n"
        f"{role}\n\n"
        "The paper may be from any field (chemistry, biology, computer science, physics, social science...). "
        "Use the terminology of its own field and of the target journal. Do not assume a computer-science structure.\n\n"
        "YOU MUST RETURN ONLY VALID JSON. NO TEXT BEFORE OR AFTER THE JSON. NO MARKDOWN. NO EXPLANATION.\n\n"
        f"{schema}\n\n"
        f"{_MOOD_AND_SEVERITY}\n\n"
        f"{_GROUNDING_RULES}"
    )



def _fit_text(text: str, budget: int) -> str:
    """Keep the start and the end of a text that is too long, so the conclusion is not lost."""
    if len(text) <= budget:
        return text
    head = int(budget * 0.65)
    return text[:head].rstrip() + OMITTED_MARKER + text[-(budget - head) :].lstrip()


def _fit_sections(sections: dict[str, str], budget: int) -> dict[str, str]:
    """Share the budget between sections in proportion to their length (each keeps at least 800 characters)."""
    total = sum(len(v) for v in sections.values())
    if total <= budget:
        return sections
    return {
        name: _fit_text(text, max(800, int(budget * len(text) / total))) for name, text in sections.items()
    }


def _profile_block(ctx: AnalysisContext) -> str:
    """What the model needs to know about the kind of paper, which sections to expect and which keys to score."""
    profile = pp.get_profile(ctx.article_type)
    lines: list[str] = []
    if ctx.article_type in pp.ARTICLE_TYPES:
        origin = {
            "label": "the article type is printed in the file",
            "user": "chosen by the author",
        }.get(ctx.type_source, "inferred")
        lines.append(f'Article type: {profile.label} ({origin}). Set "paper_format" to "{ctx.article_type}".')
        lines.append("Sections expected in this type: " + (", ".join(profile.expected) or "none in particular") + ".")
        if profile.not_expected:
            lines.append(
                "Sections NOT expected in this type (never a weakness, never score them): "
                + ", ".join(profile.not_expected)
                + "."
            )
        keys = profile.criteria
        lines.append("Criteria (use exactly these keys): " + _criteria_help(keys))
    else:
        lines.append(
            'Article type: not stated. Decide from the text and set "paper_format" to one of research_article, '
            "communication, perspective, review, mini_review."
        )
        lines.append("If it is a research_article or communication, use these criteria keys: " + _criteria_help(pp.PROFILES[pp.RESEARCH_ARTICLE].criteria))
        lines.append("If it is a perspective, review or mini_review, use these criteria keys: " + _criteria_help(pp.PROFILES[pp.REVIEW].criteria))
    lines.append(
        "Do not score citation integrity; it is computed from the reference check. The Supporting Information is not provided."
    )
    if ctx.structure_mode == "derived":
        lines.append(
            "Section structure: the file has no reliable headings. Read the whole text and find 3 to 6 sections in the "
            "paper's own terms (for a chemistry paper: for example Design and synthesis, Characterization, "
            "Mechanism, Conclusion). For each, set source to \"derived\" and copy 8-14 words EXACTLY from the "
            "manuscript, where that section begins, into \"anchor\". List them in reading order. Do not list a "
            "section for content that is not in the text."
        )
    else:
        names = ", ".join(f'"{name}"' for name in ctx.section_texts)
        lines.append(
            "Section structure: headings were detected. Score ONLY these sections and use these names exactly "
            f"as \"name\" (source \"detected\", empty anchor): {names}."
        )
    return "\n".join(lines)


def _criteria_help(criteria: tuple[tuple[str, float], ...]) -> str:
    return "; ".join(f'"{key}" ({pp.CRITERION_HELP.get(key, key)})' for key, _ in criteria)


class AbstractAnalysisProvider(ABC):
    """Base class for all LLM analysis providers."""

    SYSTEM_PROMPT_A: str = _build_system_prompt(MODE_A)
    SYSTEM_PROMPT_B: str = _build_system_prompt(MODE_B)

    def get_system_prompt(self, mode: str) -> str:
        """Return the appropriate system prompt for the given mode."""
        return self.SYSTEM_PROMPT_A if mode.lower() == MODE_A else self.SYSTEM_PROMPT_B

    def build_user_message(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
    ) -> str:
        """Build a provider-independent user message. The paper text is trimmed to fit, the rest never is."""
        ctx = current_context()
        journal_json = json.dumps(journal_data, ensure_ascii=True)
        head = (
            "Analyse the following paper input and return strict JSON only.\n\n"
            f"Target journal:\n{target_journal}\n\n"
        )
        if ctx is not None:
            head += _profile_block(ctx) + "\n\n"
        tail = f"Citation summary:\n{citation_summary}\n\nJournal data:\n{journal_json}\n"

        derived = ctx is not None and ctx.structure_mode == "derived" and bool(ctx.body)
        fixed = len(head) + len(tail) + len(abstract) + 200
        budget = max(4000, min(BODY_BUDGET_CHARS, MAX_USER_MESSAGE_CHARS - fixed))
        if derived:
            paper = f"Manuscript text, in reading order (title, authors and abstract included):\n{_fit_text(ctx.body, budget)}"
        else:
            fitted = _fit_sections({k: str(v) for k, v in sections.items()}, budget)
            paper = f"Sections:\n{json.dumps(fitted, ensure_ascii=True)}"
        return f"{head}Abstract:\n{abstract}\n\n{paper}\n\n{tail}"[:MAX_USER_MESSAGE_CHARS]

    def parse_json_response(self, raw: str) -> dict[str, Any]:
        """Strip markdown fences and parse JSON content."""
        cleaned = raw.strip()
        if cleaned.startswith(JSON_CODE_FENCE):
            cleaned = cleaned[len(JSON_CODE_FENCE) :].strip()
            if cleaned.endswith(GENERIC_CODE_FENCE):
                cleaned = cleaned[: -len(GENERIC_CODE_FENCE)].strip()
        elif cleaned.startswith(GENERIC_CODE_FENCE):
            cleaned = cleaned[len(GENERIC_CODE_FENCE) :].strip()
            if cleaned.endswith(GENERIC_CODE_FENCE):
                cleaned = cleaned[: -len(GENERIC_CODE_FENCE)].strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError("Model response is not valid JSON") from exc

        if not isinstance(parsed, dict):
            raise ValueError("Model response JSON must be an object")
        return parsed

    def normalize_analysis_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Normalize provider JSON into an AnalysisResult-compatible, checked structure."""
        normalized = dict(parsed)

        rejection_reasons = normalized.get("rejection_reasons", [])
        normalized_reasons: list[dict[str, Any]] = []
        if isinstance(rejection_reasons, list):
            for reason in rejection_reasons:
                if isinstance(reason, dict):
                    normalized_reasons.append(reason)
                else:
                    normalized_reasons.append({"reason": str(reason)})
        else:
            normalized_reasons = [{"reason": str(rejection_reasons)}]
        normalized["rejection_reasons"] = normalized_reasons

        # sections, criteria, quotes and the overall score are verified or computed in code
        return finalize_payload(normalized, current_context())

    @abstractmethod
    async def analyse(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Analyse a paper and return structured results."""
        raise NotImplementedError

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """Generate text/JSON given a system prompt and user prompt."""
        raise NotImplementedError
