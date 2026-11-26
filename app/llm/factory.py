"""Picks the provider for an agent.

Only `openai_compatible` exists (PRODUCT.md: no native Anthropic/Gemini), so
this is a one-branch dispatch — its job is to be the single place that knows
how `Settings` credentials turn into a live provider.
"""

from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.models.agent import Agent

PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"


def resolve_model(agent: Agent, settings: Settings | None = None) -> str:
    """`agent.model` wins; `LLM_MODEL` is the fallback when the agent left it blank."""
    settings = settings or get_settings()
    return agent.model or settings.llm_model


def get_provider(agent: Agent, settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    provider = agent.provider or PROVIDER_OPENAI_COMPATIBLE
    if provider != PROVIDER_OPENAI_COMPATIBLE:
        raise ValueError(f"unsupported LLM provider {provider!r}")
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        max_tokens_param=settings.llm_max_tokens_param,
    )
