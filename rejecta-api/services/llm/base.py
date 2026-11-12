"""Provider-agnostic base utilities for paper analysis."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from config import settings
from models import AnalysisResult
from services import paper_profile as pp
from services.analysis_guard import AnalysisContext, current_context, finalize_payload

# Bump when the prompt or the scoring changes: it is part of the cache key, so old results are not reused.
PROMPT_VERSION = "2026-11-13.4"  # .4: detected sections listed with the heading the paper prints
MODE_A = "a"
MODE_B = "b"
MESSAGE_OVERHEAD_CHARS = 40_000  # room for the instructions, citation summary and journal data
MIN_BODY_BUDGET_CHARS = 4000
OMITTED_MARKER = " [... text omitted to fit ...] "
OMISSION_NOTE = (
    "NOTE: the manuscript is longer than this model can take, so part of its text was left out where you see "
    f"'{OMITTED_MARKER.strip()}'. Never treat content you were not shown as missing from the paper, and never "
    "write a rejection reason about it."
)
JOURNAL_DATA_NOTE = (
    "How to read the journal data: recent_papers are the journal's newest research articles and similar_papers "
    "its articles closest to this manuscript; use them as context for journal fit and novelty. "
    "two_year_mean_citedness indicates the journal's standing (higher means more selective). If match_quality is "
    '"approximate", the record may describe a different journal from the one named: judge fit by the target '
    "journal named above."
)
COMPACT_RETRY_NOTE = (
    "IMPORTANT: a previous answer to this request was too long and was cut off. Keep every text field short: "
    "oracle_letter at most 150 words, one sentence per section field, at most 4 rejection reasons."
)
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
CRITERIA SCALE. Score every criterion against what the TARGET JOURNAL actually publishes, not against a perfect paper:
- 90-100: among the strongest papers this journal publishes.
- 75-89: typical of the papers this journal accepts; the weaknesses are minor.
- 60-74: publishable here after revision; real but fixable weaknesses.
- 40-59: serious weaknesses; a major revision with an uncertain outcome.
- 0-39: not publishable in this journal as it stands.
Give each criterion the score the paper earns AT THIS JOURNAL. A paper that would be a typical, well-executed article of this journal belongs at 75 or above; do not lower a score to appear rigorous, and do not raise one because the work is sound but aimed at a different readership. The editor's tone of voice never changes a score.
journal_fit asks whether this journal's editor would consider the paper in scope and at the journal's level, for its article type. A broad, top-tier journal publishes only work whose significance reaches well beyond its own field; a sound specialist paper, or an unsolicited review, sent there has a journal_fit below 40, and a desk_reject for scope or level is then correct. A paper squarely in the scope of a specialist journal has a high journal_fit there.

rejection_reasons: list the real issues a careful reviewer at this journal would raise, most important first, at most 5. Do not pad the list: for a strong paper one or two minor reasons, or none, is the right answer. Every reason MUST have all four parts, and code deletes a reason that lacks one:
1. problem: exactly what the weakness is.
2. evidence: the exact words of the manuscript where this concern shows (for a missing control or an unsupported claim, copy the sentence that makes the claim). Code works out the section and figure from where those words sit, so do not describe the location yourself.
3. impact: how it affects novelty, scientific validity or publication potential, and why a reviewer would act on it.
4. solution: the specific change, tied to the evidence and the problem. Do not write a solution such as 'strengthen the discussion' or 'clarify the implications' that would fit any paper; a solution that shares nothing specific with the problem and the evidence is deleted.
Derive every reason from this paper's own claims and data. Never write a reason you cannot tie to a specific passage. Before saying the paper lacks something (a control, a comparison, a discussion of a topic), check the whole text: a reason about content the paper does contain is wrong.
REFERENCES are checked separately by code. "Unverified" only means the databases hold no record of a reference: books, reports, theses, patents and older or regional work are often missing. Never make unverified references a rejection reason, and never lower a score for them. Only "ghost" references (a DOI or arXiv id that does not exist) are a concern.
severity must be one of: desk_reject, major, minor.
- desk_reject: an editor would return the paper without sending it to review: clearly outside the journal's scope, a fatal flaw in the design or data that the text shows, or a contribution clearly below what this journal publishes. Use it only when you can point to the passage that shows it, and never for a problem that a revision could fix.
- major: reviewers would require substantial revision before acceptance.
- minor: a real issue that would not prevent acceptance if addressed in revision.
Calibrate to the TARGET JOURNAL: a top-tier journal (Nature, Science, Cell, JACS, Angewandte and equivalent) expects broader significance and more complete evidence than a specialist journal. Judge the paper by the standard of the journal named, not a stricter or a looser one.

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
  "paper_format": "<exactly one of: research_article, communication, perspective, review, mini_review, editorial>",
  "field": "<the paper's scientific field in 2-5 words, in that field's own terms>",
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
      "severity": "<exactly one of: desk_reject, major, minor>",
      "criterion": "<the key of the criterion this concern lowers, exactly as listed in criteria>"
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
            "Your job: give an honest, specific and fair assessment of whether this paper will survive desk review "
            "at the target journal, as its most experienced handling editor would. Reference specific sections and "
            "claims. Do not write generic advice that could apply to any paper."
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
        "You are Professor Aldric Frowns, a demanding but fair journal editor with 34 years on editorial boards "
        "at leading scientific journals. Your manner is dry and exacting; your judgement is accurate. You reject "
        "what deserves rejection and you recognise good work when you see it.\n"
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
        if ctx.article_type in pp.NON_RESEARCH_TYPES:
            lines.append(
                "This is not a research paper. Judge it as what it is: whether it does its job, is accurate and reads "
                "well. Do not ask for new data, methods, novelty or a research contribution, and never use "
                "desk_reject."
            )
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
            "communication, perspective, review, mini_review, editorial (an editorial, foreword or introduction to "
            "a themed issue: invited, with no research of its own)."
        )
        lines.append("If it is a research_article or communication, use these criteria keys: " + _criteria_help(pp.PROFILES[pp.RESEARCH_ARTICLE].criteria))
        lines.append("If it is a perspective, review or mini_review, use these criteria keys: " + _criteria_help(pp.PROFILES[pp.REVIEW].criteria))
        lines.append(
            "If it is an editorial, use these criteria keys and never desk_reject it: "
            + _criteria_help(pp.PROFILES[pp.EDITORIAL].criteria)
        )
    lines.append(
        "Do not score citation integrity; it is computed from the reference check. The Supporting Information is not provided."
    )
    lines.extend(publication_lines(ctx))
    lines.extend(consistency_lines(ctx))
    if ctx.structure_mode == "derived":
        names = pp.derived_section_names(ctx.article_type if ctx.article_type in pp.ARTICLE_TYPES else None)
        listed = ", ".join(f'"{name}" (role {role})' for name, role in names)
        lines.append(
            "Section structure: the file has no reliable headings. Divide the text into sections using ONLY these "
            f"names, in this order: {listed}. Use a name only for content that is really in the text (a "
            "communication often has no separate Experimental section: then leave it out), and never invent other "
            "names. For each section set source to \"derived\" and copy 8-14 words EXACTLY from the manuscript, "
            "where that section begins, into \"anchor\"."
        )
        kept = [n for n in ctx.previous_sections if n in {name for name, _ in names}]
        if kept:
            lines.append(
                "The earlier version of this manuscript was divided into these sections: "
                + ", ".join(f'"{n}"' for n in kept)
                + ". Divide this version the same way: use exactly these sections, in this order, for as long as "
                "their content is still in the text. Leave one out only when its content is really gone, and add "
                "another name from the list above only when the text now has content that none of these can hold."
            )
    else:
        def listed_name(name: str) -> str:
            printed = ctx.section_titles.get(name, "")
            return f'"{name}" (headed "{printed}" in the paper)' if printed and printed.lower() != name.lower() else f'"{name}"'

        names = ", ".join(listed_name(name) for name in ctx.section_texts)
        lines.append(
            "Section structure: headings were detected. Score ONLY these sections and use these names exactly "
            f"as \"name\" (source \"detected\", empty anchor): {names}. When you write about a section, call it "
            "by its heading in the paper."
        )
    return "\n".join(lines)


# Two-year mean citedness of a journal's articles (OpenAlex) -> how selective it is. Nature and Science are
# about 24, JACS 18, Angewandte and Chem. Sci. 8-10, ChemComm and Macromol. Rapid Commun. about 4.5.
_STANDING_BANDS = (
    (15.0, "top-tier, broad-scope journal. It publishes only work of exceptional significance far beyond its own field."),
    (7.0, "high-impact journal. It expects clearly significant advances of interest across its whole field."),
    (2.5, "established specialist journal. It publishes sound, useful advances within its field."),
    (0.0, "specialist or regional journal with modest citation impact."),
)


def previous_issues_block(issues: list[dict[str, Any]]) -> str:
    """Instructions for a revised paper: the earlier version's issues, to be judged one by one."""
    listed = "\n".join(f'{item["id"]}. [{item["severity"]}] {item["problem"]}' for item in issues)
    return (
        "PREVIOUS VERSION. This manuscript is a revised version of one the author had analysed before. That "
        f"analysis raised these issues:\n{listed}\n"
        'Add a field "previous_issues" to your JSON: a list with one entry for EACH issue above, '
        '{"id": <its number>, "status": "resolved" | "partly_resolved" | "open", "note": "<one sentence: what '
        'in this version resolves it, or what is still missing>"}. Judge only from the text of this version. '
        "Score this version on its own merits: do not raise or lower any score because it is a revision, and "
        "list in rejection_reasons only the issues that remain or are new."
    )


def journal_standing(journal_data: dict[str, Any]) -> str:
    """How selective the target journal is, in words, from its citation data. Empty when unknown or uncertain."""
    if not journal_data.get("found") or journal_data.get("match_quality") == "approximate":
        return ""
    citedness = journal_data.get("two_year_mean_citedness")
    if not isinstance(citedness, (int, float)):
        return ""
    for floor, words in _STANDING_BANDS:
        if citedness >= floor:
            return f"{words} (its articles are cited {citedness:.1f} times each within two years)"
    return ""


def publication_lines(ctx: AnalysisContext) -> list[str]:
    """What the publication record says about this paper, so the model does not review a published or retracted
    paper as if it were a fresh submission. The decision that follows from it is made by code, not by the model."""
    pub = ctx.publication
    if pub is None:
        return []
    where = " ".join(part for part in (f"in {pub.journal}" if pub.journal else "", f"({pub.year})" if pub.year else "") if part)
    if pub.status in ("retracted", "expression_of_concern"):
        what = "has been retracted" if pub.status == "retracted" else "is under an expression of concern"
        return [
            f"Publication record (checked by code, not by you): this article {what}. The recommendation that follows "
            "from this is set by code. Judge the text on its own merits; do not guess why it was retracted, and do not "
            "invent problems to explain it."
        ]
    if pub.status in ("published", "corrected"):
        base_line = (
            f"Publication record (checked by code): this paper is already published {where}".rstrip()
            + " and passed peer review there. Raise only concrete problems that a quoted passage shows. Use major "
            "only for a problem a reviewer would require to be fixed; wording and style points are minor. An empty "
            "rejection_reasons list is a valid answer."
        )
        if pub.at_target_journal:
            return [base_line + " The target journal is the journal that published it: never use severity desk_reject."]
        return [
            base_line + " The target journal is a different one: judge scope and level for the target journal, and use "
            "desk_reject only for scope or level there, never for a flaw in the work itself."
        ]
    return []


def consistency_lines(ctx: AnalysisContext) -> list[str]:
    """Facts code established before the review: inconsistencies in the paper's own values, and its disclosures."""
    lines: list[str] = []
    report = ctx.consistency
    if report is not None and report.findings:
        listed = " ".join(f"({n}) {f.message}" for n, f in enumerate(report.findings, start=1))
        lines.append(
            "Consistency check (done by code; every item quotes the manuscript): " + listed + " Weigh these when you "
            "score rigor and evidence. Do NOT list them in rejection_reasons: code adds them to the report itself."
        )
    disclosed = ctx.disclosures
    if disclosed is not None and disclosed.interests:
        lines.append(
            "Disclosed interests (from the paper's own statement): " + ", ".join(disclosed.interests) + ". Where a claim "
            "about the work's promise or safety rests on few data, say that independent confirmation matters. A "
            "disclosed interest is not misconduct and is not a rejection reason."
        )
    return lines


def _criteria_help(criteria: tuple[tuple[str, float], ...]) -> str:
    return "; ".join(f'"{key}" ({pp.CRITERION_HELP.get(key, key)})' for key, _ in criteria)


class AbstractAnalysisProvider(ABC):
    """Base class for all LLM analysis providers."""

    # Characters of manuscript text this provider's model can take. None: settings.ANALYSIS_BODY_BUDGET_CHARS.
    body_budget_chars: int | None = None

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
        standing = journal_standing(journal_data)
        if standing:
            head += f"Target journal standing: {standing}\n\n"
        if ctx is not None:
            head += _profile_block(ctx) + "\n\n"
        tail = (
            f"Citation summary:\n{citation_summary}\n\nJournal data:\n{journal_json}\n"
            f"{JOURNAL_DATA_NOTE}\n"
        )

        derived = ctx is not None and ctx.structure_mode == "derived" and bool(ctx.body)
        budget = max(MIN_BODY_BUDGET_CHARS, getattr(self, "body_budget_chars", None) or settings.ANALYSIS_BODY_BUDGET_CHARS)
        if derived:
            text = _fit_text(ctx.body, budget)
            cut = len(text) < len(ctx.body)
            paper = f"Manuscript text, in reading order (title, authors and abstract included):\n{text}"
        else:
            plain = {k: str(v) for k, v in sections.items()}
            fitted = _fit_sections(plain, budget)
            cut = fitted != plain
            paper = f"Sections:\n{json.dumps(fitted, ensure_ascii=True)}"
        if cut:
            head += OMISSION_NOTE + "\n\n"
        if ctx is not None and ctx.previous_issues:
            tail += "\n" + previous_issues_block(ctx.previous_issues) + "\n"
        message = f"{head}Abstract:\n{abstract}\n\n{paper}\n\n{tail}"
        return message[: budget + MESSAGE_OVERHEAD_CHARS]

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
