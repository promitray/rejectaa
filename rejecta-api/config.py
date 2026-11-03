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
    # Extra origins by pattern: Netlify deploy previews of this site only.
    CORS_ORIGIN_REGEX: str = r"https://([a-z0-9-]+--)?rejectaa\.netlify\.app"
    ENVIRONMENT: str = "development"
    # Swagger UI and /openapi.json. Set false in production.
    DOCS_ENABLED: bool = True

    # Database (Supabase Postgres, schema "app").
    # DATABASE_URL: runtime connection as the least-privilege role rejecta_api.
    # MIGRATION_DATABASE_URL: owner connection (postgres) for Alembic and
    # admin scripts only. Never used by the running API.
    DATABASE_URL: str = ""
    MIGRATION_DATABASE_URL: str = ""
    DB_SSL_MODE: str = "require"
    DB_POOL_MIN_SIZE: int = 1
    DB_POOL_MAX_SIZE: int = 10
    DB_COMMAND_TIMEOUT_SECONDS: float = 15.0

    # Auth: own JWT (EdDSA / Ed25519). Generate with: python -m scripts.generate_keys
    # JWT_PRIVATE_KEY: base64url raw 32-byte Ed25519 seed (or a PEM).
    # JWT_PREVIOUS_PUBLIC_KEYS: "kid:base64url_public,..." still accepted for
    # verification during key rotation (never used for signing).
    JWT_PRIVATE_KEY: str = ""
    JWT_KEY_ID: str = ""
    JWT_PREVIOUS_PUBLIC_KEYS: str = ""
    JWT_ISSUER: str = "https://rejecta-production.up.railway.app"
    JWT_AUDIENCE: str = "rejecta-api"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30
    SESSION_MAX_LIFETIME_DAYS: int = 90
    # Refresh-token cookie. The browser reaches auth routes through the
    # Netlify proxy at /api/auth/*, so that is the cookie path in production.
    AUTH_COOKIE_NAME: str = "rejecta_rt"
    AUTH_COOKIE_PATH: str = "/api/auth"
    AUTH_COOKIE_SECURE: bool = True
    # Optional: secret of Netlify's signed proxy (X-Nf-Sign). When set and the
    # signature is valid, the client IP from Netlify is trusted for rate limits.
    NETLIFY_PROXY_SECRET: str = ""
    LOGIN_MAX_FAILURES: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15
    AUTH_USER_CACHE_SECONDS: float = 30.0

    # Admin two-factor auth (TOTP). MFA_ENCRYPTION_KEY is a Fernet key that
    # encrypts TOTP secrets at rest and keys the recovery-code HMAC.
    # Generate with: python -m scripts.generate_keys --write-env
    # Never rotate it casually: existing admin authenticators would stop working.
    MFA_ENCRYPTION_KEY: str = ""
    MFA_ISSUER_NAME: str = "Rejecta"
    MFA_TOKEN_TTL_MINUTES: int = 5

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

    @field_validator("DB_SSL_MODE")
    @classmethod
    def validate_db_ssl_mode(cls, value: str) -> str:
        """Only TLS-required (Supabase) or disabled (local test DB) are allowed."""
        normalized = value.strip().lower()
        if normalized not in {"require", "disable"}:
            raise ValueError("DB_SSL_MODE must be 'require' or 'disable'")
        return normalized

    @field_validator("DB_POOL_MIN_SIZE", "DB_POOL_MAX_SIZE")
    @classmethod
    def validate_pool_size(cls, value: int) -> int:
        """Keep pool sizes within what a Supabase project can serve."""
        if not 0 <= value <= 50:
            raise ValueError("DB pool sizes must be between 0 and 50")
        return value

    @field_validator("ACCESS_TOKEN_TTL_MINUTES")
    @classmethod
    def validate_access_ttl(cls, value: int) -> int:
        """Access tokens stay short-lived: 5 to 60 minutes."""
        if not 5 <= value <= 60:
            raise ValueError("ACCESS_TOKEN_TTL_MINUTES must be between 5 and 60")
        return value

    @field_validator("REFRESH_TOKEN_TTL_DAYS", "SESSION_MAX_LIFETIME_DAYS")
    @classmethod
    def validate_session_days(cls, value: int) -> int:
        """Refresh tokens and sessions: 1 to 365 days."""
        if not 1 <= value <= 365:
            raise ValueError("Session lifetimes must be between 1 and 365 days")
        return value

    @field_validator("AUTH_COOKIE_PATH")
    @classmethod
    def validate_cookie_path(cls, value: str) -> str:
        """Cookie path must be absolute."""
        if not value.startswith("/"):
            raise ValueError("AUTH_COOKIE_PATH must start with '/'")
        return value

    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() == "production"

    def production_problems(self) -> list[str]:
        """Settings that are unsafe in production. The API refuses to start with any."""
        if not self.is_production():
            return []
        problems = []
        if not self.DATABASE_URL.strip():
            problems.append("DATABASE_URL is not set")
        if not self.JWT_PRIVATE_KEY.strip() or not self.JWT_KEY_ID.strip():
            problems.append("JWT_PRIVATE_KEY / JWT_KEY_ID are not set")
        if not self.MFA_ENCRYPTION_KEY.strip():
            problems.append("MFA_ENCRYPTION_KEY is not set")
        if not self.AUTH_COOKIE_SECURE:
            problems.append("AUTH_COOKIE_SECURE must be true")
        if self.DOCS_ENABLED:
            problems.append("DOCS_ENABLED must be false")
        if self.DB_SSL_MODE != "require":
            problems.append("DB_SSL_MODE must be 'require'")
        if not self.APP_URL.startswith("https://"):
            problems.append("APP_URL must be the https app URL (used in password links)")
        return problems

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
