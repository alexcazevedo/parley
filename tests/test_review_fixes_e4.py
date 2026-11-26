"""Review fixes for the conversation-correctness run (ticket 06).

1. An operator who hands off or closes the conversation while the sentiment
   scorer runs ends the turn: the turn's own handoff is refused, nothing is
   sent, the runtime is never called, `process_turn` returns `None`.
2. A turn whose `handoff_to_human` committed and whose runtime then failed (or
   ran out of time) still sends the agent's handoff message, never the
   runtime's error text.
3. Migration 0012 locks `conversations` against writes before it closes the
   duplicates, so no new duplicate can slip in before the index is built.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import ERROR_MESSAGE, AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.handoff.summarizer import handoff_with_summary
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import DEFAULT_HANDOFF_MESSAGE, Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import HandoffRequestedBy
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from tests.test_operator_takeover import (
    AI_REPLY,
    HANDOFF_MESSAGE,
    _committed_state,
    _operator,
    _prepare,
    _stored_replies,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class CountingRuntime:
    """Text runtime that records whether the turn ever reached it."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, conversation, input_text: str) -> str:
        self.calls += 1
        return AI_REPLY


async def _enable_sentiment(db_session, conversation_id: uuid.UUID) -> None:
    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, conversation.agent_id)
    agent.sentiment_escalation_enabled = True
    await db_session.commit()


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [("handoff", ConversationState.HUMAN_HANDOFF), ("close", ConversationState.CLOSED)],
)
async def test_operator_action_during_sentiment_scoring_ends_the_turn(
    client, db_session, test_engine, monkeypatch, action, expected_state
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )
    await _enable_sentiment(db_session, conversation_id)

    async def frustrated_after_operator_acts(
        session, conversation, agent, input_text, message_ids=()
    ):
        await _operator(test_engine, conversation_id, action)
        return 0.99

    monkeypatch.setattr(turn_module, "score_frustration", frustrated_after_operator_acts)
    runtime = CountingRuntime()

    result = await process_turn(db_session, conversation_id, messages, runtime)

    assert result is None
    assert runtime.calls == 0
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []
    assert await _committed_state(test_engine, conversation_id) == expected_state


async def test_operator_handoff_during_sentiment_scoring_with_agent_runtime(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )
    await _enable_sentiment(db_session, conversation_id)

    async def frustrated_after_operator_acts(
        session, conversation, agent, input_text, message_ids=()
    ):
        await _operator(test_engine, conversation_id, "handoff")
        return 0.99

    monkeypatch.setattr(turn_module, "score_frustration", frustrated_after_operator_acts)
    provider = FakeLLMProvider([LLMResponse(content=AI_REPLY)])

    result = await process_turn(
        db_session, conversation_id, messages, AgentRuntime(provider=provider)
    )

    assert result is None
    assert adapter.sent == []
    assert await _stored_replies(test_engine, conversation_id) == []
    assert await _committed_state(test_engine, conversation_id) == ConversationState.HUMAN_HANDOFF


class _SlowBrief:
    async def complete(self, *args, **kwargs):
        await asyncio.sleep(0.6)
        return LLMResponse(content="brief")


async def _handoff_then_deadline(client, db_session, test_engine, monkeypatch, handoff_message):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )
    if handoff_message != HANDOFF_MESSAGE:
        conversation = await db_session.get(Conversation, conversation_id)
        agent = await db_session.get(Agent, conversation.agent_id)
        agent.handoff_message = handoff_message
        await db_session.commit()
    # The brief outlasts the turn's deadline, so the second tool of the same
    # step never runs: the runtime returns its generic error message.
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: _SlowBrief()
    )
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="handoff_to_human", arguments={"reason": "pediu"}),
                    ToolCall(id="c2", name="remember", arguments={"content": "gosta de azul"}),
                ]
            )
        ]
    )
    runtime = AgentRuntime(
        provider=provider, registry=ToolRegistry([BuiltinExecutor()]), timeout_seconds=0.4
    )
    await process_turn(db_session, conversation_id, messages, runtime)
    return conversation_id, adapter


async def test_handoff_then_deadline_sends_the_handoff_message(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, adapter = await _handoff_then_deadline(
        client, db_session, test_engine, monkeypatch, HANDOFF_MESSAGE
    )

    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]
    assert await _committed_state(test_engine, conversation_id) == ConversationState.HUMAN_HANDOFF


async def test_handoff_then_deadline_with_empty_handoff_message_sends_the_default(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, adapter = await _handoff_then_deadline(
        client, db_session, test_engine, monkeypatch, ""
    )

    assert adapter.sent == [DEFAULT_HANDOFF_MESSAGE]
    assert ERROR_MESSAGE not in await _stored_replies(test_engine, conversation_id)


class HandoffThenFailRuntime:
    """Session-aware double: commits a handoff, then fails like AgentRuntime does."""

    async def run_turn(self, session, conversation, input_text: str) -> str:
        await handoff_with_summary(
            session, conversation, reason="pediu", requested_by=HandoffRequestedBy.AGENT
        )
        await session.commit()
        return ERROR_MESSAGE


async def test_handoff_then_runtime_failure_sends_the_handoff_message(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )

    await process_turn(db_session, conversation_id, messages, HandoffThenFailRuntime())

    assert adapter.sent == [HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [HANDOFF_MESSAGE]


def test_migration_0012_locks_conversations_before_anything_else(monkeypatch):
    path = REPO_ROOT / "migrations" / "versions" / "0012_one_open_conversation.py"
    spec = importlib.util.spec_from_file_location("migration_0012_under_test", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    calls: list[tuple[str, str]] = []

    class RecordingOp:
        def execute(self, statement) -> None:
            calls.append(("execute", " ".join(str(statement).split())))

        def create_index(self, name, *args, **kwargs) -> None:
            calls.append(("create_index", name))

    monkeypatch.setattr(migration, "op", RecordingOp())
    migration.upgrade()

    assert calls[0] == ("execute", "LOCK TABLE conversations IN SHARE ROW EXCLUSIVE MODE")
    assert calls[-1] == ("create_index", "uq_conversations_open_contact_channel")
