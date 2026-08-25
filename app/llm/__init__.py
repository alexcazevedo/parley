"""LLM abstraction: provider-agnostic types plus OpenAI-compatible transport."""

from app.llm.base import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCall,
    ToolSpec,
)
from app.llm.factory import get_provider, resolve_model

__all__ = [
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_TOOL",
    "ROLE_USER",
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "ToolCall",
    "ToolSpec",
    "get_provider",
    "resolve_model",
]
