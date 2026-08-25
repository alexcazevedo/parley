"""Application settings, loaded from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://harness:harness@postgres:5432/harness"
    redis_url: str = "redis://redis:6379/0"
    admin_api_key: str = "changeme-admin-key"
    secrets_key: str = "changeme-fernet-key-base64=="
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = "changeme-llm-key"
    llm_model: str = "gpt-4o-mini"
    debounce_seconds: int = 3
    log_level: str = "INFO"
    rate_limit_tenant_per_min: int = 600
    rate_limit_user_per_min: int = 30
    max_message_chars: int = 4000
    # SSRF guard for outbound customer-tool HTTP calls (see app/tools/ssrf.py).
    # Comma-separated hostname allowlist; when non-empty only these hosts may
    # be called, bypassing the private/loopback IP checks — this is how
    # docker-compose points tools at the in-network `mock-tools` demo service.
    tool_allowed_hosts: str = ""
    # Disables the SSRF guard entirely. Development only.
    tool_ssrf_disabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
