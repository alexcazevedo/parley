"""Agents on `openai`, `anthropic` and `google`: replies through Pydantic AI.

The same end-to-end pipeline as `tests/test_agent_loop.py` (ingest, drain,
tick), with the agent's provider set to a native one and the vendor model
replaced by `tests/native_fakes.py` (a `FunctionModel`): what is asserted is
what parley would have sent to the vendor, and parley's loop does the rest.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic_ai import messages as pai
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.llm.base import (
    ImagePart,
    LLMError,
    LLMMessage,
    TextPart,
    ToolCall,
    ToolSpec,
)
from app.llm.factory import get_provider
from app.llm.providers import native as native_module
from app.llm.providers.native import (
    NATIVE_PROVIDERS,
    NativeProvider,
    build_native_model,
    has_credentials,
    key_setting,
)
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.models.agent import DEFAULT_HANDOFF_MESSAGE, Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.base import ToolResult
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests import native_fakes

DEBOUNCE_SECONDS = 0.2
SORRY = "Desculpe, tive um problema. Tente de novo."
FIXTURES = Path(__file__).parent / "fixtures"

providers = pytest.mark.parametrize("provider", NATIVE_PROVIDERS)
native_model = native_fakes.native_model  # the fixture, in this module's scope


@pytest.fixture
def native_keys(monkeypatch, tmp_path):
    """A key for every native provider (never sent anywhere: the model is fake)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class StubExecutor:
    def __init__(self, name: str = "get_order") -> None:
        self.name = name
        self.calls: list[dict] = []

    def can_handle(self, name: str) -> bool:
        return name == self.name

    async def specs(self, agent, session) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=self.name,
                description="Look up an order by id",
                parameters={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            )
        ]

    async def execute(self, name: str, arguments: dict, ctx) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult.success({"status": "shipped"})


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, provider: str, **overrides) -> tuple[dict, str, str]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "provider": provider,
            "model": f"{provider}-model",
            **overrides,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"], agent.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _turn(fake_redis, db_session, runtime) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _messages(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _runs(db_session, conversation_id) -> list[AgentRun]:
    result = await db_session.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.created_at)
    )
    return list(result.scalars().all())


def _of(request, kind) -> list:
    return [part for part in request.parts() if isinstance(part, kind)]


# --- through the whole turn -------------------------------------------------------


@providers
async def test_plain_reply(client, fake_redis, db_session, native_keys, native_model, provider):
    headers, channel_id, _ = await _setup(client, provider, temperature=0.2, max_tokens=321)
    conversation_id = await _send(client, headers, channel_id, "oi")
    native_model.reply("Olá! Como posso ajudar?", input_tokens=12, output_tokens=7)

    await _turn(fake_redis, db_session, AgentRuntime())

    assert len(native_model.requests) == 1
    request = native_model.requests[0]
    assert (request.provider, request.model) == (provider, f"{provider}-model")
    system = _of(request, pai.SystemPromptPart)
    assert system[0].content.startswith("Você é o atendente da Acme.")
    assert [p.content for p in _of(request, pai.UserPromptPart)] == ["oi"]
    # The token limit and temperature reach the vendor's settings.
    assert request.settings["max_tokens"] == 321
    assert request.settings["temperature"] == 0.2

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Olá! Como posso ajudar?"
    [run] = await _runs(db_session, conversation_id)
    assert run.status == AgentRunStatus.SUCCESS
    assert (run.llm_calls, run.prompt_tokens, run.completion_tokens) == (1, 12, 7)


@providers
async def test_tool_call_result_reaches_next_request(
    client, fake_redis, db_session, native_keys, native_model, provider
):
    headers, channel_id, _ = await _setup(client, provider)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido 12345")
    native_model.call("get_order", {"order_id": "12345"}, call_id="call_9", input_tokens=10)
    native_model.reply("Seu pedido já foi enviado.", input_tokens=20, output_tokens=6)
    stub = StubExecutor()

    await _turn(fake_redis, db_session, AgentRuntime(registry=ToolRegistry([stub])))

    assert stub.calls == [{"order_id": "12345"}]
    first, second = native_model.requests
    assert [tool.name for tool in first.tools] == ["get_order"]
    assert first.tools[0].parameters_json_schema["required"] == ["order_id"]
    [call] = _of(second, pai.ToolCallPart)
    [result] = _of(second, pai.ToolReturnPart)
    assert (call.tool_name, call.tool_call_id, call.args) == (
        "get_order",
        "call_9",
        {"order_id": "12345"},
    )
    assert (result.tool_name, result.tool_call_id) == ("get_order", "call_9")
    assert "shipped" in result.content

    messages = await _messages(db_session, conversation_id)
    assert [(m.role, m.type) for m in messages] == [
        (MessageRole.USER, "text"),
        (MessageRole.ASSISTANT, "tool_call"),
        (MessageRole.TOOL, "tool_result"),
        (MessageRole.ASSISTANT, "text"),
    ]
    assert messages[-1].content == "Seu pedido já foi enviado."
    [run] = await _runs(db_session, conversation_id)
    assert (run.llm_calls, run.tool_calls) == (2, 1)
    assert (run.prompt_tokens, run.completion_tokens) == (30, 11)


@providers
async def test_terminal_tool_ends_the_turn(
    client, fake_redis, db_session, native_keys, native_model, provider, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client, provider)
    conversation_id = await _send(client, headers, channel_id, "quero falar com uma pessoa")
    native_model.call("handoff_to_human", {"reason": "pediu"}, call_id="h1")

    await _turn(
        fake_redis, db_session, AgentRuntime(registry=ToolRegistry([BuiltinExecutor()]))
    )

    assert len(native_model.requests) == 1
    assert "handoff_to_human" in [tool.name for tool in native_model.requests[0].tools]
    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == DEFAULT_HANDOFF_MESSAGE
    [run] = await _runs(db_session, conversation_id)
    assert run.status == AgentRunStatus.SUCCESS
    assert (run.llm_calls, run.tool_calls) == (1, 1)


@providers
async def test_image_reaches_the_request(
    client, fake_redis, db_session, native_keys, native_model, provider
):
    headers, channel_id, _ = await _setup(client, provider)
    png = (FIXTURES / "order_label.png").read_bytes()
    upload = await client.post(
        "/channels/webchat/media",
        data={"channel_id": str(channel_id), "user_id": "user-1", "caption": "o que diz?"},
        files={"file": ("order_label.png", png, "image/png")},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    native_model.reply("Diz 12345.")

    await _turn(fake_redis, db_session, AgentRuntime())

    [request] = native_model.requests
    [user] = _of(request, pai.UserPromptPart)
    image, caption = user.content
    assert isinstance(image, pai.BinaryContent)
    assert image.media_type == "image/png" and image.data == png
    assert caption == "o que diz?"


@providers
async def test_vision_off_sends_text_only(
    client, fake_redis, db_session, native_keys, native_model, provider, monkeypatch
):
    monkeypatch.setenv("LLM_VISION_ENABLED", "false")
    get_settings.cache_clear()
    headers, channel_id, _ = await _setup(client, provider)
    png = (FIXTURES / "order_label.png").read_bytes()
    upload = await client.post(
        "/channels/webchat/media",
        data={"channel_id": str(channel_id), "user_id": "user-1"},
        files={"file": ("order_label.png", png, "image/png")},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    native_model.reply("Recebi.")

    await _turn(fake_redis, db_session, AgentRuntime())

    [user] = _of(native_model.requests[0], pai.UserPromptPart)
    assert isinstance(user.content, str)


@providers
async def test_error_sends_error_message(
    client, fake_redis, db_session, native_keys, native_model, provider
):
    headers, channel_id, _ = await _setup(client, provider, error_message=SORRY)
    conversation_id = await _send(client, headers, channel_id, "oi")
    native_model.fail(RuntimeError("503 overloaded"))

    await _turn(fake_redis, db_session, AgentRuntime())

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == SORRY
    [run] = await _runs(db_session, conversation_id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error.startswith("LLMError") and "503 overloaded" in run.error


@providers
async def test_delay_past_deadline_sends_error_message(
    client, fake_redis, db_session, native_keys, native_model, provider
):
    headers, channel_id, _ = await _setup(client, provider, error_message=SORRY)
    conversation_id = await _send(client, headers, channel_id, "oi")
    native_model.delay(5.0).reply("tarde demais")

    started = time.perf_counter()
    await _turn(fake_redis, db_session, AgentRuntime(timeout_seconds=0.3))
    assert time.perf_counter() - started < 4

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == SORRY
    [run] = await _runs(db_session, conversation_id)
    assert run.status == AgentRunStatus.ERROR


@pytest.mark.parametrize(
    ("first", "then"), [("anthropic", "google"), ("google", "openai"), ("openai", "anthropic")]
)
async def test_history_continues_on_another_provider(
    client, fake_redis, db_session, native_keys, native_model, first, then
):
    headers, channel_id, agent_id = await _setup(client, first)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido 12345")
    native_model.call("get_order", {"order_id": "12345"}, call_id="toolu_1")
    native_model.reply("Já foi enviado.")
    runtime = AgentRuntime(registry=ToolRegistry([StubExecutor()]))
    await _turn(fake_redis, db_session, runtime)

    switched = await client.patch(
        f"/agents/{agent_id}", json={"provider": then, "model": f"{then}-model"}, headers=headers
    )
    assert switched.status_code == 200, switched.text
    await _send(client, headers, channel_id, "e quando chega?")
    native_model.reply("Amanhã.")
    await _turn(fake_redis, db_session, runtime)

    last = native_model.requests[-1]
    assert (last.provider, last.model) == (then, f"{then}-model")
    kinds = [type(message).__name__ for message in last.messages]
    request, response = "ModelRequest", "ModelResponse"
    assert kinds == [request, response, request, response, request]
    [call] = _of(last, pai.ToolCallPart)
    [result] = _of(last, pai.ToolReturnPart)
    assert call.tool_call_id == result.tool_call_id == "toolu_1"
    assert result.tool_name == "get_order"
    assert [p.content for p in _of(last, pai.UserPromptPart)] == [
        "cadê meu pedido 12345",
        "e quando chega?",
    ]
    assert [p.content for p in _of(last, pai.TextPart)] == ["Já foi enviado."]
    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == "Amanhã."


# --- the provider on its own -------------------------------------------------------


@providers
async def test_max_retries_from_http_retry_attempts(
    native_keys, native_model, provider, monkeypatch
):
    monkeypatch.setenv("HTTP_RETRY_ATTEMPTS", "5")
    get_settings.cache_clear()
    native_model.reply("ok")
    await NativeProvider(provider).complete([LLMMessage(role="user", content="oi")], model="m")
    settings = native_model.requests[0].settings
    assert settings["max_retries"] == 4
    assert settings["timeout"] == native_module.DEFAULT_TIMEOUT_SECONDS


@providers
async def test_single_attempt_means_no_vendor_retry(native_keys, native_model, provider):
    native_model.reply("ok")
    settings = get_settings().model_copy(update={"http_retry_attempts": 1})
    await NativeProvider(provider, settings).complete(
        [LLMMessage(role="user", content="oi")], model="m"
    )
    assert native_model.requests[0].settings["max_retries"] == 0


@providers
async def test_empty_response_and_bad_arguments(native_keys, native_model, provider):
    native_model.empty()
    native_model.call("get_order", '{"order_id": ', call_id="c1")
    llm = NativeProvider(provider)
    empty = await llm.complete([LLMMessage(role="user", content="oi")], model="m")
    assert empty.content is None and empty.tool_calls == []
    bad = await llm.complete([LLMMessage(role="user", content="oi")], model="m")
    assert bad.tool_calls == [ToolCall(id="c1", name="get_order", arguments={})]


@providers
async def test_failure_is_llm_error(native_keys, native_model, provider):
    native_model.fail(ValueError("bad request"))
    with pytest.raises(LLMError, match="bad request"):
        await NativeProvider(provider).complete(
            [LLMMessage(role="user", content="oi")], model="m"
        )


async def test_multimodal_conversion():
    messages = native_module.to_model_messages(
        [
            LLMMessage(role="system", content="sys"),
            LLMMessage(
                role="user", content=[ImagePart(mime="image/jpeg", data=b"x"), TextPart("t")]
            ),
            LLMMessage(
                role="assistant",
                content="vou ver",
                tool_calls=[ToolCall(id="a", name="f", arguments={}), ToolCall(id="b", name="g")],
            ),
            LLMMessage(role="tool", content="ra", tool_call_id="a"),
            LLMMessage(role="tool", content="rb", tool_call_id="b"),
        ]
    )
    assert [type(m).__name__ for m in messages] == ["ModelRequest", "ModelResponse", "ModelRequest"]
    results = messages[2].parts
    assert [(r.tool_name, r.tool_call_id, r.content) for r in results] == [
        ("f", "a", "ra"),
        ("g", "b", "rb"),
    ]


# --- credentials and dispatch ------------------------------------------------------


def test_key_settings_and_credentials():
    assert [key_setting(p) for p in NATIVE_PROVIDERS] == [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
    ]
    empty = get_settings().model_copy(
        update={"openai_api_key": "", "anthropic_api_key": "k", "google_api_key": ""}
    )
    assert not has_credentials("openai", empty)
    assert has_credentials("anthropic", empty)
    with pytest.raises(ValueError):
        key_setting("mistral")


@providers
def test_get_provider_native_needs_a_key(provider):
    settings = get_settings().model_copy(
        update={"openai_api_key": "", "anthropic_api_key": "", "google_api_key": ""}
    )
    with pytest.raises(ValueError, match=key_setting(provider)):
        get_provider(Agent(provider=provider), settings)
    keyed = settings.model_copy(update={key_setting(provider).lower(): "k"})
    built = get_provider(Agent(provider=provider), keyed)
    assert isinstance(built, NativeProvider) and built.provider == provider


def test_get_provider_unknown_still_raises():
    with pytest.raises(ValueError, match="unsupported LLM provider"):
        get_provider(Agent(provider="mistral"))


async def test_openai_compatible_never_touches_native(monkeypatch, respx_mock):
    def refuse(*args, **kwargs):
        raise AssertionError("native module used for openai_compatible")

    monkeypatch.setattr(native_module, "build_native_model", refuse)
    monkeypatch.setattr(native_module.NativeProvider, "__init__", refuse)
    for value in (None, "openai_compatible"):
        provider = get_provider(Agent(provider=value))
        assert isinstance(provider, OpenAICompatibleProvider)
    settings = get_settings()
    respx_mock.post(f"{settings.llm_base_url.rstrip('/')}/chat/completions").respond(
        json={"choices": [{"message": {"content": "oi"}}], "usage": {}}
    )
    response = await get_provider(Agent(provider="openai_compatible")).complete(
        [LLMMessage(role="user", content="oi")], model="gpt-4o-mini"
    )
    assert response.content == "oi"


@providers
def test_build_native_model_makes_the_vendor_model(provider):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.models.openai import OpenAIChatModel

    settings = get_settings().model_copy(update={key_setting(provider).lower(): "k"})
    model = build_native_model(provider, "some-model", settings)
    expected = {"openai": OpenAIChatModel, "anthropic": AnthropicModel, "google": GoogleModel}
    assert isinstance(model, expected[provider])
    assert model.model_name == "some-model"
