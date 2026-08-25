"""Customer HTTP tools end-to-end: the real pipeline (ingest -> debounce ->
scheduler tick -> AgentRuntime -> HttpToolExecutor), with the LLM scripted and
the customer's endpoint mocked by respx. Nothing leaves the process.
"""

import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.message import Message, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
ORDERS_URL = "https://example.test/orders"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """`example.test` resolves to a public address; everything else fails.

    respx intercepts the transport, so this is only feeding the SSRF guard.
    """

    def getaddrinfo(host, port, *args, **kwargs):
        if host != "example.test":
            raise socket.gaierror(f"no fake DNS entry for {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


async def _setup(client, *, tool: dict | None = None) -> tuple[dict, str]:
    """Tenant + agent (wired to `get_order`) + tool row + webchat channel."""
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}

    payload = {
        "name": "get_order",
        "description": "Look up an order by id",
        "input_schema": ORDER_SCHEMA,
        "endpoint": ORDERS_URL,
        "method": "GET",
        "auth_config": {"type": "bearer", "token": "s3cr3t-token"},
        "timeout_seconds": 5,
    }
    payload.update(tool or {})
    created = await client.post("/tools", json=payload, headers=headers)
    assert created.status_code == 201, created.text

    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            "tool_names": [payload["name"]],
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


def _provider(tool_name: str = "get_order", arguments: dict | None = None) -> FakeLLMProvider:
    return FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name=tool_name,
                        arguments={"order_id": "12345"} if arguments is None else arguments,
                    )
                ]
            ),
            LLMResponse(content="Seu pedido 12345 já foi enviado."),
        ]
    )


async def _run(client, fake_redis, db_session, text="cadê meu pedido 12345", **kwargs):
    headers, channel_id = await _setup(client, **kwargs)
    conversation_id = await _send(client, headers, channel_id, text)
    await _drain_jobs(fake_redis)
    return conversation_id


async def _turn(fake_redis, db_session, provider) -> None:
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _tool_result_message(db_session, conversation_id: uuid.UUID) -> Message:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == MessageRole.TOOL)
        .order_by(Message.created_at)
    )
    messages = list(result.scalars().all())
    assert messages, "the tool result never reached the transcript"
    return messages[-1]


async def _executions(db_session, conversation_id: uuid.UUID) -> list[ToolExecution]:
    """Scoped to this conversation: the API layer commits, so rows written by
    earlier tests are still in the database.
    """
    runs = select(AgentRun.id).where(AgentRun.conversation_id == conversation_id)
    result = await db_session.execute(
        select(ToolExecution)
        .where(ToolExecution.agent_run_id.in_(runs))
        .order_by(ToolExecution.created_at)
    )
    return list(result.scalars().all())


async def test_get_tool_is_called_with_query_args_and_bearer_auth(
    client, fake_redis, db_session
):
    conversation_id = await _run(client, fake_redis, db_session)
    provider = _provider()

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json={"status": "shipped", "eta": "amanhã"})
        )
        await _turn(fake_redis, db_session, provider)

    assert route.called
    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url) == f"{ORDERS_URL}?order_id=12345"
    assert request.headers["authorization"] == "Bearer s3cr3t-token"
    assert not request.content  # GET carries no body

    tool_result = await _tool_result_message(db_session, conversation_id)
    assert '"ok": true' in tool_result.content.lower()
    assert "shipped" in tool_result.content

    executions = await _executions(db_session, conversation_id)
    assert len(executions) == 1
    execution = executions[0]
    assert execution.status == ToolExecutionStatus.OK
    assert execution.tool_name == "get_order"
    assert execution.tool_id is not None
    assert execution.arguments == {"order_id": "12345"}
    assert execution.result == {"status": "shipped", "eta": "amanhã"}
    assert execution.error is None
    assert execution.latency_ms is not None

    # The turn still ends with the model's answer.
    assert provider.call_count == 2


async def test_post_tool_sends_json_body_and_custom_header_auth(
    client, fake_redis, db_session
):
    await _run(
        client,
        fake_redis,
        db_session,
        tool={
            "method": "POST",
            "auth_config": {"type": "header", "header_name": "X-Api-Token", "token": "abc123"},
        },
    )

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(ORDERS_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
        await _turn(fake_redis, db_session, _provider())

    request = route.calls.last.request
    assert request.headers["x-api-token"] == "abc123"
    assert "authorization" not in request.headers
    assert json.loads(request.read()) == {"order_id": "12345"}
    assert not request.url.query


async def test_http_500_is_recorded_as_error_and_the_loop_continues(
    client, fake_redis, db_session
):
    conversation_id = await _run(client, fake_redis, db_session)
    provider = _provider()

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(500, text="boom"))
        await _turn(fake_redis, db_session, provider)

    tool_result = await _tool_result_message(db_session, conversation_id)
    assert '"ok": false' in tool_result.content.lower()
    assert "HTTP 500" in tool_result.content

    execution = (await _executions(db_session, conversation_id))[0]
    assert execution.status == ToolExecutionStatus.ERROR
    assert "HTTP 500" in execution.error
    assert execution.result is None
    # The model got the error payload and still produced an answer.
    assert provider.call_count == 2


async def test_timeout_is_recorded_as_error(client, fake_redis, db_session):
    conversation_id = await _run(client, fake_redis, db_session)

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(side_effect=httpx.ConnectTimeout("too slow"))
        await _turn(fake_redis, db_session, _provider())

    tool_result = await _tool_result_message(db_session, conversation_id)
    assert "timed out" in tool_result.content

    execution = (await _executions(db_session, conversation_id))[0]
    assert execution.status == ToolExecutionStatus.ERROR
    assert "timed out after 5s" in execution.error


async def test_invalid_arguments_are_rejected_before_any_request(
    client, fake_redis, db_session
):
    conversation_id = await _run(client, fake_redis, db_session)

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _turn(fake_redis, db_session, _provider(arguments={"orderId": 12345}))

    assert not route.called
    tool_result = await _tool_result_message(db_session, conversation_id)
    assert "invalid arguments" in tool_result.content

    execution = (await _executions(db_session, conversation_id))[0]
    assert execution.status == ToolExecutionStatus.ERROR
    assert "invalid arguments" in execution.error
    assert execution.arguments == {"orderId": 12345}


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8000/orders", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"],
)
async def test_unsafe_endpoints_are_blocked_before_the_request(
    client, fake_redis, db_session, endpoint
):
    conversation_id = await _run(client, fake_redis, db_session, tool={"endpoint": endpoint})

    async with respx.mock(assert_all_called=False) as router:
        catch_all = router.route().mock(return_value=httpx.Response(200, json={}))
        await _turn(fake_redis, db_session, _provider())

    assert not catch_all.called
    tool_result = await _tool_result_message(db_session, conversation_id)
    assert "blocked endpoint" in tool_result.content

    execution = (await _executions(db_session, conversation_id))[0]
    assert execution.status == ToolExecutionStatus.ERROR


async def test_tool_not_listed_on_the_agent_is_unknown_and_not_recorded(
    client, fake_redis, db_session
):
    conversation_id = await _run(client, fake_redis, db_session)

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _turn(fake_redis, db_session, _provider(tool_name="delete_everything"))

    assert not route.called
    tool_result = await _tool_result_message(db_session, conversation_id)
    assert "unknown tool: delete_everything" in tool_result.content
    assert await _executions(db_session, conversation_id) == []


async def test_non_json_response_is_wrapped_as_text(client, fake_redis, db_session):
    conversation_id = await _run(client, fake_redis, db_session)

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(200, text="plain body"))
        await _turn(fake_redis, db_session, _provider())

    tool_result = await _tool_result_message(db_session, conversation_id)
    assert "plain body" in tool_result.content

    execution = (await _executions(db_session, conversation_id))[0]
    assert execution.status == ToolExecutionStatus.OK
    assert execution.result == {"text": "plain body"}


async def test_the_tool_spec_reaches_the_model(client, fake_redis, db_session):
    await _run(client, fake_redis, db_session)
    provider = _provider()

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={"status": "shipped"}))
        await _turn(fake_redis, db_session, provider)

    specs = provider.calls[0]["tools"]
    assert [spec.name for spec in specs] == ["get_order"]
    assert specs[0].parameters == ORDER_SCHEMA
    assert specs[0].description == "Look up an order by id"
