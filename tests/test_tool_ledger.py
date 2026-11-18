"""Tool calls are recorded first (PRODUCT.md E3.7, ticket 04).

- A tool call's `ToolExecution` row is committed with status `STARTED` before
  the tool runs, and completed (`OK`/`ERROR`, `model_content`) and committed
  again when it returns.
- An HTTP tool's endpoint is called with no transaction open.
- The finished row survives a turn that fails afterwards.
- Built-ins (`remember`, `handoff_to_human`) leave one finished row each, and
  their own writes are committed together with it.
- A call interrupted by cancellation leaves its row `STARTED`.
- Unknown and disabled tools leave no row.
- An HTTP call that may have reached the endpoint (a timeout other than a
  connect timeout, another transport error, an unexpected exception) stays
  `STARTED` with `error`/`latency_ms`; one that never left (connect error or
  timeout, blocked endpoint, invalid arguments) finishes as `ERROR`.
- A terminal result's text (`handoff_to_human`) is stored and replayed.
- `find_prior`'s filter and join columns are indexed.
"""

import asyncio
import json
import os
import socket
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import TurnFailed, process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff
from app.models.memory import Memory
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools import ledger
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
# The agent's default `handoff_message`.
HANDOFF_MESSAGE = "Vou te transferir para um atendente humano."
ORDERS_URL = "https://example.test/orders"
ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}


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


async def _setup(
    client, fake_redis, db_session, *, http_tool: bool = True, **agent
) -> Conversation:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    if http_tool:
        created = await client.post(
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
        assert created.status_code == 201, created.text
        agent.setdefault("tool_names", ["get_order"])
    created_agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            **agent,
        },
        headers=headers,
    )
    assert created_agent.status_code == 201, created_agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": created_agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "cadê?"},
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


def _step(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)])


def _order_step(arguments: dict | None = None) -> LLMResponse:
    return _step("get_order", {"order_id": "12345"} if arguments is None else arguments)


async def _executions(session, conversation_id) -> list[ToolExecution]:
    runs = select(AgentRun.id).where(AgentRun.conversation_id == conversation_id)
    result = await session.execute(
        select(ToolExecution)
        .where(ToolExecution.agent_run_id.in_(runs))
        .order_by(ToolExecution.created_at)
    )
    return list(result.scalars().all())


async def _committed_executions(engine, conversation_id) -> list[ToolExecution]:
    """What another session sees: only committed rows."""
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as other:
        return await _executions(other, conversation_id)


async def _tool_result_contents(session, conversation_id) -> list[str]:
    result = await session.execute(
        select(Message.content)
        .where(Message.conversation_id == conversation_id, Message.role == MessageRole.TOOL)
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


# --- HTTP tools ---------------------------------------------------------------------


async def test_http_tool_row_is_committed_started_while_the_endpoint_is_called(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    seen: list[list[tuple[str, ToolExecutionStatus]]] = []
    in_transaction: list[bool] = []

    async def endpoint(request):
        rows = await _committed_executions(test_engine, conversation.id)
        seen.append([(row.tool_name, row.status) for row in rows])
        in_transaction.append(db_session.in_transaction())
        return httpx.Response(200, json={"status": "shipped"})

    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Saiu.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))
    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(side_effect=endpoint)
        reply = await runtime.run_turn(db_session, conversation, "cadê?")

    assert reply == "Saiu."
    assert route.call_count == 1
    assert seen == [[("get_order", ToolExecutionStatus.STARTED)]]
    assert in_transaction == [False]

    # Finished and committed when it returned, with today's field values.
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.OK
    assert row.tool_id is not None
    assert row.arguments == {"order_id": "12345"}
    assert row.result == {"status": "shipped"}
    assert row.error is None
    assert row.latency_ms is not None
    assert row.model_content == (await _tool_result_contents(db_session, conversation.id))[0]
    assert json.loads(row.model_content) == {"ok": True, "data": {"status": "shipped"}}


async def test_turn_failing_after_the_tool_returned_keeps_the_ok_row(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    conversation = await _setup(client, fake_redis, db_session)

    class FailingAdapter:
        async def send_message(self, message, contact, channel) -> None:
            raise RuntimeError("provider returned 503")

    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: FailingAdapter())
    result = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.IN,
        )
    )
    inbound = list(result.scalars().all())
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Saiu.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={"status": "shipped"}))
        with pytest.raises(TurnFailed):
            await process_turn(db_session, conversation.id, inbound, runtime)

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.OK
    assert row.result == {"status": "shipped"}
    assert json.loads(row.model_content) == {"ok": True, "data": {"status": "shipped"}}


async def test_invalid_arguments_finish_the_row_as_error_without_a_request(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_order_step({"orderId": 1}), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await runtime.run_turn(db_session, conversation, "cadê?")

    assert not route.called
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR
    assert row.arguments == {"orderId": 1}
    assert row.result is None
    assert row.error is not None and "invalid arguments" in row.error
    content = json.loads(row.model_content)
    assert content["ok"] is False and "invalid arguments" in content["error"]


async def test_http_error_finishes_the_row_as_error_with_model_content(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(500, text="boom"))
        await runtime.run_turn(db_session, conversation, "cadê?")

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR
    assert row.result is None
    assert row.error is not None and "HTTP 500" in row.error
    assert row.model_content == (await _tool_result_contents(db_session, conversation.id))[0]


async def test_cancelled_call_leaves_the_row_started(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    conversation_id = conversation.id
    entered = asyncio.Event()

    async def endpoint(request):
        entered.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    provider = FakeLLMProvider([_order_step(), LLMResponse(content="não chega aqui")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))
    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(side_effect=endpoint)
        task = asyncio.create_task(runtime.run_turn(db_session, conversation, "cadê?"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await db_session.rollback()

    [row] = await _committed_executions(test_engine, conversation_id)
    assert row.status == ToolExecutionStatus.STARTED
    assert row.result is None
    assert row.model_content is None
    assert row.latency_ms is None


async def test_unknown_http_tool_leaves_no_row(client, fake_redis, db_session, test_engine):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_step("nope", {}), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    await runtime.run_turn(db_session, conversation, "cadê?")

    assert await _executions(db_session, conversation.id) == []


# --- built-ins ----------------------------------------------------------------------


async def test_remember_leaves_one_finished_row_committed_with_the_memory(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session, http_tool=False)
    conversation_id = conversation.id
    provider = FakeLLMProvider(
        [
            _step("remember", {"type": "preference", "content": "prefere PIX"}),
            LLMResponse(content="Anotado."),
        ]
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    reply = await runtime.run_turn(db_session, conversation, "cadê?")
    assert reply == "Anotado."
    # A failed turn would roll back now; the tool's writes are already safe.
    await db_session.rollback()

    [row] = await _committed_executions(test_engine, conversation_id)
    assert row.tool_name == "remember"
    assert row.tool_id is None
    assert row.status == ToolExecutionStatus.OK
    assert row.arguments == {"type": "preference", "content": "prefere PIX"}
    assert row.error is None
    assert row.latency_ms is not None
    memory_id = row.result["id"]
    assert json.loads(row.model_content) == {"ok": True, "data": {"id": memory_id}}
    async with async_sessionmaker(bind=test_engine)() as other:
        memory = await other.get(Memory, uuid.UUID(memory_id))
        assert memory is not None and memory.content == "prefere PIX"


async def test_handoff_leaves_one_finished_row_committed_with_the_handoff(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session, http_tool=False)
    conversation_id = conversation.id
    provider = FakeLLMProvider([_step("handoff_to_human", {"reason": "pediu humano"})])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    await runtime.run_turn(db_session, conversation, "cadê?")
    await db_session.rollback()

    [row] = await _committed_executions(test_engine, conversation_id)
    assert row.tool_name == "handoff_to_human"
    assert row.tool_id is None
    assert row.status == ToolExecutionStatus.OK
    assert row.result == {"ok": True}
    assert json.loads(row.model_content) == {"ok": True, "data": {"ok": True}}
    async with async_sessionmaker(bind=test_engine)() as other:
        stored = await other.get(Conversation, conversation_id)
        assert stored.state == ConversationState.HUMAN_HANDOFF
        handoffs = (
            await other.execute(select(Handoff).where(Handoff.conversation_id == conversation_id))
        ).scalars().all()
        assert len(handoffs) == 1


async def test_builtin_invalid_arguments_finish_the_row_as_error(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session, http_tool=False)
    provider = FakeLLMProvider([_step("remember", {"content": ""}), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    await runtime.run_turn(db_session, conversation, "cadê?")

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR
    assert row.result is None
    assert row.error is not None
    assert json.loads(row.model_content)["ok"] is False


async def test_disabled_builtin_leaves_no_row(client, fake_redis, db_session):
    conversation = await _setup(
        client, fake_redis, db_session, http_tool=False, memory_enabled=False
    )
    provider = FakeLLMProvider(
        [_step("remember", {"type": "preference", "content": "x"}), LLMResponse(content="Ok.")]
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    await runtime.run_turn(db_session, conversation, "cadê?")

    assert await _executions(db_session, conversation.id) == []


# --- unknown outcome: what a failed HTTP call leaves (ticket 10) --------------------


@pytest.mark.parametrize(
    ("raised", "expected_error"),
    [
        (httpx.ReadTimeout("slow"), "tool timed out after 5s"),
        (httpx.WriteTimeout("slow"), "tool timed out after 5s"),
        (httpx.RemoteProtocolError("dropped"), "request failed: RemoteProtocolError"),
        (RuntimeError("boom"), "tool execution failed"),
    ],
    ids=["read-timeout", "write-timeout", "protocol-error", "unexpected"],
)
async def test_call_that_may_have_reached_the_endpoint_stays_started(
    client, fake_redis, db_session, test_engine, raised, expected_error
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(side_effect=raised)
        reply = await runtime.run_turn(db_session, conversation, "cadê?")

    assert reply == "Ops."
    assert route.call_count == 1
    # The model of this attempt gets today's failure text.
    [content] = await _tool_result_contents(db_session, conversation.id)
    assert json.loads(content) == {"ok": False, "error": expected_error}

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.STARTED
    assert row.error == expected_error
    assert row.latency_ms is not None
    assert row.result is None
    assert row.model_content is None


@pytest.mark.parametrize(
    ("raised", "expected_error"),
    [
        (httpx.ConnectError("refused"), "request failed: ConnectError"),
        (httpx.ConnectTimeout("too slow"), "tool timed out after 5s"),
    ],
    ids=["connect-error", "connect-timeout"],
)
async def test_call_that_never_left_finishes_as_error(
    client, fake_redis, db_session, test_engine, raised, expected_error
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(side_effect=raised)
        await runtime.run_turn(db_session, conversation, "cadê?")

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR
    assert row.error == expected_error
    assert json.loads(row.model_content) == {"ok": False, "error": expected_error}


async def test_blocked_endpoint_finishes_as_error(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    conversation = await _setup(client, fake_redis, db_session)

    def private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Ops.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await runtime.run_turn(db_session, conversation, "cadê?")

    assert not route.called
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR
    assert row.error is not None and row.error.startswith("blocked endpoint")


# --- terminal results survive reuse (ticket 10) -------------------------------------


async def test_handoff_row_stores_the_terminal_text(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session, http_tool=False)
    provider = FakeLLMProvider([_step("handoff_to_human", {"reason": "pediu humano"})])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))

    reply = await runtime.run_turn(db_session, conversation, "cadê?")

    assert reply == HANDOFF_MESSAGE
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.terminal_text == HANDOFF_MESSAGE


async def test_non_terminal_row_has_no_terminal_text(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_order_step(), LLMResponse(content="Saiu.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))

    async with respx.mock(assert_all_called=False) as router:
        router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
        await runtime.run_turn(db_session, conversation, "cadê?")

    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.terminal_text is None


def test_replay_restores_the_terminal_meta():
    row = ToolExecution(
        status=ToolExecutionStatus.OK,
        result={"ok": True},
        model_content=json.dumps({"ok": True, "data": {"ok": True}}),
        terminal_text="Já chamo alguém.",
    )

    result = ledger.replay(row)

    assert result.ok is True
    assert result.meta == {"reused": True, "terminal": True, "final_text": "Já chamo alguém."}
    assert result.to_content() == row.model_content


def test_replay_of_a_non_terminal_row_is_only_reused():
    row = ToolExecution(
        status=ToolExecutionStatus.OK,
        result={"status": "ok"},
        model_content=json.dumps({"ok": True, "data": {"status": "ok"}}),
    )

    assert ledger.replay(row).meta == {"reused": True}


# --- indexes for find_prior (ticket 10) ---------------------------------------------


async def test_find_prior_columns_are_indexed(test_engine):
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE indexname IN ('ix_agent_runs_conversation_id', "
                "'ix_tool_executions_agent_run_id')"
            )
        )
        rows = {row.indexname: (row.tablename, row.indexdef) for row in result}

    assert set(rows) == {"ix_agent_runs_conversation_id", "ix_tool_executions_agent_run_id"}
    assert rows["ix_agent_runs_conversation_id"][0] == "agent_runs"
    assert "(conversation_id)" in rows["ix_agent_runs_conversation_id"][1]
    assert rows["ix_tool_executions_agent_run_id"][0] == "tool_executions"
    assert "(agent_run_id)" in rows["ix_tool_executions_agent_run_id"][1]
