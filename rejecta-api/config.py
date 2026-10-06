"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLM_PROVIDER_ANTHROPIC = "anthropic"
LLM_PROVIDER_OPENAI = "openai"
LLM_PROVIDER_HUGGINGFACE = "huggingface"
VALID_LLM_PROVIDERS = {
    LLM_PROVIDER_ANTHROPIC,
    LLM_PROVIDER_OPENAI,
    LLM_PROVIDER_HUGGINGFACE,
}


LOCAL_APP_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
PRODUCTION_APP_ORIGINS = (
    "https://app.rejecta.ai",
    "https://rejecta.scivalon.com",
    "https://www.rejecta.scivalon.com",
    "https://rejectaa.netlify.app",
)


class Settings(BaseSettings):
    """Runtime settings for the API service."""

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    HF_MODEL_NAME: str = "mistralai/Mistral-7B-Instruct-v0.3"
    HF_API_TOKEN: str = ""
    HF_USE_LOCAL: bool = False
    LLM_PROVIDER: str = LLM_PROVIDER_ANTHROPIC
    FRONTEND_URL: str = "http://localhost:5173"
    APP_URL: str = "http://localhost:5173"
    MARKETING_URL: str = "https://rejecta.ai"
    CORS_ORIGINS: str = ""
    ENVIRONMENT: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("LLM_PROVIDER")
    @classmethod
    def validate_llm_provider(cls, value: str) -> str:
        """Ensure configured provider is one of the supported providers."""
        normalized = value.strip().lower()
        if normalized not in VALID_LLM_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(VALID_LLM_PROVIDERS)}"
            )
        return normalized

    def allowed_origins(self) -> list[str]:
        """Browser origins allowed to call this API.

        The app (localhost in dev, app.rejecta.ai in production) is the
        only frontend that should call the API. The marketing site does not.
        """
        configured = [
            self.FRONTEND_URL,
            self.APP_URL,
            *self.CORS_ORIGINS.split(","),
            *LOCAL_APP_ORIGINS,
            *PRODUCTION_APP_ORIGINS,
        ]
        origins: list[str] = []
        for item in configured:
            origin = item.strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
        return origins


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance for the app lifecycle."""
    return Settings()


settings = get_settings()
