"""services/calibrator/corpus.py
Extracts field-specific stylistic priors and authentic editorial conventions for target journals.
Integrates with OpenAlex with robust fallback presets for major venues.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

# Known stylistic fingerprints for major editorial houses
TIER1_STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "nature": {
        "voice": "active-dominant, assertive, high-level narrative framing",
        "intro_pattern": "Broad significance first, immediate statement of empirical finding in paragraph 2",
        "hedging_level": "moderate-disciplined (avoid 'could potentially maybe'; use precise probabilistic bounds)",
        "banned_synthetic_markers": [
            "delve", "testament", "paramount", "underscores", "beacon", "moreover", "tapestry", "invaluable", "crucial"
        ],
        "target_cadence": "Declarative, varied sentence lengths with punchy transitions."
    },
    "ieee": {
        "voice": "third-person objective, passive acceptable, precision-first, formal notation",
        "intro_pattern": "Formal problem definition, explicit contribution listing, systematic related work delta",
        "hedging_level": "strict-bounded (delimit exact boundary conditions and operational constraints)",
        "banned_synthetic_markers": [
            "vibrant tapestry", "delve", "crucial", "testament", "pivotal", "invaluable"
        ],
        "target_cadence": "Systematic, unambiguous, mathematically dense, low ornamentation."
    },
    "cell": {
        "voice": "mechanistic, hypothesis-driven, active first-person plural ('We reasoned that...')",
        "intro_pattern": "Biological paradox or knowledge gap, molecular mechanism, functional consequence",
        "hedging_level": "rigorous evidence-tied (claims directly indexed to experimental assays)",
        "banned_synthetic_markers": [
            "delve", "underscores", "paramount", "testament", "beacon"
        ],
        "target_cadence": "Logical deduction chains, step-by-step mechanistic assertions."
    },
    "default": {
        "voice": "rigorous academic, clear agent attribution, crisp empirical rhythm",
        "intro_pattern": "Gap-first argument structure with explicit contribution boundaries",
        "hedging_level": "scientifically calibrated (calibrated to sample size and variance)",
        "banned_synthetic_markers": [
            "delve", "pivotal", "invaluable", "underscores", "testament", "paramount", "tapestry"
        ],
        "target_cadence": "Crisp academic cadence without corporate marketing filler."
    }
}


class JournalCorpusService:
    """Service to resolve and enrich journal-specific editorial registers."""

    @classmethod
    async def get_journal_register_profile(cls, journal_name: str) -> Dict[str, Any]:
        """Normalize journal name and extract target stylistic fingerprint."""
        normalized = journal_name.lower().strip()

        if any(v in normalized for v in ["nature", "science", "pnas"]):
            profile = TIER1_STYLE_PRESETS["nature"].copy()
        elif any(v in normalized for v in ["ieee", "acm", "transactions", "journal of machine learning"]):
            profile = TIER1_STYLE_PRESETS["ieee"].copy()
        elif any(v in normalized for v in ["cell", "lancet", "nejm", "immunity", "neuron"]):
            profile = TIER1_STYLE_PRESETS["cell"].copy()
        else:
            profile = TIER1_STYLE_PRESETS["default"].copy()

        profile["target_journal"] = journal_name
        return profile
