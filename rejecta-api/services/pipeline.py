"""The analysis pipeline behind POST /analyse-paper, callable without HTTP.

The API endpoint and the scoring evaluation (scripts/eval_scoring.py) both call `run_analysis`, so the
evaluation measures exactly what users get. The citation and journal lookups can be swapped for cached
versions, so repeated evaluation runs see the same external data.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from models import (
    AnalysisResult,
    CitationResult,
    CitationStatus,
    CitationSummary,
    ConsistencyReport,
    Disclosures,
    JournalMatch,
    ParsedDocument,
    PublicationStatus,
    RevisionComparison,
)
from services import revisions
from services import paper_profile
from services import section_review
from services.analysis_guard import AnalysisContext, paper_context
from services.citations import MAX_REFERENCES, build_citation_summary, verify_citations
from services.consistency import check_consistency
from services.disclosures import read_disclosures
from services.evidence_check import check_support
from services.integrity import check_publication, same_journal
from services.journals import match_journal
from services.llm import get_provider_cached

logger = logging.getLogger(__name__)

VerifyCitations = Callable[[list[str]], Awaitable[list[CitationResult]]]
MatchJournal = Callable[[str, str], Awaitable[JournalMatch]]
CheckPublication = Callable[[ParsedDocument], Awaitable[PublicationStatus]]
CheckConsistency = Callable[[ParsedDocument], Awaitable[ConsistencyReport]]


@dataclass
class PipelineResult:
    analysis: AnalysisResult
    citations: CitationSummary
    journal: JournalMatch
    context: AnalysisContext
    revision: RevisionComparison | None = None
    integrity: PublicationStatus | None = None
    consistency: ConsistencyReport | None = None
    disclosures: Disclosures | None = None


def citation_summary_text(summary: CitationSummary) -> str:
    """The citation facts the model is given. Editorial warnings stay in the API response for the UI;
    feeding them to the model skewed scores (e.g. 75 -> 38 on a landmark paper)."""
    not_found = summary.unverified - summary.check_incomplete
    return (
        f"{summary.verified} verified, "
        f"{summary.ghost} ghost (DOI or arXiv ID does not exist in CrossRef/arXiv; "
        f"possible fabricated reference), "
        f"{not_found} unverified (checked, no matching record found), "
        f"{summary.check_incomplete} not checked (a lookup service was busy, so nothing can be "
        f"concluded about them) "
        f"out of {summary.total} total references."
    )


async def run_analysis(
    parsed: ParsedDocument,
    journal: str,
    mode: str,
    *,
    verify: VerifyCitations = verify_citations,
    match: MatchJournal = match_journal,
    check: CheckPublication = check_publication,
    consistency: CheckConsistency | None = None,
    previous: revisions.PreviousAnalysis | None = None,
    review_sections_enabled: bool = True,
) -> PipelineResult:
    """Check citations and the journal, then run the model and its checks on a parsed paper.

    `previous` is an earlier analysis of a version of this paper (see services.revisions): its issues are
    put to the model, and the result is compared with it. A failed citation or journal lookup never fails
    the analysis; a failed model call raises.
    """
    def default_consistency(paper: ParsedDocument) -> Awaitable[ConsistencyReport]:
        return check_consistency(paper, get_provider_cached())

    citation_outcome, journal_outcome, integrity_outcome, consistency_outcome = await asyncio.gather(
        verify(parsed.references),
        match(parsed.abstract, journal),
        check(parsed),
        (consistency or default_consistency)(parsed),
        return_exceptions=True,
    )
    if isinstance(consistency_outcome, BaseException):
        logger.warning("Consistency check failed; continuing without it", exc_info=consistency_outcome)
        consistency_report = ConsistencyReport(status="not_checked", note="The consistency check could not be completed.")
    else:
        consistency_report = consistency_outcome
    try:
        disclosures: Disclosures | None = read_disclosures(parsed)
    except Exception:  # noqa: BLE001 - context only
        logger.warning("Reading the disclosure statements failed", exc_info=True)
        disclosures = None
    if isinstance(integrity_outcome, BaseException):
        logger.warning("Publication status check failed; continuing without it", exc_info=integrity_outcome)
        integrity: PublicationStatus | None = PublicationStatus(status="not_checked", file_marks=list(parsed.integrity_marks))
        if parsed.integrity_marks:  # the file's own marks still count
            integrity.status = "retracted" if any(m.kind != "expression_of_concern" for m in parsed.integrity_marks) else "expression_of_concern"
    else:
        integrity = integrity_outcome

    if isinstance(citation_outcome, Exception):
        logger.warning("Citation lookup failed; marking all unverified", exc_info=citation_outcome)
        citation_results = [
            CitationResult(raw=ref, status=CitationStatus.unverified) for ref in parsed.references[:MAX_REFERENCES]
        ]
    else:
        citation_results = citation_outcome

    if isinstance(journal_outcome, Exception):
        logger.warning("Journal lookup failed; continuing without a match", exc_info=journal_outcome)
        journal_data = JournalMatch(found=False, target=journal)
    else:
        journal_data = journal_outcome

    if integrity is not None and integrity.status in ("published", "corrected"):
        integrity.at_target_journal = same_journal(
            integrity, journal, journal_data.openalex_name if journal_data.found else None
        )
    citation_summary = build_citation_summary(citation_results)
    summary_text = citation_summary_text(citation_summary)
    journal_dump = journal_data.model_dump()

    context = AnalysisContext(
        source_text=parsed.full_text,
        article_type=parsed.article_type or paper_profile.UNKNOWN,
        type_source=parsed.article_type_source,
        structure_mode="detected" if parsed.structure == "detected" else "derived",
        section_texts={k: v for k, v in parsed.sections.items() if k in parsed.section_roles},
        section_roles=parsed.section_roles,
        section_titles=dict(parsed.section_titles),
        abstract=parsed.abstract,
        body=parsed.body,
        refers_to_si=parsed.refers_to_si,
        citations_total=citation_summary.total,
        citations_verified=citation_summary.verified,
        citations_ghost=citation_summary.ghost,
        citations_unchecked=citation_summary.check_incomplete,
        extra_text=f"{summary_text} {journal_dump}",
        numbers_text=f"{parsed.abstract} {parsed.body}",
        parse_warnings=list(parsed.parse_warnings),
        # the very same file: nothing changed, so the model is not asked which earlier issues are resolved
        previous_issues=previous.issues() if previous is not None and not previous.same_file else [],
        previous_sections=previous.section_names() if previous is not None else [],
        publication=integrity,
        consistency=consistency_report,
        disclosures=disclosures,
    )
    provider = get_provider_cached()
    # The section-by-section review. With headings from the parser it needs nothing from the main analysis,
    # so it runs at the same time; without them it waits for the sections the main analysis finds.
    early_review: asyncio.Task[section_review.SectionReview] | None = None
    if context.structure_mode == "detected" and review_sections_enabled:
        early_review = asyncio.create_task(
            section_review.review_sections(section_review.inputs_from_parsed(context), journal, provider, context)
        )
    try:
        with paper_context(context):
            analysis = await provider.analyse(
                abstract=parsed.abstract,
                sections=parsed.sections,
                target_journal=journal,
                citation_summary=summary_text,
                journal_data=journal_dump,
                mode=mode,
            )
            # Stage 4: does each quoted passage support the concern it is given for? (a second, cheap model call)
            analysis = await check_support(analysis, provider, context)
    except BaseException:
        if early_review is not None:
            early_review.cancel()
        raise
    if early_review is not None:
        review = await early_review
    elif review_sections_enabled:
        review = await section_review.review_sections(
            section_review.inputs_from_analysis(context, analysis), journal, provider, context
        )
    else:
        review = section_review.SectionReview()
    analysis = section_review.apply_review(analysis, review, context)
    revision = revisions.compare(previous, analysis, journal) if previous is not None else None
    return PipelineResult(
        analysis=analysis, citations=citation_summary, journal=journal_data, context=context, revision=revision,
        integrity=integrity,
        consistency=consistency_report,
        disclosures=disclosures,
    )
