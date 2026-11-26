"""E7 success demo: the clauses that need no Docker."""

# ruff: noqa: F811  (the imported `adapter` fixture is used as a test argument)

import re

import structlog

from app.agents.runtime import AgentRuntime
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation
from app.models.message import MessageRole
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from tests.test_agent_loop import ExplodingProvider, _drain_jobs, _messages, _run_turn, _send
from tests.test_agent_loop import _setup as _setup_agent
from tests.test_handoff_reply_owed import _inbound, _setup, adapter  # noqa: F401

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def test_turn_log_lines_carry_tenant_and_conversation(
    client, fake_redis, db_session, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    tenant_id = str(conversation.tenant_id)
    messages = await _inbound(db_session, conversation_id)
    runtime = AgentRuntime(
        provider=FakeLLMProvider([LLMResponse(content="ok")]),
        registry=ToolRegistry([BuiltinExecutor()]),
    )

    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        await process_turn(db_session, conversation_id, messages, runtime)

    line = next(e for e in logs if e["event"] == "agent.run_finished")
    assert line["tenant_id"] == tenant_id
    assert line["conversation_id"] == str(conversation_id)
    assert line["run_id"]


async def test_malformed_request_id_comes_back_replaced(client):
    response = await client.get("/health", headers={"X-Request-ID": "has space"})
    header = response.headers["X-Request-ID"]
    assert header != "has space"
    assert UUID_RE.match(header)


async def test_agent_error_message_reaches_the_customer(client, fake_redis, db_session):
    text = "Sorry, try again."
    headers, channel_id, _ = await _setup_agent(client, error_message=text)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    await _run_turn(fake_redis, db_session, AgentRuntime(provider=ExplodingProvider()))

    replies = [
        m.content
        for m in await _messages(db_session, conversation_id)
        if m.role == MessageRole.ASSISTANT
    ]
    assert replies == [text]
