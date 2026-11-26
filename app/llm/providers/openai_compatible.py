"""OpenAI-compatible chat-completions provider.

One implementation covers OpenAI, OpenRouter, Ollama, vLLM and friends: they
all speak `POST {base_url}/chat/completions` with the same tools payload. The
only knobs are `base_url` and `api_key` (PRODUCT.md).
"""

import base64
import json
import time
from typing import Any

import httpx
import structlog

from app.http_client import shared_client
from app.http_retry import send_with_retry
from app.llm.base import (
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

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0


def _content_to_wire(
    content: str | list[ContentPart] | None,
) -> str | list[dict[str, Any]] | None:
    """Text content passes through; multimodal content becomes OpenAI parts.

    Images travel inline as `data:` URLs — no upload step, no public URL to
    host, and the bytes are already in memory from the media storage.
    """
    if not isinstance(content, list):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, ImagePart):
            encoded = base64.b64encode(part.data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.mime};base64,{encoded}"},
                }
            )
        elif isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
    return parts


def _message_to_wire(message: LLMMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role,
        "content": _content_to_wire(message.content),
    }
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _tool_to_wire(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters or {"type": "object", "properties": {}},
        },
    }


def _parse_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw in raw_calls or []:
        function = raw.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            # Models do emit malformed JSON. Don't kill the turn: hand the tool
            # an empty argument set and let it report the failure.
            logger.warning("llm.bad_tool_arguments", raw=str(raw_arguments)[:500])
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    return calls


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        max_tokens_param: str = "max_tokens",
    ) -> None:
        self._max_tokens_param = max_tokens_param
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            return shared_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_to_wire(message) for message in messages],
            "temperature": temperature,
            self._max_tokens_param: max_tokens,
        }
        if tools:
            payload["tools"] = [_tool_to_wire(spec) for spec in tools]
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        try:
            response = await send_with_retry(
                lambda: self._get_client().post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=self._timeout,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"{exc.response.status_code} from {self.base_url}: {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"transport error talking to {self.base_url}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected completion body: {str(body)[:500]}") from exc

        usage_body = body.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            usage=LLMUsage(
                prompt_tokens=int(usage_body.get("prompt_tokens") or 0),
                completion_tokens=int(usage_body.get("completion_tokens") or 0),
            ),
            latency_ms=latency_ms,
        )
