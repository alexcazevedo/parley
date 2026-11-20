"""A re-run turn does not repeat a tool call (PRODUCT.md E3.7, ticket 05).

- A call an earlier attempt of the same turn already finished (`OK` or
  `ERROR`) is not made again: the saved result is handed to the model.
- "Same turn" is the batch: an earlier run whose `input_message_ids` is a
  non-empty subset of this run's ids (a retry, or a turn merged with newer
  messages). A later turn with other messages calls the tool again.
- A call left `STARTED` (outcome unknown) is repeated only for a GET HTTP tool
  and for built-ins; any other HTTP tool gets `OUTCOME_UNKNOWN_ERROR` and a
  `tools.outcome_unknown` warning.
- A reused or refused call writes no new `ToolExecution` row.
- A call that timed out (other than a connect timeout) counts as unknown
  outcome; a refused connection is a saved error and is reused.
- A reused terminal result (`handoff_to_human`) still ends the loop.
"""

import json
import os
import socket
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import TurnFailed, process_turn
from app.llm.base import ROLE_TOOL, LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools import ledger
from app.tools.base import ToolResult
from app.tools.builtin import BuiltinExecutor
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
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


async def _setup(client, fake_redis, db_session, **agent) -> Conversation:
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
    agent.setdefault("tool_names", ["book", "get_order"])
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


async def _new_inbound(db_session, conversation: Conversation, text: str) -> Message:
    message = Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.IN,
        role=MessageRole.USER,
        type="text",
        content=text,
        created_at=datetime.now(UTC),
    )
    db_session.add(message)
    await db_session.commit()
    return message


def _step(name: str, arguments: dict | None = None) -> LLMResponse:
    arguments = ARGS if arguments is None else arguments
    return LLMResponse(tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)])


def _runtime(provider, executors=None) -> AgentRuntime:
    return AgentRuntime(
        provider=provider, registry=ToolRegistry(executors or [HttpToolExecutor()])
    )


def _tool_contents(provider: FakeLLMProvider) -> list[str]:
    """The tool results this attempt handed the model on its last call.

    Only the trailing ones: an earlier attempt's committed tool messages are
    part of the history the retry's prompt is built from.
    """
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


async def _runs(engine, conversation_id) -> list[AgentRun]:
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as other:
        result = await other.execute(
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at)
        )
        return list(result.scalars().all())


async def _failed_attempt(db_session, conversation, messages, adapter, provider) -> None:
    """One attempt whose tool ran and whose send then failed."""
    adapter.fail = True
    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation.id, messages, _runtime(provider))
    adapter.fail = False


async def _earlier_started_run(
    db_session, conversation: Conversation, messages: list[Message], tool_name: str, args: dict
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
        arguments=args,
        status=ToolExecutionStatus.STARTED,
    )
    db_session.add(execution)
    await db_session.commit()
    return execution


# --- wiring -------------------------------------------------------------------------


async def test_process_turn_records_the_batch_ids_on_the_run(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    provider = FakeLLMProvider([LLMResponse(content="Oi.")])

    await process_turn(db_session, conversation.id, messages, _runtime(provider))

    [run] = await _runs(test_engine, conversation.id)
    assert run.input_message_ids == [str(m.id) for m in messages]


async def test_run_turn_without_ids_stores_none(client, fake_redis, db_session, test_engine):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([LLMResponse(content="Oi.")])

    await _runtime(provider).run_turn(db_session, conversation, "reserva")
    await db_session.commit()

    [run] = await _runs(test_engine, conversation.id)
    assert run.input_message_ids is None


async def test_three_argument_session_runtime_still_works(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)

    class ThreeArgRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def run_turn(self, session, conversation, input_text: str) -> str:
            self.calls += 1
            return "ok"

    runtime = ThreeArgRuntime()
    await process_turn(db_session, conversation.id, messages, runtime)
    assert runtime.calls == 1
    assert adapter.sent == ["ok"]


# --- reuse of a finished call -------------------------------------------------------


async def test_retried_turn_reuses_the_post_result(
    client, fake_redis, db_session, test_engine, adapter
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
        with capture_logs() as logs:
            await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert adapter.sent == ["Reservado."]
    assert _tool_contents(second) == _tool_contents(first)
    assert json.loads(_tool_contents(second)[0]) == {"ok": True, "data": {"booking": "B-1"}}
    assert any(e["event"] == "tools.result_reused" for e in logs)

    # No new row for the reused call; the run still counts it.
    rows = await _committed_executions(test_engine, conversation.id)
    assert len(rows) == 1
    runs = await _runs(test_engine, conversation.id)
    assert [r.tool_calls for r in runs] == [1, 1]


async def test_saved_error_result_is_reused(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Ops.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Ops.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(return_value=httpx.Response(500, text="boom"))
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert _tool_contents(second) == _tool_contents(first)
    content = json.loads(_tool_contents(second)[0])
    assert content["ok"] is False and "HTTP 500" in content["error"]
    assert len(await _committed_executions(test_engine, conversation.id)) == 1


async def test_turn_merged_with_a_newer_message_reuses(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    [m1] = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(
            return_value=httpx.Response(200, json={"booking": "B-1"})
        )
        await _failed_attempt(db_session, conversation, [m1], adapter, first)
        m2 = await _new_inbound(db_session, conversation, "pode ser às 10h")
        await process_turn(db_session, conversation.id, [m1, m2], _runtime(second))

    assert route.call_count == 1
    assert _tool_contents(second) == _tool_contents(first)


async def test_later_turn_with_other_messages_calls_again(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    [m1] = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado de novo.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(
            return_value=httpx.Response(200, json={"booking": "B-1"})
        )
        await process_turn(db_session, conversation.id, [m1], _runtime(first))
        m2 = await _new_inbound(db_session, conversation, "outra reserva")
        await process_turn(db_session, conversation.id, [m2], _runtime(second))

    assert route.call_count == 2


async def test_retry_with_different_arguments_calls_again(
    client, fake_redis, db_session, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])
    second = FakeLLMProvider(
        [_step("book", {"order_id": "999"}), LLMResponse(content="Reservado.")]
    )

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(
            return_value=httpx.Response(200, json={"booking": "B-1"})
        )
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 2


# --- unknown outcome ----------------------------------------------------------------


async def test_started_post_call_is_not_repeated(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    execution = await _earlier_started_run(db_session, conversation, messages, "book", ARGS)
    provider = FakeLLMProvider([_step("book"), LLMResponse(content="Não sei se reservou.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={}))
        with capture_logs() as logs:
            await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert not route.called
    expected = ToolResult.failure(ledger.OUTCOME_UNKNOWN_ERROR).to_content()
    assert _tool_contents(provider) == [expected]
    [warning] = [e for e in logs if e["event"] == "tools.outcome_unknown"]
    assert warning["log_level"] == "warning"
    assert warning["tool"] == "book"
    assert warning["execution_id"] == str(execution.id)
    assert warning["conversation_id"] == str(conversation.id)
    # Only the earlier attempt's row: the refused call wrote none.
    rows = await _committed_executions(test_engine, conversation.id)
    assert [r.id for r in rows] == [execution.id]


async def test_started_get_call_is_repeated(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    await _earlier_started_run(db_session, conversation, messages, "get_order", ARGS)
    provider = FakeLLMProvider([_step("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json={"status": "shipped"})
        )
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert route.call_count == 1
    assert json.loads(_tool_contents(provider)[0]) == {
        "ok": True,
        "data": {"status": "shipped"},
    }
    rows = await _committed_executions(test_engine, conversation.id)
    assert [r.status for r in rows] == [ToolExecutionStatus.STARTED, ToolExecutionStatus.OK]


async def test_finished_row_wins_over_a_started_one(
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
        # A newer attempt that died mid-call does not hide the finished result.
        await _earlier_started_run(db_session, conversation, messages, "book", ARGS)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert _tool_contents(second) == _tool_contents(first)


async def test_started_builtin_call_is_repeated(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    args = {"type": "preference", "content": "prefere PIX"}
    await _earlier_started_run(db_session, conversation, messages, "remember", args)
    provider = FakeLLMProvider([_step("remember", args), LLMResponse(content="Anotado.")])

    await process_turn(
        db_session,
        conversation.id,
        messages,
        _runtime(provider, [BuiltinExecutor(), HttpToolExecutor()]),
    )

    content = json.loads(_tool_contents(provider)[0])
    assert content["ok"] is True
    async with async_sessionmaker(bind=test_engine)() as other:
        memory = await other.get(Memory, uuid.UUID(content["data"]["id"]))
        assert memory is not None and memory.content == "prefere PIX"


async def test_started_row_of_a_later_turn_is_ignored(
    client, fake_redis, db_session, adapter
):
    """A `STARTED` row from a run over other messages is not this turn's."""
    conversation = await _setup(client, fake_redis, db_session)
    [m1] = await _inbound(db_session, conversation.id)
    other = await _new_inbound(db_session, conversation, "outra")
    await _earlier_started_run(db_session, conversation, [other], "book", ARGS)
    provider = FakeLLMProvider([_step("book"), LLMResponse(content="Reservado.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(
            return_value=httpx.Response(200, json={"booking": "B-1"})
        )
        await process_turn(db_session, conversation.id, [m1], _runtime(provider))

    assert route.call_count == 1


# --- timeouts are unknown outcome (ticket 10) ---------------------------------------


async def test_get_that_timed_out_is_called_again(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("get_order"), LLMResponse(content="Ops.")])
    second = FakeLLMProvider([_step("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.Response(200, json={"status": "shipped"}),
            ]
        )
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 2
    assert json.loads(_tool_contents(first)[0]) == {
        "ok": False,
        "error": "tool timed out after 5s",
    }
    assert json.loads(_tool_contents(second)[0]) == {
        "ok": True,
        "data": {"status": "shipped"},
    }
    assert adapter.sent == ["Saiu."]
    rows = await _committed_executions(test_engine, conversation.id)
    assert [r.status for r in rows] == [ToolExecutionStatus.STARTED, ToolExecutionStatus.OK]


async def test_post_that_timed_out_is_not_called_again(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Ops.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Não sei se reservou.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        with capture_logs() as logs:
            await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert json.loads(_tool_contents(first)[0]) == {
        "ok": False,
        "error": "tool timed out after 5s",
    }
    expected = ToolResult.failure(ledger.OUTCOME_UNKNOWN_ERROR).to_content()
    assert _tool_contents(second) == [expected]
    assert any(e["event"] == "tools.outcome_unknown" for e in logs)
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.STARTED
    assert row.error == "tool timed out after 5s"


async def test_refused_connection_is_saved_as_error_and_reused(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    first = FakeLLMProvider([_step("book"), LLMResponse(content="Ops.")])
    second = FakeLLMProvider([_step("book"), LLMResponse(content="Ops.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(side_effect=httpx.ConnectError("refused"))
        await _failed_attempt(db_session, conversation, messages, adapter, first)
        await process_turn(db_session, conversation.id, messages, _runtime(second))

    assert route.call_count == 1
    assert _tool_contents(second) == _tool_contents(first)
    assert json.loads(_tool_contents(second)[0]) == {
        "ok": False,
        "error": "request failed: ConnectError",
    }
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.status == ToolExecutionStatus.ERROR


# --- a reused terminal result still ends the loop (ticket 10) -----------------------


async def test_reused_handoff_ends_the_loop_with_the_handoff_message(
    client, fake_redis, db_session, test_engine, fake_handoff_brief
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    message_ids = [m.id for m in messages]
    handoff = _step("handoff_to_human", {"reason": "pediu humano"})
    executors = [BuiltinExecutor(), HttpToolExecutor()]
    first = FakeLLMProvider([handoff])
    second = FakeLLMProvider([handoff, LLMResponse(content="não devia chegar aqui")])

    first_reply = await _runtime(first, executors).run_turn(
        db_session, conversation, "reserva", message_ids=message_ids
    )
    await db_session.commit()
    with capture_logs() as logs:
        second_reply = await _runtime(second, executors).run_turn(
            db_session, conversation, "reserva", message_ids=message_ids
        )
    await db_session.commit()

    agent_message = "Vou te transferir para um atendente humano."
    assert first_reply == agent_message
    assert second_reply == agent_message
    assert second.call_count == 1
    assert any(e["event"] == "tools.result_reused" for e in logs)
    # The reused call wrote no new row.
    [row] = await _committed_executions(test_engine, conversation.id)
    assert row.tool_name == "handoff_to_human"
