"""Picks the provider for an agent.

`openai_compatible` (the default) is the hand-written provider on
`LLM_BASE_URL`; `openai`, `anthropic` and `google` go through Pydantic AI
(`app.llm.providers.native`, imported only for those, so an
`openai_compatible` agent never loads it). This is the single place that knows
how `Settings` credentials turn into a live provider.
"""

from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.models.agent import Agent

PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
# Mirrors `app.llm.providers.native.NATIVE_PROVIDERS`, kept here so the
# dispatch needs no import of that module for the default path.
_NATIVE = ("openai", "anthropic", "google")


def resolve_model(agent: Agent, settings: Settings | None = None) -> str:
    """`agent.model` wins; `LLM_MODEL` is the fallback when the agent left it blank."""
    settings = settings or get_settings()
    return agent.model or settings.llm_model


def get_provider(agent: Agent, settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    provider = agent.provider or PROVIDER_OPENAI_COMPATIBLE
    if provider in _NATIVE:
        from app.llm.providers.native import NativeProvider, has_credentials, key_setting

        if not has_credentials(provider, settings):
            raise ValueError(f"LLM provider {provider!r} needs {key_setting(provider)}")
        return NativeProvider(provider, settings)
    if provider != PROVIDER_OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported LLM provider {provider!r}")
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        max_tokens_param=settings.llm_max_tokens_param,
    )
