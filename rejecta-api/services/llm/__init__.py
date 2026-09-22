"""LLM provider factory and cache helpers."""

from functools import lru_cache

from config import (
    LLM_PROVIDER_ANTHROPIC,
    LLM_PROVIDER_HUGGINGFACE,
    LLM_PROVIDER_OPENAI,
    settings,
)
from services.llm.anthropic import AnthropicProvider
from services.llm.base import AbstractAnalysisProvider
from services.llm.huggingface import HuggingFaceProvider
from services.llm.openai import OpenAIProvider

ANTHROPIC_API_KEY_MISSING = "ANTHROPIC_API_KEY not set"
OPENAI_API_KEY_MISSING = "OPENAI_API_KEY not set"
UNKNOWN_PROVIDER_MESSAGE = "Unknown LLM_PROVIDER: {provider}"


def get_provider() -> AbstractAnalysisProvider:
    """Return provider selected by environment configuration."""
    if settings.LLM_PROVIDER == LLM_PROVIDER_ANTHROPIC:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError(ANTHROPIC_API_KEY_MISSING)
        return AnthropicProvider(api_key=settings.ANTHROPIC_API_KEY)

    if settings.LLM_PROVIDER == LLM_PROVIDER_OPENAI:
        if not settings.OPENAI_API_KEY:
            raise ValueError(OPENAI_API_KEY_MISSING)
        return OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODEL,
        )

    if settings.LLM_PROVIDER == LLM_PROVIDER_HUGGINGFACE:
        return HuggingFaceProvider(
            model_name=settings.HF_MODEL_NAME,
            api_token=settings.HF_API_TOKEN,
            use_local=settings.HF_USE_LOCAL,
        )

    raise ValueError(UNKNOWN_PROVIDER_MESSAGE.format(provider=settings.LLM_PROVIDER))


@lru_cache
def get_provider_cached() -> AbstractAnalysisProvider:
    """Return a cached provider instance for process lifetime."""
    return get_provider()
