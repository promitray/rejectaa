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
    JournalMatch,
    ParsedDocument,
    RevisionComparison,
)
from services import revisions
from services import paper_profile
from services.analysis_guard import AnalysisContext, paper_context
from services.citations import MAX_REFERENCES, build_citation_summary, verify_citations
from services.journals import match_journal
from services.llm import get_provider_cached

logger = logging.getLogger(__name__)

VerifyCitations = Callable[[list[str]], Awaitable[list[CitationResult]]]
MatchJournal = Callable[[str, str], Awaitable[JournalMatch]]


@dataclass
class PipelineResult:
    analysis: AnalysisResult
    citations: CitationSummary
    journal: JournalMatch
    context: AnalysisContext
    revision: RevisionComparison | None = None


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
    previous: revisions.PreviousAnalysis | None = None,
) -> PipelineResult:
    """Check citations and the journal, then run the model and its checks on a parsed paper.

    `previous` is an earlier analysis of a version of this paper (see services.revisions): its issues are
    put to the model, and the result is compared with it. A failed citation or journal lookup never fails
    the analysis; a failed model call raises.
    """
    citation_outcome, journal_outcome = await asyncio.gather(
        verify(parsed.references),
        match(parsed.abstract, journal),
        return_exceptions=True,
    )

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
        previous_issues=previous.issues() if previous is not None else [],
    )
    with paper_context(context):
        analysis = await get_provider_cached().analyse(
            abstract=parsed.abstract,
            sections=parsed.sections,
            target_journal=journal,
            citation_summary=summary_text,
            journal_data=journal_dump,
            mode=mode,
        )
    revision = revisions.compare(previous, analysis, journal) if previous is not None else None
    return PipelineResult(
        analysis=analysis, citations=citation_summary, journal=journal_data, context=context, revision=revision
    )
