"""Scenario agent adapter for Rejecta's Paper Analysis pipeline."""

from __future__ import annotations

import logging
from pathlib import Path

import scenario
from models import ParsedDocument
from scripts.eval_scoring import cached_lookups
from services.parser import parse_document
from services.pipeline import PipelineResult, run_analysis

logger = logging.getLogger(__name__)


class PaperAnalysisAdapter(scenario.AgentAdapter):
    """Bridges Rejecta's core Paper Analysis pipeline to LangWatch Scenario simulations."""

    def __init__(
        self,
        paper_path: Path,
        target_journal: str,
        mode: str = "rigorous",
        live_lookups: bool = False,
    ) -> None:
        super().__init__()
        self.paper_path = paper_path
        self.target_journal = target_journal
        self.mode = mode
        self.live_lookups = live_lookups

        self.parsed: ParsedDocument | None = None
        self.last_result: PipelineResult | None = None
        self.execution_error: Exception | None = None

    async def call(self, input: scenario.AgentInput | None = None) -> scenario.AgentReturnTypes:
        """Execute Rejecta paper analysis and return formatted review for simulation."""
        if not self.paper_path.exists():
            raise FileNotFoundError(f"Paper PDF not found at {self.paper_path}")

        # 1. Parse PDF bytes
        pdf_bytes = self.paper_path.read_bytes()
        self.parsed = parse_document(pdf_bytes, filename=self.paper_path.name)

        # 2. Setup lookup hooks (cached by default to prevent rate limits)
        verify_fn, match_fn = cached_lookups(live=self.live_lookups)

        # 3. Execute analysis pipeline
        try:
            self.last_result = await run_analysis(
                parsed=self.parsed,
                journal=self.target_journal,
                mode=self.mode,
                verify=verify_fn,
                match=match_fn,
            )
        except Exception as exc:
            self.execution_error = exc
            logger.error("Analysis pipeline failed for %s: %s", self.paper_path.name, exc)
            return f"Analysis failed with error: {exc}"

        analysis = self.last_result.analysis
        citations = self.last_result.citations
        integrity = self.last_result.integrity

        # Format report for the Scenario Judge / User Simulator
        reasons_text = "\n".join(
            f"  - [{r.severity.value if hasattr(r.severity, 'value') else r.severity}] {r.reason} "
            f"(Evidence quote: \"{r.evidence}\")"
            for r in analysis.rejection_reasons
        ) or "  - None noted"

        status_text = integrity.status if integrity else "unknown"

        summary = (
            f"=== Rejecta Analysis Report ===\n"
            f"File: {self.paper_path.name}\n"
            f"Target Journal: {self.target_journal}\n"
            f"Detected Article Type: {self.parsed.article_type or 'unknown'}\n"
            f"Publication Integrity Status: {status_text}\n"
            f"Overall Score: {analysis.mood_score}/100\n"
            f"Verdict: {analysis.verdict}\n"
            f"Journal Fit: {analysis.journal_fit}\n"
            f"Novelty Assessment: {analysis.novelty_assessment}\n"
            f"Citation Integrity: {citations.verified} verified, {citations.ghost} ghost out of {citations.total}\n"
            f"Key Critical Concerns & Reasons:\n{reasons_text}\n"
            f"================================"
        )
        return summary
