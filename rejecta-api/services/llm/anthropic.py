"""Anthropic-backed LLM analysis provider."""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from models import AnalysisResult
from services.llm.base import COMPACT_RETRY_NOTE, AbstractAnalysisProvider, MODE_A

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000  # the general-purpose tools (Calibrator, Forge, ...)
ANALYSIS_MAX_TOKENS = 8000  # the full report; at 4000 long reports were cut off mid-JSON
FIX_JSON_MESSAGE = (
    "Your previous response was not valid JSON matching the required schema. Return only the corrected, "
    "valid JSON."
)
SERVER_ERROR_MESSAGE = "Failed to parse model output after retry"

logger = logging.getLogger(__name__)


class AnthropicProvider(AbstractAnalysisProvider):
    """Analysis provider using Anthropic Claude."""

    def __init__(self, api_key: str, model: str = ANTHROPIC_DEFAULT_MODEL) -> None:
        """Create an async Anthropic client and bind model name."""
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """General-purpose structured JSON generation using Anthropic."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "text", None)
        )

    async def _complete(self, system_prompt: str, messages: list[dict[str, str]]) -> tuple[str, bool, str]:
        """(text, whether it was cut off at the token limit, the exact model that answered)."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=ANALYSIS_MAX_TOKENS,
            temperature=0,
            system=system_prompt,
            messages=messages,
        )
        text = "".join(block.text for block in response.content if getattr(block, "text", None))
        return text, response.stop_reason == "max_tokens", getattr(response, "model", None) or self.model

    def _result(self, text: str, model_version: str) -> AnalysisResult:
        parsed = self.parse_json_response(text)
        parsed["provider_used"] = f"anthropic/{model_version}"
        return AnalysisResult.model_validate(self.normalize_analysis_payload(parsed))

    async def analyse(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Run analysis through Anthropic and return validated result.

        A report cut off at the token limit is asked for again, shorter. An answer that is not valid JSON or
        fails validation is sent back once for repair; if that fails too, the error is raised.
        """
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )
        messages = [{"role": "user", "content": user_message}]
        text, cut_off, version = await self._complete(system_prompt, messages)
        if cut_off:
            logger.warning("Analysis output hit the token limit; asking for a shorter report")
            messages = [{"role": "user", "content": f"{user_message}\n\n{COMPACT_RETRY_NOTE}"}]
            text, _, version = await self._complete(system_prompt, messages)

        try:
            return self._result(text, version)
        except Exception:
            logger.warning("Analysis output failed validation; asking the model to repair it", exc_info=True)
            retry_text, _, version = await self._complete(
                system_prompt,
                [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": FIX_JSON_MESSAGE}],
            )
            try:
                return self._result(retry_text, version)
            except Exception as exc:
                raise ValueError(SERVER_ERROR_MESSAGE) from exc
