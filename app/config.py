"""Application settings, loaded from environment / .env."""

from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped default for ADMIN_API_KEY; only accepted when DEMO_MODE is true.
PLACEHOLDER_ADMIN_API_KEY = "changeme-admin-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://harness:harness@postgres:5432/harness"
    redis_url: str = "redis://redis:6379/0"
    # Demo mode mounts the demo surface (`/chat` page, `/chat/bootstrap`,
    # `/dev/*`) and is the only mode that boots on placeholder secrets.
    demo_mode: bool = False
    admin_api_key: str = PLACEHOLDER_ADMIN_API_KEY
    secrets_key: str = "changeme-fernet-key-base64=="
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = "changeme-llm-key"
    llm_model: str = "gpt-4o-mini"
    # Name of the token-limit field in the chat-completions payload; some
    # models want `max_completion_tokens`. One per deployment, not per agent.
    llm_max_tokens_param: str = Field(default="max_tokens", min_length=1)
    # Short retry on 429/5xx for model calls, speech-to-text and GET tools
    # (see app/http_retry.py). Attempts count the first try; the wait doubles
    # from the base, a numeric Retry-After replaces it, both capped by the max.
    http_retry_attempts: int = Field(default=3, ge=1)
    http_retry_base_seconds: float = Field(default=0.5, ge=0)
    http_retry_max_wait_seconds: float = Field(default=10.0, ge=0)
    debounce_seconds: int = 3
    # Turns one worker runs at once (see app/workers/scheduler.py). Each
    # running turn holds one database connection for its whole length.
    worker_concurrency: int = Field(default=4, ge=1)
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
    # Cap on bytes read from a customer-tool HTTP response.
    tool_max_response_bytes: int = 32768
    # Fixed plaintext key for the seeded webchat demo tenant.
    # Dev/demo shortcut only — never rotated, never per-tenant.
    webchat_demo_api_key: str = "demo-webchat-key"
    # Humanizer pacing knobs (see app/conversations/humanize.py).
    humanize_chars_per_sec: float = 35.0
    humanize_min_delay_seconds: float = 0.8
    humanize_max_delay_seconds: float = 4.0
    humanize_max_parts: int = 4
    # Sentiment escalation classifier (see app/agents/sentiment.py).
    sentiment_threshold: float = 0.7
    sentiment_timeout_seconds: float = 5.0
    # Category classifier (see app/agents/categorize.py): past this the
    # model's answer is dropped and the conversation keeps its category.
    category_timeout_seconds: float = Field(default=5.0, gt=0)
    # How many long-term memories the agent gets in its context each turn.
    memory_context_limit: int = Field(default=20, ge=1, le=100)
    # Media storage (see app/media/storage.py): local filesystem
    # root for uploaded images/audio, and the max accepted file size.
    # Text the model reads in place of an image or voice note it cannot see or
    # hear (see app/agents/context.py); never sent to a customer.
    image_placeholder: str = "[imagem enviada pelo cliente]"
    audio_placeholder: str = "[áudio enviado pelo cliente]"
    media_root: str = "./media"
    media_max_bytes: int = 16 * 1024 * 1024
    # Vision (see app/agents/context.py): turn this off for a
    # deployment whose model has no image support — images then reach the
    # model as a text placeholder instead. `vision_max_images` caps how many
    # of the newest images in the context window carry actual bytes; older
    # ones degrade to text, because every image costs tokens.
    llm_vision_enabled: bool = True
    vision_max_images: int = 3
    # Speech-to-text (see app/media/stt.py): defaults to the LLM
    # provider's own base URL/key (one OpenAI key covers chat + Whisper);
    # override either to point transcription at a different backend.
    stt_base_url: str | None = None
    stt_api_key: str | None = None
    stt_model: str = "whisper-1"
    stt_language: str = "pt"
    stt_timeout_seconds: float = 20.0
    # Reply check (see app/agents/boundaries.py `check_reply`): past this the
    # check counts as failed and the reply is blocked.
    boundary_check_timeout_seconds: float = 8.0
    # Operator boundary floor: path to a JSON file applied to every agent of
    # every tenant (see app/agents/boundaries.py `load_floor`). Empty = none.
    boundary_floor_file: str = ""
    # Channel endpoint SSRF guard (see app/tools/ssrf.py "channel endpoints"):
    # comma-separated hostnames an Evolution channel `base_url` may use even
    # though they are private. Additive: every other host must be public.
    # Separate from (and unaffected by) the TOOL_* settings above.
    channel_allowed_hosts: str = ""


class InsecureSettingsError(RuntimeError):
    """Raised at boot when non-demo settings still carry placeholder secrets."""


def assert_secure_settings(settings: Settings) -> None:
    """Refuse placeholder/invalid secrets unless demo mode is on.

    Error messages name the env var but never include the value.
    """
    if settings.demo_mode:
        return
    if not settings.admin_api_key or settings.admin_api_key == PLACEHOLDER_ADMIN_API_KEY:
        raise InsecureSettingsError(
            "ADMIN_API_KEY is empty or the shipped placeholder; set a real value "
            "(placeholders are only accepted with DEMO_MODE=true)"
        )
    try:
        Fernet(settings.secrets_key.encode())
    except Exception:
        raise InsecureSettingsError(
            "SECRETS_KEY is not a valid Fernet key; generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` '
            "(placeholders are only accepted with DEMO_MODE=true)"
        ) from None


@lru_cache
def get_settings() -> Settings:
    return Settings()
