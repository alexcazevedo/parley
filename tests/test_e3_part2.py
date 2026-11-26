"""PRODUCT.md's success demo for E3 part 2, one test per clause, each driven
through `process_turn` with `AgentRuntime`.

1. no transaction is open while the model or a tool is called;
2. a timed-out model call leaves the session usable;
3. a retried turn does not call a returned tool again and gets its saved result;
4. unknown outcome: a non-GET call is not repeated, a GET call is;
5. a tool call's record survives a failed turn;
6. a model call answered 429 then 200 does not fail the turn;
7. a non-GET tool answered 503 is called exactly once;
8. the HTTP client is created once and closed on shutdown.
"""

import asyncio
import json
import os
import socket
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import http_client
from app.agents.runtime import ERROR_MESSAGE, AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import TurnFailed, process_turn
from app.llm.base import ROLE_TOOL, LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools import ledger
from app.tools.base import ToolResult
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
LLM_URL = "https://llm.example.com/v1"
CHAT_URL = f"{LLM_URL}/chat/completions"
BOOK_URL = "https://example.test/bookings"
ORDERS_URL = "https://example.test/orders"
SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}
ARGS = {"order_id": "12345"}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """`example.test` resolves to a public address (feeds the SSRF guard only)."""
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host == "example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)


class SwitchableAdapter:
    """Channel double: every send raises while `fail` is set, else is recorded."""

    def __init__(self) -> None:
        self.fail = False
        self.sent: list[str] = []

    async def send_message(self, message, contact, channel) -> None:
        if self.fail:
            raise RuntimeError("provider returned 503")
        self.sent.append(message.content)


@pytest.fixture
def adapter(monkeypatch) -> SwitchableAdapter:
    double = SwitchableAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: double)
    return double


class TransactionProbeProvider(FakeLLMProvider):
    """Records `session.in_transaction()` at every model call."""

    def __init__(self, session, scripted) -> None:
        super().__init__(scripted)
        self.session = session
        self.in_transaction: list[bool] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.in_transaction.append(self.session.in_transaction())
        return await super().complete(
            messages, tools, model=model, temperature=temperature, max_tokens=max_tokens
        )


class SlowProvider:
    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        await asyncio.sleep(5)
        return LLMResponse(content="tarde demais")


async def _setup(client, fake_redis, db_session) -> Conversation:
    """Tenant with a POST `book` and a GET `get_order` tool, one inbound message."""
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    for name, url, method in (("book", BOOK_URL, "POST"), ("get_order", ORDERS_URL, "GET")):
        created = await client.post(
            "/tools",
            json={
                "name": name,
                "description": name,
                "input_schema": SCHEMA,
                "endpoint": url,
                "method": method,
                "timeout_seconds": 5,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            "tool_names": ["book", "get_order"],
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "reserva"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    conversation = await db_session.get(
        Conversation, uuid.UUID(response.json()["conversation_id"])
    )
    assert conversation is not None
    return conversation


async def _inbound(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
            Message.role == MessageRole.USER,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


def _step(name: str, arguments: dict | None = None) -> LLMResponse:
    arguments = ARGS if arguments is None else arguments
    return LLMResponse(tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)])


def _runtime(provider, **kwargs) -> AgentRuntime:
    return AgentRuntime(
        provider=provider, registry=ToolRegistry([HttpToolExecutor()]), **kwargs
    )


def _tool_contents(provider: FakeLLMProvider) -> list[str]:
    """The tool results this attempt handed the model on its last call (trailing ones)."""
    trailing: list[str] = []
    for message in reversed(provider.calls[-1]["messages"]):
        if message.role != ROLE_TOOL:
            break
        trailing.append(message.content)
    return list(reversed(trailing))


async def _committed_executions(engine, conversation_id) -> list[ToolExecution]:
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as other:
        runs = select(AgentRun.id).where(AgentRun.conversation_id == conversation_id)
        result = await other.execute(
            select(ToolExecution)
            .where(ToolExecution.agent_run_id.in_(runs))
            .order_by(ToolExecution.created_at)
        )
        return list(result.scalars().all())


async def _failed_attempt(db_session, conversation, messages, adapter, provider) -> None:
    """One attempt whose tool ran and whose send then failed."""
    adapter.fail = True
    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation.id, messages, _runtime(provider))
    adapter.fail = False


async def _earlier_started_run(
    db_session, conversation: Conversation, messages: list[Message], tool_name: str
) -> ToolExecution:
    """An earlier attempt of this batch that died mid-call: its row is `STARTED`."""
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        status=AgentRunStatus.RUNNING,
        input_text="reserva",
        input_message_ids=[str(m.id) for m in messages],
    )
    db_session.add(run)
    await db_session.flush()
    execution = ToolExecution(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        agent_run_id=run.id,
        tool_name=tool_name,
        arguments=ARGS,
        status=ToolExecutionStatus.STARTED,
    )
    db_session.add(execution)
    await db_session.commit()
    return execution


# 1. No transaction while the model or a tool is called ---------------------------


async def test_no_transaction_is_open_while_the_model_or_a_tool_is_called(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    provider = TransactionProbeProvider(
        db_session, [_step("book"), LLMResponse(content="Reservado.")]
    )
    in_transaction_during_tool: list[bool] = []

    def tool_answer(request: httpx.Request) -> httpx.Response:
        in_transaction_during_tool.append(db_session.in_transaction())
        return httpx.Response(200, json={"booking": "B-1"})

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(side_effect=tool_answer)
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert adapter.sent == ["Reservado."]
    assert route.call_count == 1
    assert in_transaction_during_tool == [False]
    assert provider.in_transaction == [False, False]


# 2. A timed-out model call leaves the session usable -----------------------------


async def test_timed_out_model_call_leaves_the_session_usable(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)

    await process_turn(
        db_session,
        conversation.id,
        messages,
        AgentRuntime(provider=SlowProvider(), timeout_seconds=0.1),
    )

    assert adapter.sent == [ERROR_MESSAGE]
    # The same session still queries and commits, and the run is on record.
    await db_session.execute(select(1))
    await db_session.commit()
    async with async_sessionmaker(bind=test_engine)() as other:
        result = await other.execute(
            select(AgentRun).where(AgentRun.conversation_id == conversation.id)
        )
        [run] = list(result.scalars().all())
    assert run.status == AgentRunStatus.ERROR
    assert run.error is not None and run.error.startswith("TimeoutError")


# 3. A retried turn gets the saved result, the tool is not called again -----------


async def test_retried_turn_does_not_call_a_returned_tool_again(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(
            return_value=httpx.Response(200, json={"booking": "B-1"})
        )
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert adapter.sent == ["Reservado."]
    assert _tool_contents(second) == _tool_contents(first)
    assert json.loads(_tool_contents(second)[0]) == {"ok": True, "data": {"booking": "B-1"}}


# 4. Unknown outcome: non-GET not repeated, GET repeated --------------------------


async def test_unknown_outcome_is_not_repeated_for_post_but_is_for_get(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    await _earlier_started_run(db_session, conversation, messages, "book")
    await _earlier_started_run(db_session, conversation, messages, "get_order")
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="book", arguments=ARGS),
                    ToolCall(id="call_2", name="get_order", arguments=ARGS),
                ]
            ),
            LLMResponse(content="Verifiquei."),
        ]
    )

    async with respx.mock(assert_all_called=False) as router:
        post_route = router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={}))
        get_route = router.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json={"status": "shipped"})
        )
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert not post_route.called
    assert get_route.call_count == 1
    book_content, order_content = _tool_contents(provider)
    assert book_content == ToolResult.failure(ledger.OUTCOME_UNKNOWN_ERROR).to_content()
    assert json.loads(order_content) == {"ok": True, "data": {"status": "shipped"}}


# 5. A tool call's record survives a failed turn ----------------------------------


async def test_tool_record_survives_a_failed_turn(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    provider = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])

    async with respx.mock(assert_all_called=False) as router:
        router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={"booking": "B-1"}))
        adapter.fail = True
        with pytest.raises(TurnFailed):
            await process_turn(db_session, conversation.id, messages, _runtime(provider))
    await db_session.rollback()  # what the worker does with the failed turn's session

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.tool_name == "book"
    assert row.status == ToolExecutionStatus.OK
    assert row.result == {"booking": "B-1"}


# 6. Model call 429 then 200 does not fail the turn -------------------------------


async def test_model_429_then_200_does_not_fail_the_turn(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    provider = OpenAICompatibleProvider(base_url=LLM_URL, api_key="k")

    async with respx.mock(assert_all_called=False) as router:
        chat = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, text="slow down"),
                httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "Oi!"}}], "usage": {}},
                ),
            ]
        )
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert chat.call_count == 2
    assert adapter.sent == ["Oi!"]


# 7. A non-GET tool answered 503 is called exactly once ---------------------------


async def test_post_tool_503_is_called_exactly_once(client, fake_redis, db_session, adapter):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    provider = FakeLLMProvider([_step("book"), LLMResponse(content="Não consegui reservar.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(return_value=httpx.Response(503, text="busy"))
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert route.call_count == 1
    content = json.loads(_tool_contents(provider)[0])
    assert content["ok"] is False and "HTTP 503" in content["error"]
    assert adapter.sent == ["Não consegui reservar."]


# 8. The client is created once and closed on shutdown ----------------------------


async def test_http_client_is_created_once_and_closed_on_shutdown(
    client, fake_redis, db_session, adapter, monkeypatch
):
    from app.main import create_app

    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    created: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.__init__

    def counting_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)
    # The model call and the tool call are both outbound HTTP in one turn.
    provider = OpenAICompatibleProvider(base_url=LLM_URL, api_key="k")
    tool_call = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "book", "arguments": json.dumps(ARGS)},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    final = {"choices": [{"message": {"content": "Reservado."}}], "usage": {}}

    async with respx.mock(assert_all_called=False) as router:
        router.post(CHAT_URL).mock(
            side_effect=[httpx.Response(200, json=tool_call), httpx.Response(200, json=final)]
        )
        router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={"booking": "B-1"}))
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert adapter.sent[0] == "Reservado."
    shared = http_client.shared_client()
    assert created == [shared]  # one client for the model call and the tool call

    app = create_app()
    async with app.router.lifespan_context(app):
        assert not shared.is_closed
    assert shared.is_closed
