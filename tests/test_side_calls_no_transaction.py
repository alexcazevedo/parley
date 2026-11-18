"""The frustration score and the handoff brief call the model with no
transaction open, and a timeout wraps only the model call.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

import app.agents.sentiment as sentiment_module
import app.handoff.summarizer as summarizer_module
from app.agents.sentiment import score_frustration
from app.config import Settings
from app.handoff.service import HandoffService, IllegalTransitionError
from app.handoff.summarizer import generate_handoff_summary, handoff_with_summary
from app.llm.base import LLMResponse
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import HandoffRequestedBy


class RecordingProvider:
    def __init__(self, session, content="0.5", delay=0.0):
        self.session = session
        self.content = content
        self.delay = delay
        self.calls = 0
        self.in_transaction: list[bool] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.calls += 1
        self.in_transaction.append(self.session.in_transaction())
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResponse(content=self.content)


def _agent() -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Bot",
        instructions="be nice",
        model="gpt-4o-mini",
        memory_enabled=False,
    )


def _conversation(agent: Agent, **kw) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        contact_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        agent_id=agent.id,
        **kw,
    )


def _patch_sentiment(monkeypatch, provider, timeout=5):
    monkeypatch.setattr(sentiment_module, "get_provider", lambda agent, settings=None: provider)
    monkeypatch.setattr(
        sentiment_module, "get_settings", lambda: Settings(sentiment_timeout_seconds=timeout)
    )


def _patch_summarizer(monkeypatch, provider, agent, timeout=None):
    async def loader(session, conversation):
        return agent

    monkeypatch.setattr(summarizer_module, "load_conversation_agent", loader)
    monkeypatch.setattr(summarizer_module, "get_provider", lambda agent, settings=None: provider)
    if timeout is not None:
        monkeypatch.setattr(summarizer_module, "SUMMARY_TIMEOUT_SECONDS", timeout)


async def _session_still_usable(session):
    result = await session.execute(text("select 1"))
    assert result.scalar_one() == 1
    await session.commit()


async def test_sentiment_no_transaction_during_model_call(db_session, monkeypatch):
    agent = _agent()
    provider = RecordingProvider(db_session, "0.4")
    _patch_sentiment(monkeypatch, provider)
    assert await score_frustration(db_session, _conversation(agent), agent, "oi") == 0.4
    assert provider.in_transaction == [False]


async def test_sentiment_timeout_leaves_session_usable(db_session, monkeypatch):
    agent = _agent()
    provider = RecordingProvider(db_session, delay=0.5)
    _patch_sentiment(monkeypatch, provider, timeout=0.05)
    assert await score_frustration(db_session, _conversation(agent), agent, "oi") is None
    assert provider.in_transaction == [False]
    await _session_still_usable(db_session)


async def test_summary_no_transaction_during_model_call(db_session, monkeypatch):
    agent = _agent()
    provider = RecordingProvider(db_session, "brief")
    _patch_summarizer(monkeypatch, provider, agent)
    assert await generate_handoff_summary(db_session, _conversation(agent), "x") == "brief"
    assert provider.in_transaction == [False]


async def test_summary_timeout_leaves_session_usable(db_session, monkeypatch):
    agent = _agent()
    provider = RecordingProvider(db_session, delay=0.5)
    _patch_summarizer(monkeypatch, provider, agent, timeout=0.05)
    assert await generate_handoff_summary(db_session, _conversation(agent), "x") is None
    assert provider.in_transaction == [False]
    await _session_still_usable(db_session)


async def test_ensure_can_handoff_rejects_bad_states(db_session):
    agent = _agent()
    service = HandoffService(db_session)
    for state in (ConversationState.HUMAN_HANDOFF, ConversationState.CLOSED):
        with pytest.raises(IllegalTransitionError):
            service.ensure_can_handoff(_conversation(agent, state=state))
    for state in (ConversationState.ACTIVE_AI, ConversationState.WAITING):
        assert service.ensure_can_handoff(_conversation(agent, state=state)) is None


async def test_illegal_handoff_raises_before_model_call(db_session, monkeypatch):
    agent = _agent()
    provider = RecordingProvider(db_session, "brief")
    _patch_summarizer(monkeypatch, provider, agent)
    conversation = _conversation(agent, state=ConversationState.CLOSED)
    with pytest.raises(IllegalTransitionError):
        await handoff_with_summary(db_session, conversation, "x", HandoffRequestedBy.AGENT)
    assert provider.calls == 0
