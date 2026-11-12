"""OpenAI-backed LLM analysis provider."""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI

from models import AnalysisResult
from services.llm.base import COMPACT_RETRY_NOTE, AbstractAnalysisProvider, MODE_A

OPENAI_DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 4000  # the general-purpose tools (Calibrator, Forge, ...)
ANALYSIS_MAX_TOKENS = 8000  # the full report; at 4000 long reports were cut off mid-JSON
ANALYSIS_SEED = 20261110  # with temperature 0, makes repeated runs of the same input far more alike
JSON_OBJECT_TYPE = "json_object"
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
REPAIR_PROMPT = (
    "Your previous output failed schema validation. Return ONLY valid JSON that matches "
    "the required schema exactly. Do not include markdown or extra text."
)

logger = logging.getLogger(__name__)


class OpenAIProvider(AbstractAnalysisProvider):
    """Analysis provider using OpenAI GPT models."""

    def __init__(self, api_key: str, model: str = OPENAI_DEFAULT_MODEL) -> None:
        """Create an async OpenAI client and bind model name."""
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """General-purpose structured JSON generation."""
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=temperature,
            seed=ANALYSIS_SEED,  # the consistency and evidence checks use this too: same input, same answer
            response_format={"type": JSON_OBJECT_TYPE},
            messages=[
                {"role": ROLE_SYSTEM, "content": system_prompt},
                {"role": ROLE_USER, "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or "{}"

    async def _complete(self, messages: list[dict[str, str]]) -> tuple[str, bool, str]:
        """(content, whether it was cut off at the token limit, the exact model version that answered)."""
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=ANALYSIS_MAX_TOKENS,
            temperature=0,
            seed=ANALYSIS_SEED,
            response_format={"type": JSON_OBJECT_TYPE},
            messages=messages,
        )
        choice = response.choices[0]
        return choice.message.content or "{}", choice.finish_reason == "length", response.model or self.model

    def _result(self, content: str, model_version: str) -> AnalysisResult:
        parsed = self.parse_json_response(content)
        parsed["provider_used"] = f"openai/{model_version}"
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
        """Run analysis through OpenAI and return validated result.

        A report cut off at the token limit is asked for again, shorter (repairing a cut-off JSON only
        produces a worse cut-off JSON). An answer that fails validation is sent back once for repair.
        """
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )
        messages = [{"role": ROLE_SYSTEM, "content": system_prompt}, {"role": ROLE_USER, "content": user_message}]
        content, cut_off, version = await self._complete(messages)
        if cut_off:
            logger.warning("Analysis output hit the token limit; asking for a shorter report")
            messages = [
                {"role": ROLE_SYSTEM, "content": system_prompt},
                {"role": ROLE_USER, "content": f"{user_message}\n\n{COMPACT_RETRY_NOTE}"},
            ]
            content, _, version = await self._complete(messages)

        try:
            return self._result(content, version)
        except Exception:
            logger.warning("Analysis output failed validation; asking the model to repair it", exc_info=True)
            retry_content, _, version = await self._complete(
                [*messages, {"role": ROLE_ASSISTANT, "content": content}, {"role": ROLE_USER, "content": REPAIR_PROMPT}]
            )
            return self._result(retry_content, version)
