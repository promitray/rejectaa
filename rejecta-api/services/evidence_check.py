"""Stage 4, second half: does each quoted passage actually support the concern it is given for?

The first check (services.analysis_guard) makes sure a concern's quotation is really in the paper. That is not
enough: a true quotation can be attached to a concern it says nothing about. A real case: "Lack of detail in the
synthesis steps", supported by "The prepared nanocatalyst was identified by TEM images recorded using an EM10C-100
kV series microscope", a sentence about characterisation, while the synthesis was described in detail.

So one more, short model call is made: for each concern the model raised, does the quotation show the problem
(yes / partly / no)? A "no" moves the concern to the unverified concerns, with the reason, and the score is
computed again (a concern that is not supported must not cap a criterion). Concerns written by code (publication
record, consistency check) are not sent: they are facts, not judgements.

Never raises: if the call fails, the concerns stay as they are and the report says the check was not done.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from models import AnalysisResult
from services.analysis_guard import AnalysisContext, score_report

logger = logging.getLogger(__name__)

SUPPORT_PROMPT_VERSION = "2026-11-13.1"
MAX_WHY_CHARS = 240

_SYSTEM = (
    "You check reviewer comments against the manuscript passage each one quotes. You are strict and literal. "
    "Return ONLY valid JSON."
)

_INSTRUCTIONS = """For each item below, decide whether the QUOTED PASSAGE, read on its own, shows the PROBLEM.

- "yes": the passage is the claim, value or method the problem is about, and it shows the problem.
- "partly": the passage is about the same thing, but the problem also depends on text elsewhere.
- "no": the passage is about something else, or it contradicts the problem (for example the problem says a
  detail is missing and the passage is about a different step, or gives that detail).

Return {"verdicts": [{"id": <id>, "supports": "yes" | "partly" | "no", "why": "<one short sentence>"}]}, one
verdict per item.

ITEMS:
"""


def _items(analysis: AnalysisResult) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, {"id": index + 1, "problem": reason.problem or reason.reason, "quoted_passage": reason.evidence})
        for index, reason in enumerate(analysis.rejection_reasons)
        if reason.origin == "model" and reason.evidence.strip()
    ]


def _verdicts(raw: Any) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for item in (raw or {}).get("verdicts") or [] if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("supports") or "").strip().lower()
        if verdict in ("yes", "partly", "no") and number not in out:
            out[number] = (verdict, re.sub(r"\s+", " ", str(item.get("why") or "")).strip()[:MAX_WHY_CHARS])
    return out


async def check_support(analysis: AnalysisResult, provider: Any, ctx: AnalysisContext | None) -> AnalysisResult:
    """The report with each unsupported model concern moved to the unverified concerns, and the score recomputed."""
    items = _items(analysis)
    if not items or ctx is None or not hasattr(provider, "generate"):
        return analysis
    prompt = _INSTRUCTIONS + json.dumps([item for _, item in items], ensure_ascii=False, indent=1)
    try:
        raw = await provider.generate(_SYSTEM, prompt, temperature=0.0)
        parsed = provider.parse_json_response(raw) if hasattr(provider, "parse_json_response") else json.loads(raw)
    except Exception:  # noqa: BLE001 - the report stands without this check
        logger.warning("Evidence support check failed", exc_info=True)
        note = "Whether each quoted passage supports its concern could not be checked this time."
        return analysis.model_copy(update={"grounding_notes": [*analysis.grounding_notes, note]})

    verdicts = _verdicts(parsed)
    unsupported = {index for index, item in items if verdicts.get(item["id"], ("", ""))[0] == "no"}
    if not unsupported:
        return analysis

    payload = analysis.model_dump(mode="json")
    kept, moved = [], []
    for index, reason in enumerate(payload["rejection_reasons"]):
        if index in unsupported:
            why = verdicts[index + 1][1]
            reason["unverified_reason"] = (
                "The quoted passage does not show this problem" + (f": {why}" if why else ".")
            )
            moved.append(reason)
        else:
            kept.append(reason)
    payload["rejection_reasons"] = kept
    payload["unverified_concerns"] = moved + list(payload.get("unverified_concerns") or [])
    payload["grounding_notes"] = list(payload.get("grounding_notes") or []) + [
        f"{len(moved)} concern(s) quoted a passage that does not show the problem; they are listed as unverified "
        "and do not affect the score."
    ]
    score_report(payload, ctx)
    return AnalysisResult.model_validate(payload)
