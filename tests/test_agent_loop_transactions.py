"""The agent loop holds no transaction across model calls (PRODUCT.md E3.6).

- `session.in_transaction()` is `False` whenever the runtime calls the model,
  the reply check included.
- The `AgentRun` row is committed, status `RUNNING`, before the first model
  call, and survives a turn whose send fails afterwards. The runtime commits
  its final status itself, so a lock lost right after it returned does not
  leave the run `RUNNING`.
- The time limit is a deadline: it wraps each model call only, never a
  database operation or a tool, so a timed-out turn leaves the session usable.
- A model step's tool messages are added to the session only after every
  call of that step has returned.
"""

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.runtime import ERROR_MESSAGE, AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import LockLost, TurnFailed, process_turn
from app.llm.base import LLMResponse, ToolCall, ToolSpec
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.base import ToolResult
from app.tools.registry import ToolRegistry
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
BOUNDED = {
    "boundary_scope": "pedidos da loja",
    "boundary_never": ["prometer preços"],
    "boundary_refusal_message": "Só falo de pedidos.",
}


class RecordingProvider(FakeLLMProvider):
    """`FakeLLMProvider` that also records `session.in_transaction()` per call."""

    def __init__(self, session, scripted, **kwargs) -> None:
        super().__init__(scripted, **kwargs)
        self.session = session
        self.in_transaction: list[bool] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.in_transaction.append(self.session.in_transaction())
        return await super().complete(
            messages, tools, model=model, temperature=temperature, max_tokens=max_tokens
        )


class RunVisibleProvider(FakeLLMProvider):
    """Reads the turn's AgentRun from a *separate* session during the call."""

    def __init__(self, engine, conversation_id, scripted) -> None:
        super().__init__(scripted)
        self.engine = engine
        self.conversation_id = conversation_id
        self.seen: list[tuple[str, AgentRunStatus] | None] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        async with async_sessionmaker(bind=self.engine)() as other:
            result = await other.execute(
                select(AgentRun.input_text, AgentRun.status).where(
                    AgentRun.conversation_id == self.conversation_id
                )
            )
            rows = list(result.all())
        self.seen.append(tuple(rows[0]) if len(rows) == 1 else None)
        return await super().complete(
            messages, tools, model=model, temperature=temperature, max_tokens=max_tokens
        )


class SlowProvider:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        await asyncio.sleep(self.seconds)
        return LLMResponse(content="tarde demais")


class StubExecutor:
    """Scripted tool; `work` runs inside `execute` with the tool context."""

    def __init__(self, name: str = "get_order", work=None) -> None:
        self.name = name
        self.work = work
        self.started: list[str] = []
        self.finished: list[str] = []
        self.tool_call_rows_pending: list[bool] = []

    def can_handle(self, name: str) -> bool:
        return name == self.name

    async def specs(self, agent, session) -> list[ToolSpec]:
        return [ToolSpec(name=self.name, description="Look up an order", parameters={})]

    async def execute(self, name: str, arguments: dict, ctx) -> ToolResult:
        self.started.append(arguments["order_id"])
        self.tool_call_rows_pending.append(
            any(isinstance(obj, Message) and obj.type == "tool_call" for obj in ctx.session.new)
        )
        if self.work is not None:
            await self.work(ctx)
        self.finished.append(arguments["order_id"])
        return ToolResult.success({"order_id": arguments["order_id"], "status": "shipped"})


async def _setup(client, fake_redis, db_session, **agent_overrides) -> Conversation:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            **agent_overrides,
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


async def _run_turn(db_session, conversation, runtime) -> str:
    await db_session.execute(select(1))
    assert db_session.in_transaction()  # starts inside one, like the worker's turn
    return await runtime.run_turn(db_session, conversation, "cadê?")


async def _runs(session, conversation_id) -> list[AgentRun]:
    result = await session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return list(result.scalars().all())


def _tool_step(*order_ids: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"call_{order_id}", name="get_order", arguments={"order_id": order_id})
            for order_id in order_ids
        ]
    )


# --- no transaction at any model call ------------------------------------------


async def test_every_model_call_runs_outside_a_transaction(client, fake_redis, db_session):
    conversation = await _setup(client, fake_redis, db_session, **BOUNDED)
    provider = RecordingProvider(
        db_session,
        [_tool_step("1"), LLMResponse(content="Seu pedido saiu."), LLMResponse(content="ALLOW")],
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([StubExecutor()]))

    reply = await _run_turn(db_session, conversation, runtime)

    assert reply == "Seu pedido saiu."
    # Tool step, final reply, reply check.
    assert provider.in_transaction == [False, False, False]


# --- the AgentRun row -------------------------------------------------------------


async def test_agent_run_is_committed_running_before_the_first_model_call(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = RunVisibleProvider(test_engine, conversation.id, [LLMResponse(content="Oi!")])

    await _run_turn(db_session, conversation, AgentRuntime(provider=provider))

    assert provider.seen == [("cadê?", AgentRunStatus.RUNNING)]


async def test_failed_send_after_the_runtime_returned_keeps_the_agent_run(
    client, fake_redis, db_session, monkeypatch
):
    conversation = await _setup(client, fake_redis, db_session)

    class FailingAdapter:
        async def send_message(self, message, contact, channel) -> None:
            raise RuntimeError("provider returned 503")

    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: FailingAdapter())
    provider = FakeLLMProvider([LLMResponse(content="Oi!")])
    result = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.IN,
        )
    )
    inbound = list(result.scalars().all())

    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation.id, inbound, AgentRuntime(provider=provider))

    [run] = await _runs(db_session, conversation.id)
    assert run.input_text == "cadê?"


async def test_lock_lost_right_after_the_runtime_leaves_no_running_run(
    client, fake_redis, db_session, monkeypatch, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    conversation_id = conversation.id  # the lock-loss rollback expires the row

    class RecordingAdapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, message, contact, channel) -> None:
            self.sent.append(message.content)

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    result = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.IN,
        )
    )
    inbound = list(result.scalars().all())

    async def lost() -> bool:
        return False  # first asked right before the send, after the runtime returned

    outcome = await process_turn(
        db_session,
        conversation.id,
        inbound,
        AgentRuntime(provider=FakeLLMProvider([LLMResponse(content="Oi!")])),
        ownership_check=lost,
    )

    assert isinstance(outcome, LockLost)
    assert adapter.sent == []
    async with async_sessionmaker(bind=test_engine)() as other:
        [run] = await _runs(other, conversation_id)
    assert run.status == AgentRunStatus.SUCCESS
    assert run.output_text == "Oi!"
    assert run.llm_calls == 1
    assert run.latency_ms is not None


# --- the deadline -----------------------------------------------------------------


async def test_model_call_past_the_deadline_errors_and_leaves_the_session_usable(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    runtime = AgentRuntime(provider=SlowProvider(5), timeout_seconds=0.1)

    started = time.perf_counter()
    reply = await _run_turn(db_session, conversation, runtime)

    assert time.perf_counter() - started < 2
    assert reply == ERROR_MESSAGE
    # The same session still queries and commits.
    [run] = await _runs(db_session, conversation.id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error is not None and run.error.startswith("TimeoutError")
    assert run.output_text == ERROR_MESSAGE
    await db_session.commit()
    async with async_sessionmaker(bind=test_engine)() as other:
        [stored] = await _runs(other, conversation.id)
        assert stored.status == AgentRunStatus.ERROR


async def test_deadline_never_cancels_a_database_call_inside_a_tool(
    client, fake_redis, db_session
):
    conversation = await _setup(client, fake_redis, db_session)

    async def slow_query(ctx) -> None:
        await ctx.session.execute(text("SELECT pg_sleep(0.3)"))

    stub = StubExecutor(work=slow_query)
    provider = FakeLLMProvider([_tool_step("1"), LLMResponse(content="não chega aqui")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([stub]), timeout_seconds=0.1)

    reply = await _run_turn(db_session, conversation, runtime)

    # The tool's query ran to the end; the expired deadline stopped the next
    # model step instead.
    assert stub.finished == ["1"]
    assert provider.call_count == 1
    assert reply == ERROR_MESSAGE
    [run] = await _runs(db_session, conversation.id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error is not None and run.error.startswith("TimeoutError")
    await db_session.execute(select(1))
    await db_session.commit()


async def test_expired_deadline_stops_before_the_next_tool_call(client, fake_redis, db_session):
    conversation = await _setup(client, fake_redis, db_session)

    async def slow(ctx) -> None:
        await asyncio.sleep(0.3)

    stub = StubExecutor(work=slow)
    provider = FakeLLMProvider([_tool_step("1", "2"), LLMResponse(content="não chega aqui")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([stub]), timeout_seconds=0.1)

    reply = await _run_turn(db_session, conversation, runtime)

    assert stub.started == ["1"]  # the second call never starts
    assert stub.finished == ["1"]  # the first is not cut off
    assert reply == ERROR_MESSAGE
    [run] = await _runs(db_session, conversation.id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error is not None and run.error.startswith("TimeoutError")
    await db_session.commit()


# --- tool messages ----------------------------------------------------------------


async def test_tool_messages_are_added_after_every_call_of_the_step_returned(
    client, fake_redis, db_session
):
    conversation = await _setup(client, fake_redis, db_session)
    stub = StubExecutor()
    provider = FakeLLMProvider([_tool_step("1", "2"), LLMResponse(content="Ambos saíram.")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([stub]))

    reply = await _run_turn(db_session, conversation, runtime)

    assert reply == "Ambos saíram."
    assert stub.finished == ["1", "2"]
    # No tool_call row was pending in the session while a tool ran.
    assert stub.tool_call_rows_pending == [False, False]

    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.role != MessageRole.USER)
        .order_by(Message.created_at)
    )
    rows = list(result.scalars().all())
    assert [(m.type, m.tool_call_id) for m in rows] == [
        ("tool_call", "call_1"),
        ("tool_result", "call_1"),
        ("tool_result", "call_2"),
    ]
    stamps = [m.created_at for m in rows]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
