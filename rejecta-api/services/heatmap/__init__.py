"""services/heatmap package export."""

from services.heatmap.engine import (
    HeatmapAnalysisResult,
    HeatmapEngine,
    ParagraphHeatDetail,
    SentenceHeatDetail,
)
from services.heatmap.segmenter import DocumentSegmenter, ParagraphSpan, SentenceSpan
from services.heatmap.stylometrics import StylometricAnalyzer

__all__ = [
    "HeatmapEngine",
    "HeatmapAnalysisResult",
    "ParagraphHeatDetail",
    "SentenceHeatDetail",
    "DocumentSegmenter",
    "ParagraphSpan",
    "SentenceSpan",
    "StylometricAnalyzer",
]
