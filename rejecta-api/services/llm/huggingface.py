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
MAX_NEW_TOKENS = 2500


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

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """General generation fallback for HuggingFace."""
        combined_prompt = f"<system>\n{system_prompt}\n</system>\n<user>\n{user_prompt}\n</user>\n<assistant>\n"
        if self.use_local:
            return await asyncio.to_thread(self._run_local, combined_prompt)
        headers = {HF_AUTH_HEADER: f"{HF_BEARER_PREFIX}{self.api_token}"}
        payload = {
            "inputs": combined_prompt,
            "parameters": {
                "max_new_tokens": MAX_NEW_TOKENS,
                "temperature": max(0.01, temperature),
                "return_full_text": False,
            },
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                HF_API_URL_TEMPLATE.format(model_name=self.model_name),
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) > 0 and "generated_text" in data[0]:
                return str(data[0]["generated_text"])
            return str(data)

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
        system_prompt = self.get_system_prompt(mode)
        user_message = self.build_user_message(
            abstract=abstract,
            sections=sections,
            target_journal=target_journal,
            citation_summary=citation_summary,
            journal_data=journal_data,
        )
        if self.use_local:
            return await self._analyse_local(system_prompt, user_message)
        return await self._analyse_api(system_prompt, user_message)

    async def _analyse_api(
        self,
        system_prompt: str,
        user_message: str,
    ) -> AnalysisResult:
        """Use HuggingFace Inference API."""
        prompt = (
            f"<system>\n{system_prompt}\n</system>\n"
            f"<user>\n{user_message}\n</user>\n"
            f"<assistant>\n"
        )
        headers = {HF_AUTH_HEADER: f"{HF_BEARER_PREFIX}{self.api_token}"}
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": MAX_NEW_TOKENS,
                "temperature": 0.01,
                "return_full_text": False,
            },
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                HF_API_URL_TEMPLATE.format(model_name=self.model_name),
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if isinstance(data, list) and len(data) > 0 and "generated_text" in data[0]:
            raw_text = str(data[0]["generated_text"])
        elif isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"HuggingFace API error: {data['error']}")
        else:
            raw_text = str(data)

        parsed = self.parse_json_response(raw_text)
        parsed["provider_used"] = f"huggingface/{self.model_name}"
        normalized = self.normalize_analysis_payload(parsed)
        return AnalysisResult.model_validate(normalized)

    async def _analyse_local(
        self,
        system_prompt: str,
        user_message: str,
    ) -> AnalysisResult:
        """Use local HuggingFace model via transformers pipeline."""
        if self._local_pipeline is None:
            from transformers import pipeline

            self._local_pipeline = pipeline(
                "text-generation",
                model=self.model_name,
                max_new_tokens=MAX_NEW_TOKENS,
            )

        prompt = (
            f"<system>\n{system_prompt}\n</system>\n"
            f"<user>\n{user_message}\n</user>\n"
            f"<assistant>\n"
        )
        raw_text = await asyncio.to_thread(self._run_local, prompt)
        parsed = self.parse_json_response(raw_text)
        parsed["provider_used"] = f"huggingface-local/{self.model_name}"
        normalized = self.normalize_analysis_payload(parsed)
        return AnalysisResult.model_validate(normalized)

    def _run_local(self, prompt: str) -> str:
        """Run synchronous local inference in a worker thread."""
        output = self._local_pipeline(prompt)
        return str(output[0]["generated_text"])
