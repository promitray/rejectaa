"""Anthropic-backed LLM analysis provider."""

from __future__ import annotations

from typing import Any

import anthropic
from fastapi import HTTPException

from models import AnalysisResult
from services.llm.base import AbstractAnalysisProvider, MODE_A

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000
FIX_JSON_MESSAGE = "Your prior response was invalid JSON. Return only corrected valid JSON."
SERVER_ERROR_MESSAGE = "Failed to parse model output after retry"


class AnthropicProvider(AbstractAnalysisProvider):
    """Analysis provider using Anthropic Claude."""

    def __init__(self, api_key: str, model: str = ANTHROPIC_DEFAULT_MODEL) -> None:
        """Create an async Anthropic client and bind model name."""
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def analyse(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Run analysis through Anthropic and return validated result."""
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )

        first = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        first_text = "".join(
            block.text for block in first.content if getattr(block, "text", None)
        )

        try:
            parsed = self.parse_json_response(first_text)
        except ValueError:
            retry = await self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": first_text},
                    {"role": "user", "content": FIX_JSON_MESSAGE},
                ],
            )
            retry_text = "".join(
                block.text for block in retry.content if getattr(block, "text", None)
            )
            try:
                parsed = self.parse_json_response(retry_text)
            except ValueError as exc:
                raise HTTPException(status_code=500, detail=SERVER_ERROR_MESSAGE) from exc

        parsed["provider_used"] = f"anthropic/{self.model}"
        normalized = self.normalize_analysis_payload(parsed)
        return AnalysisResult.model_validate(normalized)
