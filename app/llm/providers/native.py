"""Native providers: OpenAI, Anthropic and Google through Pydantic AI.

One `complete` is one model request (`pydantic_ai.direct.model_request`): the
model never runs a tool, parley's loop does (`app.agents.runtime`). Parley's
messages are converted on every call and the response converted back, so
nothing vendor-specific is kept: a conversation written under one provider
continues under another.

Retries belong to the vendor's client: `max_retries` (`HTTP_RETRY_ATTEMPTS - 1`)
goes in the model settings, which OpenAI's, Anthropic's and Google's clients
all honour per request; parley adds none on top.

`build_native_model` is the one place a vendor model object is created; tests
replace it (`tests/native_fakes.py`), so no test reaches a vendor.
"""

from __future__ import annotations

import time
from typing import Any

import pydantic_ai
import structlog
from pydantic_ai import messages as pai
from pydantic_ai.direct import model_request
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from app.config import Settings, get_settings
from app.llm.base import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ContentPart,
    ImagePart,
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    TextPart,
    ToolCall,
    ToolSpec,
)

# A server owns its output: no first-run banner from Pydantic AI.
pydantic_ai.BANNER_ENABLED = False

logger = structlog.get_logger(__name__)

NATIVE_PROVIDERS = ("openai", "anthropic", "google")

# The same per-call limit as the OpenAI-compatible provider; the turn's
# deadline bounds the call as well.
DEFAULT_TIMEOUT_SECONDS = 30.0

_KEY_SETTINGS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def key_setting(provider: str) -> str:
    """The setting that holds `provider`'s key, e.g. `ANTHROPIC_API_KEY`."""
    try:
        return _KEY_SETTINGS[provider]
    except KeyError:
        raise ValueError(f"unsupported LLM provider {provider!r}") from None


def has_credentials(provider: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(getattr(settings, key_setting(provider).lower(), ""))


def _api_key(provider: str, settings: Settings) -> str:
    return str(getattr(settings, key_setting(provider).lower(), ""))


def build_native_model(provider: str, model: str, settings: Settings | None = None) -> Model:
    """The vendor model object for `provider`. Tests replace this function."""
    settings = settings or get_settings()
    api_key = _api_key(provider, settings)
    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(model, provider=OpenAIProvider(api_key=api_key))
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))
    if provider == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model, provider=GoogleProvider(api_key=api_key))
    raise ValueError(f"unsupported LLM provider {provider!r}")


# --- parley -> Pydantic AI ------------------------------------------------------


def _user_content(content: str | list[ContentPart] | None) -> str | list[pai.UserContent]:
    if not isinstance(content, list):
        return content or ""
    parts: list[pai.UserContent] = []
    for part in content:
        if isinstance(part, ImagePart):
            parts.append(pai.BinaryContent(data=part.data, media_type=part.mime))
        elif isinstance(part, TextPart):
            parts.append(part.text)
    return parts


def to_model_messages(messages: list[LLMMessage]) -> list[pai.ModelMessage]:
    """Parley's transcript as Pydantic AI messages.

    Consecutive non-assistant messages share one `ModelRequest`, consecutive
    assistant messages one `ModelResponse` (vendors want the turns to
    alternate, and every result of one step in a single message). A tool
    result carries its call's tool name, looked up by call id.
    """
    tool_names: dict[str, str] = {}
    out: list[pai.ModelMessage] = []
    request_parts: list[pai.ModelRequestPart] = []
    response_parts: list[pai.ModelResponsePart] = []

    def flush_request() -> None:
        if request_parts:
            out.append(pai.ModelRequest(parts=list(request_parts)))
            request_parts.clear()

    def flush_response() -> None:
        if response_parts:
            out.append(pai.ModelResponse(parts=list(response_parts)))
            response_parts.clear()

    for message in messages:
        if message.role == ROLE_ASSISTANT:
            flush_request()
            text = message.content if isinstance(message.content, str) else None
            if isinstance(message.content, list):
                text = " ".join(p.text for p in message.content if isinstance(p, TextPart))
            if text:
                response_parts.append(pai.TextPart(content=text))
            for call in message.tool_calls:
                tool_names[call.id] = call.name
                response_parts.append(
                    pai.ToolCallPart(
                        tool_name=call.name, args=dict(call.arguments), tool_call_id=call.id
                    )
                )
            continue
        flush_response()
        if message.role == ROLE_SYSTEM:
            text = message.content if isinstance(message.content, str) else None
            if isinstance(message.content, list):
                text = " ".join(p.text for p in message.content if isinstance(p, TextPart))
            request_parts.append(pai.SystemPromptPart(content=text or ""))
        elif message.role == ROLE_TOOL:
            call_id = message.tool_call_id or ""
            content = message.content if isinstance(message.content, str) else ""
            request_parts.append(
                pai.ToolReturnPart(
                    tool_name=tool_names.get(call_id, ""),
                    content=content,
                    tool_call_id=call_id,
                )
            )
        else:
            request_parts.append(pai.UserPromptPart(content=_user_content(message.content)))
    flush_request()
    flush_response()
    return out


def _tool_definition(spec: ToolSpec) -> ToolDefinition:
    return ToolDefinition(
        name=spec.name,
        description=spec.description,
        parameters_json_schema=spec.parameters or {"type": "object", "properties": {}},
    )


# --- Pydantic AI -> parley ------------------------------------------------------


def _arguments(part: pai.ToolCallPart) -> dict[str, Any]:
    try:
        return part.args_as_dict(raise_if_invalid=True)
    except (ValueError, AssertionError):
        # Models do emit malformed JSON. Don't kill the turn: hand the tool an
        # empty argument set and let it report the failure.
        logger.warning("llm.bad_tool_arguments", raw=str(part.args)[:500])
        return {}


def from_model_response(response: pai.ModelResponse, latency_ms: int) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in response.parts:
        if isinstance(part, pai.TextPart):
            texts.append(part.content)
        elif isinstance(part, pai.ToolCallPart):
            calls.append(
                ToolCall(id=part.tool_call_id, name=part.tool_name, arguments=_arguments(part))
            )
    text = "".join(texts)
    return LLMResponse(
        content=text or None,
        tool_calls=calls,
        usage=LLMUsage(
            prompt_tokens=int(response.usage.input_tokens or 0),
            completion_tokens=int(response.usage.output_tokens or 0),
        ),
        latency_ms=latency_ms,
    )


class NativeProvider:
    """`LLMProvider` for `openai`, `anthropic` and `google`."""

    def __init__(self, provider: str, settings: Settings | None = None) -> None:
        key_setting(provider)  # refuses an unknown provider
        self.provider = provider
        self._settings = settings

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        settings = self._settings or get_settings()
        model_settings = ModelSettings(
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_retries=max(0, settings.http_retry_attempts - 1),
        )
        parameters = ModelRequestParameters(
            function_tools=[_tool_definition(spec) for spec in tools or []]
        )
        started = time.perf_counter()
        try:
            vendor_model = build_native_model(self.provider, model, settings)
            response = await model_request(
                vendor_model,
                to_model_messages(messages),
                model_settings=model_settings,
                model_request_parameters=parameters,
            )
        except Exception as exc:
            raise LLMError(f"{self.provider} model {model!r} failed: {exc}"[:1000]) from exc
        return from_model_response(response, int((time.perf_counter() - started) * 1000))
