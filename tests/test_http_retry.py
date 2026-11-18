"""Short retry on 429/5xx for model calls, speech-to-text and GET tools, and
the operator-set name of the token-limit parameter.
"""

import json
import uuid

import httpx
import pytest
import respx

from app.channels.base import OutgoingMessage
from app.channels.evolution import EvolutionAdapter
from app.config import Settings, get_settings
from app.llm.base import LLMError, LLMMessage
from app.llm.factory import get_provider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.media.stt import STTError, transcribe
from app.models.agent import Agent
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact
from app.models.tool import Tool
from app.security import secrets as secrets_crypto
from app.tools.http import HttpToolExecutor

LLM_URL = "https://llm.example.com/v1"
EVO_URL = "https://evolution.example.com"
TOOL_URL = "https://tools.example.com/orders"
OK_BODY = {"choices": [{"message": {"content": "hi"}}], "usage": {}}


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record the waits instead of sleeping, and start from default settings."""
    import app.http_retry as http_retry

    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    monkeypatch.delenv("HTTP_RETRY_BASE_SECONDS", raising=False)
    get_settings.cache_clear()
    yield waited
    get_settings.cache_clear()


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(base_url=LLM_URL, api_key="k")


async def _complete(provider: OpenAICompatibleProvider):
    return await provider.complete([LLMMessage(role="user", content="x")], model="m")


def _tool(method: str) -> Tool:
    return Tool(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="t",
        input_schema={},
        endpoint=TOOL_URL,
        method=method,
        timeout_seconds=5,
    )


def test_retry_wait_grows_honours_retry_after_and_is_capped():
    from app.http_retry import RETRY_STATUSES, retry_wait_seconds

    settings = Settings(http_retry_base_seconds=0.5, http_retry_max_wait_seconds=10)
    assert RETRY_STATUSES == frozenset({429, 500, 502, 503, 504})
    assert retry_wait_seconds(1, None, settings) == 0.5
    assert retry_wait_seconds(2, None, settings) == 1.0
    assert retry_wait_seconds(3, None, settings) == 2.0
    assert retry_wait_seconds(1, "2", settings) == 2.0
    assert retry_wait_seconds(1, "999", settings) == 10.0
    assert retry_wait_seconds(20, None, settings) == 10.0
    # A date-form Retry-After is ignored.
    assert retry_wait_seconds(2, "Wed, 21 Oct 2026 07:28:00 GMT", settings) == 1.0


def test_retry_settings_defaults_and_bounds():
    settings = Settings()
    assert settings.http_retry_attempts == 3
    assert settings.http_retry_base_seconds == 0.5
    assert settings.http_retry_max_wait_seconds == 10.0
    assert settings.llm_max_tokens_param == "max_tokens"
    with pytest.raises(ValueError):
        Settings(http_retry_attempts=0)
    with pytest.raises(ValueError):
        Settings(llm_max_tokens_param="")


@pytest.mark.asyncio
@respx.mock
async def test_model_call_429_then_200_succeeds_with_retry_after_as_wait(sleeps):
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
            httpx.Response(200, json=OK_BODY),
        ]
    )
    response = await _complete(_provider())
    assert response.content == "hi"
    assert route.call_count == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
@respx.mock
async def test_model_call_503_every_time_raises_after_exactly_the_attempts(sleeps):
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(503, text="down")
    )
    with pytest.raises(LLMError, match="503"):
        await _complete(_provider())
    assert route.call_count == get_settings().http_retry_attempts == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
@respx.mock
async def test_model_call_400_is_not_retried():
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(400, text="bad")
    )
    with pytest.raises(LLMError):
        await _complete(_provider())
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_model_call_transport_error_is_not_retried():
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(LLMError):
        await _complete(_provider())
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_transcription_500_then_200_succeeds():
    route = respx.post(f"{LLM_URL}/audio/transcriptions").mock(
        side_effect=[httpx.Response(500, text="boom"), httpx.Response(200, json={"text": " ola "})]
    )
    settings = Settings(llm_base_url=LLM_URL, stt_base_url=None)
    assert await transcribe(b"abc", "audio/ogg", settings=settings) == "ola"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_transcription_500_every_time_raises_stt_error():
    route = respx.post(f"{LLM_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(500, text="boom")
    )
    settings = Settings(llm_base_url=LLM_URL, stt_base_url=None)
    with pytest.raises(STTError, match="500"):
        await transcribe(b"abc", "audio/ogg", settings=settings)
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_get_tool_503_then_200_returns_the_result(sleeps):
    route = respx.get(TOOL_URL).mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"status": "shipped"}),
        ]
    )
    result = await HttpToolExecutor()._call(_tool("GET"), {"order_id": "1"})
    assert result.ok
    assert result.data == {"status": "shipped"}
    assert route.call_count == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
@respx.mock
async def test_get_tool_503_every_time_returns_the_last_error():
    route = respx.get(TOOL_URL).mock(return_value=httpx.Response(503, text="busy"))
    result = await HttpToolExecutor()._call(_tool("GET"), {})
    assert not result.ok
    assert "HTTP 503" in (result.error or "")
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_get_tool_retry_checks_the_endpoint_before_every_try(monkeypatch):
    import app.tools.http as tools_http

    checked: list[str] = []

    async def counting_assert(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(tools_http, "assert_safe_url", counting_assert)
    respx.get(TOOL_URL).mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
    )
    await HttpToolExecutor()._call(_tool("GET"), {})
    assert checked == [TOOL_URL, TOOL_URL]


@pytest.mark.asyncio
@respx.mock
async def test_post_tool_503_is_requested_exactly_once():
    route = respx.post(TOOL_URL).mock(return_value=httpx.Response(503, text="busy"))
    result = await HttpToolExecutor()._call(_tool("POST"), {"a": 1})
    assert not result.ok
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_evolution_send_503_is_requested_exactly_once():
    route = respx.post(f"{EVO_URL}/message/sendText/inst").mock(
        return_value=httpx.Response(503, json={"error": "busy"})
    )
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": EVO_URL, "instance": "inst"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    with pytest.raises(Exception):  # noqa: B017 - today's handling, whatever it raises
        await EvolutionAdapter().send_message(
            OutgoingMessage(conversation_id=uuid.uuid4(), content="oi"), contact, channel
        )
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_default_token_limit_name_is_max_tokens():
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_BODY)
    )
    await _provider().complete(
        [LLMMessage(role="user", content="x")], model="m", max_tokens=77
    )
    payload = json.loads(route.calls.last.request.content)
    assert payload["max_tokens"] == 77


@pytest.mark.asyncio
@respx.mock
async def test_token_limit_name_comes_from_the_setting():
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(200, json=OK_BODY)
    )
    settings = Settings(llm_base_url=LLM_URL, llm_max_tokens_param="max_completion_tokens")
    provider = get_provider(Agent(model="m"), settings)
    await provider.complete([LLMMessage(role="user", content="x")], model="m", max_tokens=77)
    payload = json.loads(route.calls.last.request.content)
    assert payload["max_completion_tokens"] == 77
    assert "max_tokens" not in payload
