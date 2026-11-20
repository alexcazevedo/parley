"""The agent loop end-to-end over the real pipeline: ingest -> debounce ->
scheduler tick -> AgentRuntime -> persisted AgentRun/tool messages/reply.

The LLM is scripted (`FakeLLMProvider`) and tools are stubbed, so nothing
leaves the process.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

from app.agents.runtime import (
    ERROR_MESSAGE,
    MAX_ITERATIONS_ERROR,
    MAX_ITERATIONS_MESSAGE,
    AgentRuntime,
)
from app.llm.base import LLMError, LLMResponse, LLMUsage, ToolCall, ToolSpec
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.base import ToolResult
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2


class StubExecutor:
    """A single scripted tool, standing in for ticket 06's HTTP executor."""

    def __init__(self, name: str = "get_order", result: ToolResult | None = None) -> None:
        self.name = name
        self.result = result or ToolResult.success({"status": "shipped", "eta": "amanhã"})
        self.calls: list[dict] = []
        self.contexts: list = []

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
        self.contexts.append(ctx)
        return self.result


class ExplodingProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.call_count += 1
        raise LLMError("502 from https://api.example.com/v1: upstream is down")


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _setup(client, **agent_overrides) -> tuple[dict, str, str]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant_response.json()["api_key"]}
    agent_payload = {
        "name": "Bot",
        "instructions": "Você é o atendente da Acme.",
        "model": "gpt-4o-mini",
        **agent_overrides,
    }
    agent = await client.post("/agents", json=agent_payload, headers=headers)
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


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _run_turn(fake_redis, db_session, runtime) -> None:
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _messages(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _agent_run(db_session, conversation_id: uuid.UUID) -> AgentRun:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    runs = list(result.scalars().all())
    assert len(runs) == 1
    return runs[0]


async def test_tool_call_then_final_answer(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido 12345")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="get_order", arguments={"order_id": "12345"})
                ],
                usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            ),
            LLMResponse(
                content="Seu pedido 12345 já foi enviado.",
                usage=LLMUsage(prompt_tokens=20, completion_tokens=7),
            ),
        ]
    )
    stub = StubExecutor()
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([stub]))
    )

    assert provider.call_count == 2
    assert stub.calls == [{"order_id": "12345"}]

    messages = await _messages(db_session, conversation_id)
    kinds = [(m.role, m.type) for m in messages]
    assert kinds == [
        (MessageRole.USER, "text"),
        (MessageRole.ASSISTANT, "tool_call"),
        (MessageRole.TOOL, "tool_result"),
        (MessageRole.ASSISTANT, "text"),
    ]

    tool_call, tool_result, reply = messages[1], messages[2], messages[3]
    assert tool_call.raw_payload["tool_calls"][0]["arguments"] == {"order_id": "12345"}
    assert tool_result.tool_call_id == "call_1"
    assert "shipped" in tool_result.content
    assert reply.content == "Seu pedido 12345 já foi enviado."
    assert tool_call.agent_run_id == tool_result.agent_run_id is not None

    run = await _agent_run(db_session, conversation_id)
    assert run.status == AgentRunStatus.SUCCESS
    assert (run.llm_calls, run.tool_calls) == (2, 1)
    assert (run.prompt_tokens, run.completion_tokens) == (30, 12)
    assert run.output_text == "Seu pedido 12345 já foi enviado."
    assert run.latency_ms is not None and run.error is None
    assert tool_call.agent_run_id == run.id

    # The tool spec reached the model, and the second call saw the tool result.
    assert [spec.name for spec in provider.calls[0]["tools"]] == ["get_order"]
    assert provider.calls[0]["model"] == "gpt-4o-mini"
    assert [m.role for m in provider.calls[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert provider.calls[1]["messages"][0].content.startswith("Você é o atendente da Acme.")


async def test_model_that_only_ever_calls_tools_stops_at_max_iterations(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client, max_tool_iterations=2)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido 12345")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="call_1", name="get_order", arguments={"order_id": "1"})]
            )
        ],
        repeat_last=True,
    )
    stub = StubExecutor()
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry([stub]))
    )

    assert provider.call_count == 2  # the cap, not one more
    assert len(stub.calls) == 2

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == MAX_ITERATIONS_MESSAGE

    run = await _agent_run(db_session, conversation_id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error == MAX_ITERATIONS_ERROR
    assert (run.llm_calls, run.tool_calls) == (2, 2)


async def test_unknown_tool_is_reported_back_to_the_model(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "faz mágica")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider(
        [
            LLMResponse(tool_calls=[ToolCall(id="call_1", name="do_magic", arguments={})]),
            LLMResponse(content="Não consigo fazer isso, mas posso ajudar de outro jeito."),
        ]
    )
    # Empty registry: NoopExecutor is the only thing behind it.
    await _run_turn(
        fake_redis, db_session, AgentRuntime(provider=provider, registry=ToolRegistry())
    )

    messages = await _messages(db_session, conversation_id)
    tool_result = next(m for m in messages if m.role == MessageRole.TOOL)
    assert "unknown tool: do_magic" in tool_result.content
    assert messages[-1].content == "Não consigo fazer isso, mas posso ajudar de outro jeito."

    run = await _agent_run(db_session, conversation_id)
    assert run.status == AgentRunStatus.SUCCESS
    assert run.tool_calls == 1


async def test_provider_failure_still_answers_and_records_the_error(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    provider = ExplodingProvider()
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    messages = await _messages(db_session, conversation_id)
    assert messages[-1].content == ERROR_MESSAGE

    run = await _agent_run(db_session, conversation_id)
    assert run.status == AgentRunStatus.ERROR
    assert run.error is not None and "LLMError" in run.error
    assert run.output_text == ERROR_MESSAGE
