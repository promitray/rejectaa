"""OpenAI-backed LLM analysis provider."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from models import AnalysisResult
from services.llm.base import AbstractAnalysisProvider, MODE_A

OPENAI_DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 2000
JSON_OBJECT_TYPE = "json_object"
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
REPAIR_PROMPT = (
    "Your previous output failed schema validation. Return ONLY valid JSON that matches "
    "the required schema exactly. Do not include markdown or extra text."
)


class OpenAIProvider(AbstractAnalysisProvider):
    """Analysis provider using OpenAI GPT models."""

    def __init__(self, api_key: str, model: str = OPENAI_DEFAULT_MODEL) -> None:
        """Create an async OpenAI client and bind model name."""
        self.client = AsyncOpenAI(api_key=api_key)
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
        """Run analysis through OpenAI and return validated result."""
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            response_format={"type": JSON_OBJECT_TYPE},
            messages=[
                {"role": ROLE_SYSTEM, "content": system_prompt},
                {"role": ROLE_USER, "content": user_message},
            ],
        )

        content = response.choices[0].message.content or "{}"
        parsed = self.parse_json_response(content)
        parsed["provider_used"] = f"openai/{self.model}"

        try:
            normalized = self.normalize_analysis_payload(parsed)
            return AnalysisResult.model_validate(normalized)
        except Exception:
            retry = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                response_format={"type": JSON_OBJECT_TYPE},
                messages=[
                    {"role": ROLE_SYSTEM, "content": system_prompt},
                    {"role": ROLE_USER, "content": user_message},
                    {"role": ROLE_ASSISTANT, "content": content},
                    {"role": ROLE_USER, "content": REPAIR_PROMPT},
                ],
            )
            retry_content = retry.choices[0].message.content or "{}"
            retry_parsed = self.parse_json_response(retry_content)
            retry_parsed["provider_used"] = f"openai/{self.model}"
            normalized_retry = self.normalize_analysis_payload(retry_parsed)
            return AnalysisResult.model_validate(normalized_retry)
