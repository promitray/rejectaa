"""HuggingFace-backed LLM analysis provider."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from models import AnalysisResult
from services.llm.base import AbstractAnalysisProvider, MODE_A

HF_API_URL_TEMPLATE = "https://api-inference.huggingface.co/models/{model_name}"
HF_AUTH_HEADER = "Authorization"
HF_BEARER_PREFIX = "Bearer "
MAX_NEW_TOKENS = 2000


class HuggingFaceProvider(AbstractAnalysisProvider):
    """Analysis provider using HuggingFace hosted or local models."""

    def __init__(
        self,
        model_name: str,
        api_token: str = "",
        use_local: bool = False,
    ) -> None:
        """Store model config and initialize lazy local pipeline holder."""
        self.model_name = model_name
        self.api_token = api_token
        self.use_local = use_local
        self._local_pipeline: Any = None

    async def analyse(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Route analysis to hosted HF API or local model pipeline."""
        if self.use_local:
            return await self._analyse_local(
                abstract=abstract,
                sections=sections,
                target_journal=target_journal,
                citation_summary=citation_summary,
                journal_data=journal_data,
                mode=mode,
            )
        return await self._analyse_api(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
            mode=mode,
        )

    async def _analyse_api(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Use HuggingFace Inference API."""
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )
        full_prompt = f"{system_prompt}\n\n{user_message}"
        url = HF_API_URL_TEMPLATE.format(model_name=self.model_name)
        headers = {}
        if self.api_token:
            headers[HF_AUTH_HEADER] = f"{HF_BEARER_PREFIX}{self.api_token}"
        payload = {"inputs": full_prompt, "parameters": {"max_new_tokens": MAX_NEW_TOKENS}}

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        raw_generated: str
        if isinstance(data, list) and data and isinstance(data[0], dict):
            raw_generated = str(data[0].get("generated_text", ""))
        elif isinstance(data, dict) and "generated_text" in data:
            raw_generated = str(data["generated_text"])
        else:
            raw_generated = str(data)

        parsed = self.parse_json_response(raw_generated)
        parsed["provider_used"] = f"huggingface/{self.model_name}"
        normalized = self.normalize_analysis_payload(parsed)
        return AnalysisResult.model_validate(normalized)

    async def _analyse_local(
        self,
        abstract: str,
        sections: dict[str, Any],
        target_journal: str,
        citation_summary: str,
        journal_data: dict[str, Any],
        mode: str = MODE_A,
    ) -> AnalysisResult:
        """Use local HuggingFace model via transformers pipeline."""
        if self._local_pipeline is None:
            from transformers import pipeline

            self._local_pipeline = pipeline(
                "text-generation",
                model=self.model_name,
                device_map="auto",
                max_new_tokens=MAX_NEW_TOKENS,
            )

        prompt = (
            f"{self.get_system_prompt(mode)}\n\n"
            f"{self.build_user_message(abstract, sections, target_journal, citation_summary, journal_data)}"
        )
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._run_local, prompt)
        parsed = self.parse_json_response(result)
        parsed["provider_used"] = f"huggingface-local/{self.model_name}"
        normalized = self.normalize_analysis_payload(parsed)
        return AnalysisResult.model_validate(normalized)

    def _run_local(self, prompt: str) -> str:
        """Run synchronous local inference in a worker thread."""
        output = self._local_pipeline(prompt)
        return str(output[0]["generated_text"])
