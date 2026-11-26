"""PRODUCT.md's native-providers success demo as a test: one tenant, keys for two
vendors (fakes behind them, never a vendor), two agents on different providers."""


import httpx
import respx
from pydantic_ai import messages as pai
from sqlalchemy import select

from app.agents.boundaries import DEFAULT_REFUSAL_MESSAGE
from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.llm.providers.native import key_setting
from app.models.agent import DEFAULT_HANDOFF_MESSAGE
from app.models.agent_run import AgentRun, AgentRunStatus
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from tests import native_fakes
from tests.test_agent_providers import _tenant, keys  # noqa: F401
from tests.test_http_tools import ORDER_SCHEMA, ORDERS_URL, fake_dns  # noqa: F401
from tests.test_model_jobs import BOUNDED, _turn, job_env  # noqa: F401
from tests.test_native_providers import _messages, _of, _runs, _send
from tests.test_native_providers import _turn as _drain_and_tick
from tests.test_typed_chat_jobs import OUTPUT_TOOL

native_model = native_fakes.native_model
NEVER_ITEM = BOUNDED["boundary_never"][0]
CHECK_MODEL = "claude-small"


async def _agent(client, headers, provider, name, **extra) -> tuple[str, str]:
    """An agent on `provider` wired to `get_order`, with its own webchat channel."""
    agent = await client.post(
        "/agents",
        json={
            "name": name,
            "instructions": "Você é o atendente da Acme.",
            "provider": provider,
            "model": f"{provider}-model",
            "tool_names": ["get_order"],
            **extra,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": name, "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return agent.json()["id"], channel.json()["id"]


async def _stack(client, keys, *providers):  # noqa: F811
    """A tenant with the `get_order` tool and keys for `providers`."""
    keys(*providers)
    headers = await _tenant(client)
    tool = await client.post(
        "/tools",
        json={
            "name": "get_order",
            "description": "Look up an order by id",
            "input_schema": ORDER_SCHEMA,
            "endpoint": ORDERS_URL,
            "method": "GET",
            "timeout_seconds": 5,
        },
        headers=headers,
    )
    assert tool.status_code == 201, tool.text
    return headers


def _runtime() -> AgentRuntime:
    return AgentRuntime(registry=ToolRegistry([BuiltinExecutor(), HttpToolExecutor()]))


async def _turn_with_orders(fake_redis, db_session) -> respx.Route:
    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json={"status": "shipped"})
        )
        await _drain_and_tick(fake_redis, db_session, _runtime())
    return route


async def test_two_agents_two_providers_tool_and_handoff(
    client, fake_redis, db_session, keys, native_model, fake_handoff_brief  # noqa: F811
):
    headers = await _stack(client, keys, "anthropic", "google")
    channels = {}
    for provider in ("anthropic", "google"):
        _agent_id, channels[provider] = await _agent(client, headers, provider, provider)

    # One scripted model serves both agents, so each turn is run on its own.
    # 1. each answers a customer; each run records its provider and model
    conversations = {}
    for provider, channel_id in channels.items():
        conversations[provider] = await _send(client, headers, channel_id, "oi")
        native_model.reply(f"Olá, aqui é o {provider}!")
        await _drain_and_tick(fake_redis, db_session, _runtime())
        [run] = await _runs(db_session, conversations[provider])
        assert (run.provider, run.model) == (provider, f"{provider}-model")
        assert run.status == AgentRunStatus.SUCCESS
        assert (await _messages(db_session, conversations[provider]))[-1].content == (
            f"Olá, aqui é o {provider}!"
        )
    assert {(r.provider, r.model) for r in native_model.requests} == {
        ("anthropic", "anthropic-model"),
        ("google", "google-model"),
    }

    # 2. on both: a turn with a tool call, then a turn that hands off
    for provider, channel_id in channels.items():
        conversation_id = conversations[provider]
        await _send(client, headers, channel_id, "cadê meu pedido 12345")
        native_model.call("get_order", {"order_id": "12345"}, call_id=f"call_{provider}")
        native_model.reply("Já foi enviado.")
        route = await _turn_with_orders(fake_redis, db_session)
        assert route.call_count == 1
        run = (await _runs(db_session, conversation_id))[-1]
        assert run.error is None, run.error
        assert (run.provider, run.llm_calls, run.tool_calls) == (provider, 2, 1)
        assert (await _messages(db_session, conversation_id))[-1].content == "Já foi enviado."

        await _send(client, headers, channel_id, "quero falar com uma pessoa")
        native_model.call("handoff_to_human", {"reason": "pediu"}, call_id=f"h_{provider}")
        await _drain_and_tick(fake_redis, db_session, _runtime())
        run = (await _runs(db_session, conversation_id))[-1]
        assert run.error is None, run.error
        assert (run.provider, run.status, run.tool_calls) == (provider, AgentRunStatus.SUCCESS, 1)
        assert (await _messages(db_session, conversation_id))[-1].content == (
            DEFAULT_HANDOFF_MESSAGE
        )


async def test_conversation_continues_after_switching_provider(
    client, fake_redis, db_session, keys, native_model  # noqa: F811
):
    headers = await _stack(client, keys, "anthropic", "google")
    agent_id, channel_id = await _agent(client, headers, "anthropic", "Bot")
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido 12345")
    native_model.call("get_order", {"order_id": "12345"}, call_id="toolu_1")
    native_model.reply("Já foi enviado.")
    await _turn_with_orders(fake_redis, db_session)

    switched = await client.patch(
        f"/agents/{agent_id}", json={"provider": "google", "model": "google-model"},
        headers=headers,
    )
    assert switched.status_code == 200, switched.text
    await _send(client, headers, channel_id, "e quando chega?")
    native_model.reply("Amanhã.")
    await _drain_and_tick(fake_redis, db_session, _runtime())

    last = native_model.requests[-1]
    assert (last.provider, last.model) == ("google", "google-model")
    [call] = _of(last, pai.ToolCallPart)
    [result] = _of(last, pai.ToolReturnPart)
    assert call.tool_call_id == result.tool_call_id == "toolu_1"
    assert "shipped" in result.content
    assert [p.content for p in _of(last, pai.UserPromptPart)] == [
        "cadê meu pedido 12345",
        "e quando chega?",
    ]
    assert (await _messages(db_session, conversation_id))[-1].content == "Amanhã."
    first, second = await _runs(db_session, conversation_id)
    assert (first.provider, second.provider) == ("anthropic", "google")


async def test_agent_on_a_provider_without_a_key_is_refused(client, keys):  # noqa: F811
    headers = await _stack(client, keys, "anthropic", "google")  # no OPENAI_API_KEY
    refused = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "provider": "openai", "model": "m"},
        headers=headers,
    )
    assert refused.status_code == 422
    assert key_setting("openai") == "OPENAI_API_KEY"
    assert "OPENAI_API_KEY is not set" in refused.text


async def test_boundary_check_on_a_chat_model_blocks_with_the_rules_text(
    client, fake_redis, db_session, job_env, native_model  # noqa: F811
):
    job_env(BOUNDARY_CHECK_MODEL=f"anthropic:{CHECK_MODEL}", ANTHROPIC_API_KEY="test-key")
    reply = "Tome dipirona de 8 em 8 horas."
    native_model.call(OUTPUT_TOOL, {"allowed": False, "broken": "never_1"})
    provider = FakeLLMProvider([LLMResponse(content=reply)])

    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    [run] = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.conversation_id == conversation_id)
        )
    ).scalars().all()
    assert run.boundary_reason == NEVER_ITEM
    assert run.blocked_text == reply and run.output_text == DEFAULT_REFUSAL_MESSAGE
    [request] = native_model.requests
    assert (request.provider, request.model) == ("anthropic", CHECK_MODEL)


async def test_openai_compatible_agent_never_reaches_the_new_module(
    client, fake_redis, db_session, keys, native_model, respx_mock  # noqa: F811
):
    from app.config import get_settings

    headers = await _stack(client, keys, "anthropic", "google")
    _agent_id, channel_id = await _agent(client, headers, "openai_compatible", "Old")
    conversation_id = await _send(client, headers, channel_id, "oi")
    respx_mock.post(f"{get_settings().llm_base_url.rstrip('/')}/chat/completions").respond(
        json={"choices": [{"message": {"content": "Oi pelo caminho antigo!"}}], "usage": {}}
    )

    await _drain_and_tick(fake_redis, db_session, _runtime())

    assert native_model.requests == []
    [run] = await _runs(db_session, conversation_id)
    assert run.provider == "openai_compatible"
    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == "Oi pelo caminho antigo!"
