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
class TextPart:
    """A text chunk of a multimodal message."""

    text: str


@dataclass(slots=True)
class ImagePart:
    """An image chunk of a multimodal message.

    `data` is the raw file content: keeping it as bytes up here means nothing
    above the providers package deals with base64 or data URLs (the wire
    encoding is `_message_to_wire`'s business).
    """

    mime: str
    data: bytes


ContentPart = TextPart | ImagePart


def text_of(content: "str | list[ContentPart] | None") -> str:
    """The readable text of any message content — for logging and measuring.

    Image parts contribute nothing; a multimodal message reads as its text
    parts joined by a space.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if isinstance(part, TextPart))


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
    # A plain `str` for text-only messages; a list of parts when the message
    # carries images (see `ContentPart`). Providers serialize both.
    content: str | list[ContentPart] | None = None
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
