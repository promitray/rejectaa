"""The API must refuse to start in production with unsafe settings."""

from config import Settings

SAFE = dict(
    ENVIRONMENT="production",
    DATABASE_URL="postgresql://rejecta_api:x@host:5432/postgres",
    JWT_PRIVATE_KEY="k",
    JWT_KEY_ID="k1",
    MFA_ENCRYPTION_KEY="m",
    AUTH_COOKIE_SECURE=True,
    DOCS_ENABLED=False,
    DB_SSL_MODE="require",
    APP_URL="https://rejecta.scivalon.com",
)


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **{**SAFE, **overrides})


def test_safe_production_config_has_no_problems():
    assert make().production_problems() == []


def test_each_unsafe_setting_is_reported():
    cases = {
        "DATABASE_URL": "",
        "JWT_PRIVATE_KEY": "",
        "MFA_ENCRYPTION_KEY": "",
        "AUTH_COOKIE_SECURE": False,
        "DOCS_ENABLED": True,
        "DB_SSL_MODE": "disable",
        "APP_URL": "http://localhost:5173",
    }
    for key, value in cases.items():
        problems = make(**{key: value}).production_problems()
        assert len(problems) == 1, (key, problems)


def test_development_is_not_checked():
    assert make(ENVIRONMENT="development", DOCS_ENABLED=True, AUTH_COOKIE_SECURE=False).production_problems() == []
