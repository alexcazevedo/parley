"""Provider-agnostic LLM types.

Everything above this module (context builder, agent runtime, tools) speaks
these dataclasses; only `app/llm/providers/*` knows about wire formats. The
shapes deliberately mirror the OpenAI chat-completions vocabulary because that
is the only wire protocol we support (see PRODUCT.md), but nothing outside the
providers package serializes them.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# LLM-level roles. These are *not* `app.models.message.MessageRole` — the
# context builder maps persisted roles onto these.
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass(slots=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMMessage:
    """One entry of the prompt transcript sent to the model."""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on `role="tool"` messages: which call this is the result of.
    tool_call_id: str | None = None


@dataclass(slots=True)
class ToolSpec:
    """A tool as advertised to the model. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage | None = None
    latency_ms: int | None = None


class LLMError(RuntimeError):
    """Any failure talking to the provider (transport, HTTP status, bad body)."""


@runtime_checkable
class LLMProvider(Protocol):
    """The single seam between the agent runtime and any model backend."""

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> LLMResponse: ...
